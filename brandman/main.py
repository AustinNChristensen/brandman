from __future__ import annotations

import base64
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from html import escape
import os
import secrets
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Generic, TypeVar
from typing import Literal
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr

from . import extensions, principals, seed_packs, store
from .principals import operator_principal
from .attribution_store import AttributionStore, AttributionStoreError
from .approval_snapshots import ApprovalSnapshotStore
from .beehiiv_runtime import (
    NewsletterExportJobError,
    enqueue_newsletter_export,
    get_newsletter_export_job,
    list_newsletter_export_jobs,
    resolve_beehiiv_export_account,
)
from .beehiiv_assisted_sync import (
    BeehiivAssistedSyncError, ingest_beehiiv_measurements,
)
from .beehiiv_lifecycle import BeehiivNewsletterLifecycleProjector
from .content_dispatch import CanonicalPostNotFound, create_dispatch_from_post
from .dispatch import (
    DispatchError,
    GovernedDispatcher,
    Lifecycle,
    SQLiteDispatchStore,
    audit_event_to_dict,
    dispatch_item_to_dict,
)
from .distribution_package import DistributionPackageError, DistributionPackageStore
from .campaign_templates import CampaignTemplateError, CampaignTemplateStore
from .campaign_graph import CampaignGraphError, CampaignGraphStore
from .brand_guidelines import BrandGuidelineError, BrandGuidelineStore
from .brand_settings import BrandSettingsError, BrandSettingsStore
from .canonical_revalidation import CanonicalRevalidationError, CanonicalSourceRevalidationStore
from .canonical_runtime import enqueue_canonical_revalidation
from .learning_engine import BrandLearningEngine, LearningError
from .performance_planning import PerformancePlanningEngine
from .publishing_planner import PublishingPlanner, PublishingPlannerError
from .credentials import CredentialConfigurationError, CredentialStore
from .connection_product import CONNECTION_LANES, RATE_CARD_OPERATIONS, onboarding_manifest
from .connector_health import ConnectorHealthStore
from .feedback import FeedbackError, FeedbackNotFound, FeedbackStore
from .editorial import EditorialError, EditorialStore
from .engagement import (
    AntiSpamBlocked, EngagementError, EngagementInbox,
    InvalidEngagementTransition,
)
from .experiments import ExperimentError, ExperimentStore
from .execution_handoff import ExecutionHandoffError, ExecutionHandoffStore
from .operating_repository import build_operating_plan_service
from .operational_feedback import (
    report_approval_dead_end, report_missing_permission, report_stale_metric,
)
from .operator_workflow import build_operator_workflow
from .operator_proposals import OperatorProposalError, OperatorProposalStore
from .scheduler import PeriodicOrchestrator
from .readiness import LiveReadinessService
from .third_party_sources import ThirdPartySourceError, ThirdPartySourceService
from .provider_usage import ProviderUsageLedger
from .execution_agents import ExecutionAgentRegistry
from .beehiiv_assisted_pull import BeehiivAssistedPullError, BeehiivAssistedPullStore
from .http_security import (
    PREVIEW_SESSION_COOKIE,
    PREVIEW_SESSION_TTL_SECONDS,
    create_preview_session,
    enforce_request_boundary,
    harden_response,
    validate_preview_session,
)
from .hosted_mcp import (
    LazyMcpApplication,
    authorization_server_metadata,
    authorize as oauth_authorize,
    protected_resource_metadata,
    register_client as oauth_register_client,
    revoke as oauth_revoke,
    token as oauth_token,
    verify_mcp_bearer,
)


@dataclass(frozen=True)
class ApplicationServices:
    """One explicitly configured set of database-backed application services."""

    database: Path
    profile: str
    dispatch_store: SQLiteDispatchStore
    dispatcher: GovernedDispatcher
    attribution_store: AttributionStore
    approval_snapshot_store: ApprovalSnapshotStore
    editorial_store: EditorialStore
    brand_guideline_store: BrandGuidelineStore
    engagement_store: EngagementInbox
    operating_plan_service: Any
    periodic_orchestrator: PeriodicOrchestrator
    third_party_source_service: ThirdPartySourceService
    experiment_store: ExperimentStore
    provider_usage_ledger: ProviderUsageLedger
    execution_agent_registry: ExecutionAgentRegistry
    execution_handoff_store: ExecutionHandoffStore
    beehiiv_assisted_pull_store: BeehiivAssistedPullStore
    beehiiv_lifecycle_projector: BeehiivNewsletterLifecycleProjector
    publishing_planner: PublishingPlanner
    operator_proposal_store: OperatorProposalStore


_services: ApplicationServices | None = None
# Hosts that bind a database per request (``store.using_database``) keep one
# service set per database; a plain self-hosted process only ever has one.
_tenant_services: dict[tuple[str, str], ApplicationServices] = {}
_services_lock = RLock()
hosted_mcp_application = LazyMcpApplication()


def initialize_application_services(
    database: str | Path | None = None, *, profile: str | None = None,
) -> ApplicationServices:
    """Initialize the application only after path and profile are explicit.

    Importing this module is deliberately inert.  Runtime entry points must
    supply the database identity through ``store.DATA_PATH`` (or ``database``)
    and an explicit ``BRANDMAN_DATABASE_PROFILE`` (or ``profile``).  Profile
    compatibility is checked by :func:`store.init_db` before any schema write.
    """

    global _services
    path = Path(database if database is not None else store.DATA_PATH)
    if (
        database is None
        and "BRANDMAN_DB" not in os.environ
        and path.expanduser().resolve() == store.DEFAULT_DATA_PATH.expanduser().resolve()
    ):
        raise RuntimeError(
            "BRANDMAN_DB must be explicitly configured before BrandMan application startup"
        )
    requested_profile = profile or os.environ.get("BRANDMAN_DATABASE_PROFILE")
    if requested_profile is None:
        raise RuntimeError(
            "BRANDMAN_DATABASE_PROFILE must be explicitly configured before "
            "BrandMan application startup"
        )
    if requested_profile not in store.DATABASE_PROFILES:
        raise RuntimeError(
            "BRANDMAN_DATABASE_PROFILE must be operating, development, test, or proof"
        )
    key = (str(path.expanduser().resolve()), requested_profile)
    context_bound = database is None and store.database_override() is not None
    with _services_lock:
        if context_bound and key in _tenant_services:
            return _tenant_services[key]
        if not context_bound and _services is not None and (
            str(_services.database.expanduser().resolve()), _services.profile
        ) == key:
            return _services

        # All store helpers use this process-wide binding.  Bind it before the
        # single guarded initializer, then construct schema-owning services.
        # A context-bound database is already what ``store.DATA_PATH`` reads.
        if not context_bound:
            store.DATA_PATH = path
        store.init_db(profile=requested_profile)
        persisted_profile = store.database_profile(path)
        if persisted_profile != requested_profile:
            raise RuntimeError(
                f"configured profile {requested_profile} does not match database "
                f"profile {persisted_profile}"
            )
        dispatch = SQLiteDispatchStore(path)
        governed = GovernedDispatcher(dispatch)  # No external publishers here.
        editorial = EditorialStore(path)
        approval_snapshots = ApprovalSnapshotStore(path)
        guidelines = editorial.guidelines
        if requested_profile in {"operating", "development"}:
            for seed in seed_packs.brands():
                seeded_brand = store.get_brand(seed["slug"])
                if seeded_brand is not None:
                    guidelines.seed_guidelines(seeded_brand["id"], seed["slug"])
        services = ApplicationServices(
            database=path,
            profile=requested_profile,
            dispatch_store=dispatch,
            dispatcher=governed,
            attribution_store=AttributionStore(path),
            approval_snapshot_store=approval_snapshots,
            editorial_store=editorial,
            brand_guideline_store=guidelines,
            engagement_store=EngagementInbox(path),
            operating_plan_service=build_operating_plan_service(path),
            periodic_orchestrator=PeriodicOrchestrator(path),
            third_party_source_service=ThirdPartySourceService(path),
            experiment_store=ExperimentStore(path),
            provider_usage_ledger=ProviderUsageLedger(path),
            execution_agent_registry=ExecutionAgentRegistry(path),
            execution_handoff_store=ExecutionHandoffStore(path, editorial, governed),
            beehiiv_assisted_pull_store=BeehiivAssistedPullStore(path),
            beehiiv_lifecycle_projector=BeehiivNewsletterLifecycleProjector(path),
            publishing_planner=PublishingPlanner(path),
            operator_proposal_store=OperatorProposalStore(path, guidelines),
        )
        if context_bound:
            _tenant_services[key] = services
        else:
            _services = services
        return services


T = TypeVar("T")


class _LazyService(Generic[T]):
    """Stable compatibility handle that resolves only at runtime use."""

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)

    def _target(self) -> T:
        return getattr(initialize_application_services(), self._name)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._target(), name, value)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Rebind database-backed services for each application lifespan.

    Tests intentionally replace ``store.DATA_PATH`` before opening a TestClient;
    keeping initialization at lifespan entry preserves that isolation contract.
    """
    initialize_application_services()
    store.ensure_seeded_growth_missions()
    await hosted_mcp_application.start()
    try:
        yield
    finally:
        await hosted_mcp_application.stop()


app = FastAPI(
    title="BrandMan", version="0.1.0",
    description="Canonical brand context and approval-gated content operations.",
    lifespan=lifespan,
)
dispatch_store: SQLiteDispatchStore = _LazyService("dispatch_store")  # type: ignore[assignment]
dispatcher: GovernedDispatcher = _LazyService("dispatcher")  # type: ignore[assignment]
attribution_store: AttributionStore = _LazyService("attribution_store")  # type: ignore[assignment]
approval_snapshot_store: ApprovalSnapshotStore = _LazyService("approval_snapshot_store")  # type: ignore[assignment]
editorial_store: EditorialStore = _LazyService("editorial_store")  # type: ignore[assignment]
brand_guideline_store: BrandGuidelineStore = _LazyService("brand_guideline_store")  # type: ignore[assignment]
engagement_store: EngagementInbox = _LazyService("engagement_store")  # type: ignore[assignment]
operating_plan_service: Any = _LazyService("operating_plan_service")
periodic_orchestrator: PeriodicOrchestrator = _LazyService("periodic_orchestrator")  # type: ignore[assignment]
third_party_source_service: ThirdPartySourceService = _LazyService("third_party_source_service")  # type: ignore[assignment]
experiment_store: ExperimentStore = _LazyService("experiment_store")  # type: ignore[assignment]
provider_usage_ledger: ProviderUsageLedger = _LazyService("provider_usage_ledger")  # type: ignore[assignment]
execution_agent_registry: ExecutionAgentRegistry = _LazyService("execution_agent_registry")  # type: ignore[assignment]
execution_handoff_store: ExecutionHandoffStore = _LazyService("execution_handoff_store")  # type: ignore[assignment]
beehiiv_assisted_pull_store: BeehiivAssistedPullStore = _LazyService("beehiiv_assisted_pull_store")  # type: ignore[assignment]
beehiiv_lifecycle_projector: BeehiivNewsletterLifecycleProjector = _LazyService("beehiiv_lifecycle_projector")  # type: ignore[assignment]
publishing_planner: PublishingPlanner = _LazyService("publishing_planner")  # type: ignore[assignment]
operator_proposal_store: OperatorProposalStore = _LazyService("operator_proposal_store")  # type: ignore[assignment]
SYSTEM_QUEUE_ACTOR = "system:rest-queue"


@app.middleware("http")
async def preview_password_gate(request: Request, call_next):
    """Fail closed: every external route requires the deployment-only password."""
    boundary_failure = enforce_request_boundary(request)
    if boundary_failure is not None:
        return harden_response(boundary_failure, request)
    hosted = await extensions.authenticate(request)
    if isinstance(hosted, Response):
        return harden_response(hosted, request)
    if hosted is not None:
        return harden_response(await _call_as(hosted, request, call_next), request)
    password = os.getenv("BRANDMAN_PREVIEW_PASSWORD")
    if not password:
        response = JSONResponse(status_code=503, content={"detail": "Preview password is not configured."})
        return harden_response(response, request)
    # The marketing page at "/" is public by design (it is static and matches the live site).
    # Every operator surface, including /app and the API, stays behind authentication.
    if request.url.path == "/login" or (request.url.path == "/" and request.method == "GET"):
        return harden_response(await call_next(request), request)
    if request.url.path.startswith("/.well-known/oauth-") or request.url.path.startswith("/oauth/"):
        return harden_response(await call_next(request), request)
    if request.url.path == "/mcp" or request.url.path.startswith("/mcp/"):
        try:
            verify_mcp_bearer(request)
        except HTTPException as error:
            response = JSONResponse(
                status_code=error.status_code,
                content={"error": error.detail},
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{request.url.scheme}://{request.headers.get("host", "")}/.well-known/oauth-protected-resource/mcp"'},
            )
            return harden_response(response, request)
        return harden_response(await call_next(request), request)
    authorization = request.headers.get("authorization", "")
    basic_user = os.getenv("BRANDMAN_BASIC_USER", "").strip() or "operator"
    expected = base64.b64encode(f"{basic_user}:{password}".encode()).decode()
    basic_authenticated = secrets.compare_digest(authorization, f"Basic {expected}")
    session_authenticated = validate_preview_session(
        request.cookies.get(PREVIEW_SESSION_COOKIE, ""), password,
    )
    if not (basic_authenticated or session_authenticated):
        if _is_operator_navigation(request):
            destination = request.url.path
            if request.url.query:
                destination += "?" + request.url.query
            response = RedirectResponse("/login?" + urlencode({"next": destination}), status_code=303)
        else:
            response = JSONResponse(
                status_code=401,
                content={"detail": "Authentication required."},
                headers={"WWW-Authenticate": 'Basic realm="BrandMan"'},
            )
        return harden_response(response, request)
    # The preview credential is provisioned for the operator. Approval identity is set by
    # the authentication boundary and is never accepted from request content.
    request.state.principal = operator_principal()
    return harden_response(await call_next(request), request)


async def _call_as(authentication: extensions.Authentication, request: Request, call_next):
    """Run the request bound to a host-authenticated principal and database."""
    if authentication.principal is not None:
        request.state.principal = authentication.principal
    privileged = principals.set_privileged_principal(
        authentication.principal if authentication.privileged else None,
    )
    database = store.using_database(authentication.database) if authentication.database else nullcontext()
    try:
        with database:
            return await call_next(request)
    finally:
        principals.reset_privileged_principal(privileged)


def _is_operator_navigation(request: Request) -> bool:
    return (
        request.method.upper() in {"GET", "HEAD"}
        and (
            request.url.path in {"/", "/docs", "/redoc", "/app"}
            or request.url.path.startswith("/app/")
        )
        and "text/html" in request.headers.get("accept", "").casefold()
    )


def _safe_login_destination(value: str | None) -> str:
    candidate = (value or "/app").strip()
    if (
        not candidate.startswith("/") or candidate.startswith("//")
        or "\\" in candidate or any(ord(char) < 32 or ord(char) == 127 for char in candidate)
    ):
        return "/app"
    return candidate


def _login_page(*, destination: str | None = "/app", error: str | None = None) -> str:
    message = f'<p class="error" role="alert">{escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · BrandMan</title><style>
:root{{color-scheme:dark}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#101314;color:#f4f1e8;font:16px system-ui,sans-serif}}main{{width:min(420px,calc(100% - 40px));background:#1b2021;border:1px solid #394243;border-radius:18px;padding:32px;box-shadow:0 24px 80px #0008}}.eyebrow{{color:#b9a36a;font-size:12px;font-weight:700;letter-spacing:.14em}}h1{{font-size:30px;margin:10px 0}}p{{color:#bdc5c3;line-height:1.5}}label{{display:block;margin:24px 0 8px;font-weight:650}}input{{box-sizing:border-box;width:100%;border:1px solid #566160;border-radius:10px;padding:13px;background:#111515;color:#fff;font:inherit}}button{{width:100%;margin-top:18px;border:0;border-radius:10px;padding:13px;background:#d7bc72;color:#17170f;font:inherit;font-weight:750;cursor:pointer}}.error{{color:#ffb8ae;background:#3a2020;border-radius:8px;padding:10px}}small{{display:block;margin-top:18px;color:#84908e}}
</style></head><body><main><div class="eyebrow">BRANDMAN · PRIVATE PREVIEW</div><h1>Sign in</h1><p>BrandMan is in private preview. Enter the preview password you were given to open the operator console. If you were invited but have no password, ask the person who invited you; it is shared with invited users directly.</p>{message}<form method="post" action="/login"><input type="hidden" name="next" value="{escape(_safe_login_destination(destination), quote=True)}"><label for="password">Preview password</label><input id="password" name="password" type="password" autocomplete="current-password" required autofocus><button type="submit">Open BrandMan</button></form><small>Credentials stay in the request body and are never placed in the URL. This session expires automatically.</small></main></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return HTMLResponse(_login_page(destination=request.query_params.get("next")))


@app.post("/login")
async def create_login_session(request: Request) -> Response:
    body = await request.body()
    if len(body) > 4096:
        return HTMLResponse(_login_page(error="The sign-in request was too large."), status_code=413)
    fields = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    destination = _safe_login_destination(fields.get("next", ["/app"])[0])
    supplied = fields.get("password", [""])[0]
    password = os.getenv("BRANDMAN_PREVIEW_PASSWORD", "")
    if not password or not secrets.compare_digest(supplied, password):
        return HTMLResponse(_login_page(destination=destination, error="That preview password was not accepted."), status_code=401)
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        PREVIEW_SESSION_COOKIE, create_preview_session(password),
        max_age=PREVIEW_SESSION_TTL_SECONDS, httponly=True, samesite="strict",
        secure=request.url.scheme == "https", path="/",
    )
    return response


@app.post("/logout")
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(PREVIEW_SESSION_COOKIE, path="/")
    return response


# Hosted MCP discovery and OAuth are public by design.  They issue no service
# access without an operator password + PKCE exchange; the MCP transport itself
# is protected by a short-lived bearer token in the middleware above.
@app.get("/.well-known/oauth-protected-resource/mcp")
def mcp_protected_resource_metadata(request: Request) -> dict[str, object]:
    return protected_resource_metadata(request)


@app.get("/.well-known/oauth-authorization-server")
def mcp_authorization_server_metadata(request: Request) -> dict[str, object]:
    return authorization_server_metadata(request)


@app.post("/oauth/register")
async def mcp_register_client(request: Request) -> JSONResponse:
    return await oauth_register_client(request)


@app.get("/oauth/authorize", response_model=None)
@app.post("/oauth/authorize", response_model=None)
async def mcp_authorize(request: Request) -> RedirectResponse | HTMLResponse | JSONResponse:
    return await oauth_authorize(request)


@app.post("/oauth/token")
async def mcp_token(request: Request) -> JSONResponse:
    return await oauth_token(request)


@app.post("/oauth/revoke")
async def mcp_revoke(request: Request) -> JSONResponse:
    return await oauth_revoke(request)


class BrandInput(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    mission: str
    voice: str
    compliance_rules: str
    approval_policy: Literal["human_approval_required", "standing_approval"] = "human_approval_required"


class BrandGuidelineCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_type: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_*-]+$")
    channel: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_*-]+$")
    name: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=30000)
    rules: dict[str, Any]
    reason: str = Field(min_length=3, max_length=1000)
    source_ref: str | None = Field(default=None, max_length=2000)
    activate: bool = False


class BrandGuidelineVersionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instructions: str = Field(min_length=1, max_length=30000)
    rules: dict[str, Any]
    reason: str = Field(min_length=3, max_length=1000)
    source_ref: str | None = Field(default=None, max_length=2000)


class GuidelineActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=1000)


class NewsletterPolicyReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    checklist: dict[str, bool]


class NewsletterQuickHitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    reason: str = Field(min_length=12, max_length=1000)


class SourceInput(BaseModel):
    title: str
    source_type: Literal["beehiiv", "blog", "youtube", "rss", "manual"]
    body_summary: str
    url: str | None = None
    lifecycle_state: Literal["draft", "scheduled", "published"] = "draft"
    scheduled_for: datetime | None = None


class CanonicalRevalidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=200)


class BeehiivPostInput(BaseModel):
    id: str
    title: str
    editor_url: str | None = None
    status: Literal["draft", "scheduled", "published"]
    scheduled_at: datetime | None = None
    content_tags: list[dict[str, str]] = []
    subtitle: str | None = None
    subject_line: str | None = None
    seo_description: str | None = None


class BeehiivMeasurementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: datetime
    posts: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    publication_stats: dict[str, Any] | None = None


class AssistedBeehiivPullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_account_id: str = Field(min_length=1, max_length=200)
    scheduled_for: datetime


class CampaignInput(BaseModel):
    name: str
    objective: str
    source_id: str | None = None


class OperatorProposalPreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=8, max_length=2000)
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    candidate_ids: list[str] = Field(default_factory=list, max_length=20)


class OperatorProposalConfirmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal[True]


class PostInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: Literal["x", "instagram", "facebook", "linkedin", "newsletter"]
    body: str = Field(min_length=1, max_length=50000)
    scheduled_for: datetime | None = None


class PostEditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=50000)


class CandidateCampaignPostInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: str | None = None
    campaign_name: str = Field(default="", max_length=500)
    objective: str = Field(default="", max_length=2000)
    channel: Literal["x", "instagram", "facebook", "linkedin"] = "x"
    body: str = Field(min_length=1, max_length=50000)


class PublishingWindowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weekday: int = Field(ge=0, le=6)
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")


class PublishingPlanSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str = Field(min_length=1, max_length=100)
    windows: list[PublishingWindowInput] = Field(min_length=1, max_length=50)
    cadence_minutes: dict[str, int]


class PublishingPlanItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    initiative_id: str = Field(min_length=1, max_length=200)
    planned_for: datetime | None = None
    pinned: bool = False
    locked: bool = False


class PublishingReflowPreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_at: datetime


class PublishingReflowCommitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_id: str = Field(min_length=1, max_length=200)


class PublishingReflowUndoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit_id: str = Field(min_length=1, max_length=200)


class PerformanceInput(BaseModel):
    channel: str
    observed_at: datetime
    post_id: str | None = None
    source_id: str | None = None
    impressions: int = Field(default=0, ge=0)
    clicks: int = Field(default=0, ge=0)
    engagements: int = Field(default=0, ge=0)
    conversions: int = Field(default=0, ge=0)
    revenue_cents: int = Field(default=0, ge=0)
    notes: str = ""


class LearningInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=10000)
    proposed_change: str = Field(min_length=1, max_length=5000)
    evidence_for: list[dict] = Field(default_factory=list)
    evidence_against: list[dict] = Field(default_factory=list)
    effect: dict = Field(default_factory=dict)
    uncertainty: dict = Field(default_factory=dict)
    scope: dict = Field(default_factory=dict)
    review_at: str | None = None


class LearningLifecycleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=1000)


class ExperimentDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1, max_length=2000)
    metric: Literal["impressions", "clicks", "engagements", "conversions"]
    guardrails: dict = {}
    measurement_windows: list[dict] | None = Field(default=None, min_length=1, max_length=10)


class ProductFeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reporter: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$")
    summary: str = Field(min_length=1, max_length=500)
    details: str = Field(min_length=1, max_length=20_000)
    component: str = Field(default="agent-workflow", min_length=1, max_length=200)
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    fingerprint: str | None = None
    reproduction: str = ""
    expected_behavior: str = ""
    actual_behavior: str = ""
    workaround: str = ""
    related_ids: list[str] = []


class ExecutionClaimInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=100)
    lease_seconds: int = Field(default=900, ge=60, le=3600)


class ExternalReceiptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_token: str = Field(min_length=20, max_length=200)
    external_id: str = Field(min_length=1, max_length=500)
    external_url: str | None = Field(default=None, max_length=2000)
    status: Literal["draft", "posted"]
    content_fingerprint: str | None = Field(
        default=None, min_length=71, max_length=71, pattern=r"^sha256:[0-9a-f]{64}$",
    )
    asset_fingerprint: str | None = Field(
        default=None, min_length=71, max_length=71, pattern=r"^sha256:[0-9a-f]{64}$",
    )


class ExternalActionStartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=100)
    claim_token: str = Field(min_length=20, max_length=200)


class BeehiivPrivateDraftManifestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_path: str = Field(min_length=1, max_length=4096)
    existing_draft_id: str | None = Field(default=None, min_length=1, max_length=500)


class ExecutionDestinationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_account_id: str = Field(min_length=1, max_length=200)


class PublicActionConfirmationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    expected_material_fingerprint: str = Field(
        min_length=71, max_length=71, pattern=r"^sha256:[0-9a-f]{64}$",
    )
    confirmation_phrase: Literal["CONFIRM PUBLIC X POST"]
    validity_seconds: int = Field(default=300, ge=30, le=300)


class ExecutionControlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class FeedbackCommentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=10_000)


class FeedbackStartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignee: str = Field(min_length=1, max_length=100)
    implementation_links: list[str] = []
    implementation_notes: str = ""


class FeedbackResolveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution_evidence: str = Field(min_length=1)
    implementation_links: list[str] = []
    implementation_notes: str = ""


class FeedbackVerifyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence: str = Field(min_length=1)


class FeedbackReopenInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1)


class FeedbackReconcileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component: str = Field(min_length=1, max_length=100)
    keywords: list[str] = Field(min_length=1)
    implementation_links: list[str] = []


class KpiSnapshotInput(BaseModel):
    metric: Literal["x_followers", "active_beehiiv_subscribers"]
    value: float = Field(ge=0)
    observed_at: datetime
    source: Literal["x", "beehiiv", "manual"]
    dimensions: dict = {}
    connector_account_id: str | None = None
    connector_event_id: str | None = None
    verification_note: str = ""


class ConnectorAccountInput(BaseModel):
    connector_type: Literal["x", "beehiiv", "website", "rss"]
    account_key: str
    display_name: str
    status: Literal["healthy", "connected", "degraded", "expired", "disconnected", "needs_attention"] = "disconnected"
    scopes: list[str] = []
    capabilities: list[str] = []
    configuration: dict = {}


class ThirdPartyRssSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publisher_name: str = Field(min_length=1, max_length=200)
    feed_url: str = Field(min_length=1, max_length=2000)
    homepage_url: str | None = Field(default=None, max_length=2000)
    feed_format: Literal["auto", "rss", "atom"] = "auto"
    polling_interval_seconds: int = Field(default=1800, ge=900, le=86_400)
    reason: str = Field(min_length=1, max_length=2000)


class SourceControlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


class ConnectionCredentialInput(BaseModel):
    """Write-only connector authentication material.

    ``SecretStr`` prevents request values from appearing in model representations,
    validation output, and accidental application logs.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=200)
    credentials: dict[str, SecretStr] = Field(min_length=1)
    required_scopes: list[str] = []
    granted_scopes: list[str] = []


class ProviderRateCardInput(BaseModel):
    """Customer-supplied price for one provider request category."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=100)
    operation: Literal[
        "beehiiv_read", "beehiiv_draft", "x_owned_read", "x_general_read",
        "x_plain_post", "x_link_post",
    ]
    unit_price: str = Field(min_length=1, max_length=100)
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    effective_at: datetime


class ConnectionHealthInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["connected", "unhealthy", "reconnect_required"]
    error_code: str | None = None


class ConnectorHealthTriggerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_account_id: str | None = None
    timeout_seconds: float = Field(default=20, ge=1, le=60)


class TrackedUrlInput(BaseModel):
    base_url: str
    source: str
    medium: str
    campaign_id: str
    artifact_id: str
    cta_id: str


class EditorialDimensions(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    relevance: float = Field(default=0, ge=0, le=1)
    urgency: float = Field(default=0, ge=0, le=1)
    reader_value: float = Field(default=0, ge=0, le=1)
    novelty: float = Field(default=0, ge=0, le=1)
    confidence: float = Field(default=0, ge=0, le=1)
    search_opportunity: float = Field(default=0, ge=0, le=1, validation_alias=AliasChoices("search_opportunity", "search_potential"))
    social_potential: float = Field(default=0, ge=0, le=1, validation_alias=AliasChoices("social_potential", "social_discussion"))
    commercial_relevance: float = Field(default=0, ge=0, le=1)
    differentiation: float = Field(default=0, ge=0, le=1, validation_alias=AliasChoices("differentiation", "brand_differentiation"))


class EditorialCandidateInput(BaseModel):
    title: str
    summary: str = ""
    dimensions: EditorialDimensions
    recommended_treatment: str = "monitor"
    rationale: list[str] = []
    supporting_sources: list[dict] = []


class NewsletterIssueInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: dict
    candidate_id: str | None = None


class NewsletterRevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: dict
    change_note: str = ""


class NewsletterTransitionInput(BaseModel):
    target: Literal["outline", "draft", "fact_checked"]


class EditorialCleanupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


class ClaimVerdictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str = Field(min_length=1)
    verified: bool
    notes: str = ""


class NewsletterFactCheckInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    verdicts: list[ClaimVerdictInput] = []
    notes: str = ""


class NewsletterApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    review_token: str = Field(min_length=20)


class NewsletterRejectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)


class NewsletterExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_account_id: str | None = None
    run_after: datetime | None = None
    priority: int = Field(default=10, ge=-100, le=100)
    max_attempts: int = Field(default=3, ge=1, le=10)


class DistributionEmailInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=300)
    preview_text: str = Field(min_length=1, max_length=500)


class DistributionWebInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    slug: str = Field(min_length=1, max_length=300, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    seo_description: str = Field(min_length=1, max_length=500)
    url: str | None = Field(default=None, min_length=1, max_length=2000)


class DistributionPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audience: str = Field(min_length=1, max_length=500)
    primary_cta: str = Field(min_length=1, max_length=500)
    measurement_plan: str = Field(min_length=1, max_length=2000)
    baseline: dict = Field(default_factory=dict)


class DistributionXDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=2000)
    role: str = Field(min_length=1, max_length=100)
    hook: str = Field(min_length=1, max_length=500)
    cta: str = Field(min_length=1, max_length=500)
    destination_url: str | None = Field(default=None, min_length=1, max_length=2000)


class DistributionPackageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    primary_source_id: str = Field(min_length=1)
    campaign_name: str = Field(min_length=1, max_length=300)
    objective: str = Field(min_length=1, max_length=2000)
    email: DistributionEmailInput
    web: DistributionWebInput
    distribution: DistributionPlanInput
    x_drafts: list[DistributionXDraftInput] = Field(min_length=1, max_length=10)
    idempotency_key: str = Field(min_length=1, max_length=300)
    flight_name: str = Field(default="primary", min_length=1, max_length=100)
    flight_start: datetime | None = None
    flight_end: datetime | None = None


class DistributionMembershipMoveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_package_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=1000)


class DistributionDestinationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination_url: str = Field(min_length=1, max_length=2000)


class CampaignTemplatePreflightInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: dict
    override_reason: str | None = Field(default=None, max_length=1000)


class CampaignTemplateInstantiateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: dict
    name: str = Field(min_length=1, max_length=300)
    objective: str = Field(min_length=1, max_length=2000)
    source_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=300)
    flight_name: str = Field(default="primary", min_length=1, max_length=100)
    flight_start: datetime | None = None
    flight_end: datetime | None = None


class CampaignMembershipAttachInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_type: str = Field(min_length=1, max_length=100)
    asset_id: str = Field(min_length=1)
    channel: Literal["newsletter", "email", "web", "youtube", "x"]
    role: Literal["anchor", "touchpoint", "supporting"]
    reason: str = Field(min_length=1, max_length=1000)
    sequence: int = Field(default=0, ge=0)
    phase: str = Field(default="primary", min_length=1, max_length=100)
    attribution_context: str = Field(default="distribution", min_length=1, max_length=100)
    attribution_primary: bool = False
    flight_name: str = Field(default="primary", min_length=1, max_length=100)
    window_start: datetime | None = None
    window_end: datetime | None = None
    notes: str = Field(default="", max_length=2000)


class CampaignReasonInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)


class CampaignReorderInput(CampaignReasonInput):
    membership_ids: list[str] = Field(min_length=1)


class CampaignRelationshipInput(CampaignReasonInput):
    from_membership_id: str = Field(min_length=1)
    to_membership_id: str = Field(min_length=1)
    relationship_type: str = Field(min_length=1, max_length=100)
    notes: str = Field(default="", max_length=2000)


class CampaignFlightInput(CampaignReasonInput):
    name: str = Field(min_length=1, max_length=100)
    starts_at: datetime
    ends_at: datetime


class CampaignMetricInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observed_at: datetime
    native_metrics: dict[str, int]
    conversions: int = Field(default=0, ge=0)
    revenue_cents: int = Field(default=0, ge=0)
    attribution_confidence: str = Field(default="reported_not_independently_verified", min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=300)


class DispatchCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector: str = Field(min_length=1, max_length=50)
    payload: dict


class DispatchEditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: dict


class DispatchActorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DispatchRevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)


class DispatchApprovalInput(DispatchRevisionInput):
    review_token: str = Field(min_length=20)


class DispatchBatchMember(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    revision: int = Field(ge=1)
    review_token: str = Field(min_length=20)


class DispatchBatchApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[DispatchBatchMember] = Field(min_length=1)
    batch_id: str | None = None


class EngagementDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: Literal["reply", "like", "follow"]
    text: str | None = Field(default=None, max_length=280)


class EngagementActorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EngagementDismissInput(EngagementActorInput):
    reason: str = Field(default="", max_length=1000)


class OrchestrationTickInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: datetime | None = None
    max_decisions: int = Field(default=50, ge=1, le=500)


class BrandSettingsUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mission: str | None = Field(default=None, min_length=1, max_length=10000)
    voice: str | None = Field(default=None, min_length=1, max_length=10000)
    compliance_rules: str | None = Field(default=None, min_length=1, max_length=10000)
    approval_policy: Literal["human_approval_required", "standing_approval"] | None = None
    reason: str = Field(min_length=3, max_length=1000)


class ScheduleControlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class ExecutionAgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(min_length=1, max_length=200)
    channel: Literal["browser", "mcp"]
    enabled: bool = True


class AssistedPullClaimInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=100)
    lease_seconds: int = Field(default=900, ge=60, le=3600)


class AssistedPullHeartbeatInput(AssistedPullClaimInput):
    claim_token: str = Field(min_length=20, max_length=200)


class AssistedPullReceiptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=100)
    claim_token: str = Field(min_length=20, max_length=200)
    observed_at: datetime
    posts: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    publication_stats: dict[str, Any] | None = None


class AssistedPullFailureInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=100)
    claim_token: str = Field(min_length=20, max_length=200)
    failure_code: str = Field(min_length=3, max_length=64)


def _brand_connector_owner(slug: str, provider: str, account_id: str) -> tuple[dict, dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    rows = store.rows(
        """SELECT * FROM connector_accounts
           WHERE connector_type=? AND account_key=?""",
        (provider, account_id),
    )
    owned_row = next((row for row in rows if row["brand_id"] == brand["id"]), None)
    if owned_row is None:
        raise HTTPException(
            status_code=409,
            detail="Register this connection lane for the brand before storing its credential.",
        )
    if any(row["brand_id"] != brand["id"] for row in rows):
        raise HTTPException(
            status_code=409,
            detail="Connection identifier is already used by another brand; choose a unique lane identifier.",
        )
    owned = next(
        row for row in store.list_connector_accounts(brand["id"])
        if row["id"] == owned_row["id"]
    )
    return brand, owned


def _credential_store() -> CredentialStore:
    """Construct the encrypted store only when deployment supplied its key."""

    master_key = os.getenv("BRANDMAN_CREDENTIAL_MASTER_KEY")
    if not master_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "Connector credential encryption is not configured. "
                "Set BRANDMAN_CREDENTIAL_MASTER_KEY before managing connections."
            ),
        )
    try:
        return CredentialStore(store.DATA_PATH, master_key)
    except CredentialConfigurationError as error:
        raise HTTPException(
            status_code=503,
            detail="Connector credential encryption is misconfigured; provision a valid Fernet master key.",
        ) from error


def _feedback_store() -> FeedbackStore:
    return FeedbackStore(store.DATA_PATH)


def _feedback_operation(operation):
    try:
        return operation()
    except FeedbackNotFound as error:
        raise HTTPException(status_code=404, detail="Product feedback not found") from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except FeedbackError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "brand-os"}


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


DASHBOARD_V2_DIR = (Path(__file__).parent / "static" / "app").resolve()


@app.get("/app")
@app.get("/app/{path:path}")
def dashboard_v2(path: str = "") -> FileResponse:
    """Serve the built React dashboard (web/ -> brandman/static/app).

    Hashed asset files are served by exact path; every other path under /app
    returns the SPA entry so client-side routes survive a reload. The build
    output is same-origin only, so the CSP's ``'self'`` rule covers it, and
    the request still passes the boundary + preview password middleware.
    """
    if path:
        candidate = (DASHBOARD_V2_DIR / path).resolve()
        if candidate.is_relative_to(DASHBOARD_V2_DIR) and candidate.is_file():
            return FileResponse(candidate)
        # Hashed build assets are concrete resources, not client-side routes.
        # Returning the SPA entry here turns a stale deployment reference into
        # a misleading 200 response with an HTML MIME type.
        if path == "assets" or path.startswith("assets/"):
            raise HTTPException(status_code=404, detail="Dashboard asset not found")
    entry = DASHBOARD_V2_DIR / "index.html"
    if not entry.is_file():
        raise HTTPException(status_code=503, detail="BrandMan dashboard build is missing; run `python scripts/build_dashboard.py` in a source checkout.")
    return FileResponse(entry)


def _brand_learning(slug: str, learning_id: str) -> tuple[dict, BrandLearningEngine]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    engine = BrandLearningEngine(store.DATA_PATH)
    try:
        learning = engine.get(learning_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Learning not found") from error
    if learning["brand_id"] != brand["id"]:
        raise HTTPException(status_code=404, detail="Learning not found")
    return learning, engine


def _brand_experiment(slug: str, experiment_id: str) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        experiment = experiment_store.get(experiment_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Experiment not found") from error
    if experiment["brand_id"] != brand["id"]:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return experiment


def _brand_feedback(slug: str, feedback_id: str) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    item = _feedback_operation(lambda: _feedback_store().get(feedback_id))
    if item["brand_id"] != brand["id"]:
        raise HTTPException(status_code=404, detail="Product feedback not found")
    return item


def _distribution_packages() -> DistributionPackageStore:
    return DistributionPackageStore(store.DATA_PATH, editorial_store, dispatcher)


def _distribution_operation(operation):
    try:
        return operation()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Distribution package not found") from error
    except (DistributionPackageError, EditorialError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _campaign_graph() -> CampaignGraphStore:
    # DistributionPackageStore owns compatible migrations used by both the
    # newsletter recipe and generic graph operations.
    _distribution_packages()
    return CampaignGraphStore(store.DATA_PATH)


def _campaign_graph_operation(operation):
    try:
        return operation()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Campaign graph resource not found") from error
    except CampaignGraphError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _operator_proposal_operation(operation):
    try:
        return operation()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Operator proposal not found") from error
    except OperatorProposalError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _engagement_operation(operation):
    try:
        return operation()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Engagement opportunity not found") from error
    except (
        AntiSpamBlocked, InvalidEngagementTransition, EngagementError,
        DispatchError, ValueError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _brand_engagement(slug: str, opportunity_id: str) -> tuple[dict, dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    opportunity = engagement_store.get(opportunity_id)
    if opportunity["brand_id"] != brand["id"]:
        raise HTTPException(status_code=404, detail="Engagement opportunity not found")
    return brand, opportunity


def _dispatch_operation(operation):
    try:
        return operation()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Dispatch item not found") from error
    except (DispatchError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


app.mount("/mcp", hosted_mcp_application)


# Domain routers. Imported last: they read the models, services and helpers above.
from .routes import brands, content, newsletters, dispatch, execution, connections, performance, engagement, feedback, orchestration  # noqa: E402

for _routes in (brands, content, newsletters, dispatch, execution, connections, performance, engagement, feedback, orchestration):
    app.include_router(_routes.router)
from .routes.performance import get_mission_scorecard, get_morning_plan  # noqa: E402,F401  (used by the MCP server)


# Installed ``brandman.plugins`` may add routes. They register last, so core
# routes keep precedence.
extensions.load_plugins("app", app)
