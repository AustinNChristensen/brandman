from __future__ import annotations

import base64
from contextlib import asynccontextmanager
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

from . import store
from .principal import PREVIEW_PRINCIPAL
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
_services_lock = RLock()
hosted_mcp_application = LazyMcpApplication()


def initialize_application_services(
    database: str | Path | None = None, *, profile: str | None = None,
) -> ApplicationServices:
    """Initialize the application only after path and profile are explicit.

    Importing this module is deliberately inert.  Runtime entry points must
    supply the database identity through ``store.DATA_PATH`` (or ``database``)
    and an explicit ``BRAND_OS_DATABASE_PROFILE`` (or ``profile``).  Profile
    compatibility is checked by :func:`store.init_db` before any schema write.
    """

    global _services
    path = Path(database if database is not None else store.DATA_PATH)
    if (
        database is None
        and "BRAND_OS_DB" not in os.environ
        and path.expanduser().resolve() == store.DEFAULT_DATA_PATH.expanduser().resolve()
    ):
        raise RuntimeError(
            "BRAND_OS_DB must be explicitly configured before Brand OS application startup"
        )
    requested_profile = profile or os.environ.get("BRAND_OS_DATABASE_PROFILE")
    if requested_profile is None:
        raise RuntimeError(
            "BRAND_OS_DATABASE_PROFILE must be explicitly configured before "
            "Brand OS application startup"
        )
    if requested_profile not in store.DATABASE_PROFILES:
        raise RuntimeError(
            "BRAND_OS_DATABASE_PROFILE must be operating, development, test, or proof"
        )
    key = (str(path.expanduser().resolve()), requested_profile)
    with _services_lock:
        if _services is not None and (
            str(_services.database.expanduser().resolve()), _services.profile
        ) == key:
            return _services

        # All store helpers use this process-wide binding.  Bind it before the
        # single guarded initializer, then construct schema-owning services.
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
            demo_brand = store.get_brand("demo-brand")
            if demo_brand is not None:
                guidelines.seed_demo_brand(demo_brand["id"])
        _services = ApplicationServices(
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
        return _services


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
    store.ensure_demo_brand_growth_mission()
    await hosted_mcp_application.start()
    try:
        yield
    finally:
        await hosted_mcp_application.stop()


app = FastAPI(
    title="Brand OS", version="0.1.0",
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
    password = os.getenv("BRAND_OS_PREVIEW_PASSWORD")
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
    expected = base64.b64encode(f"operator:{password}".encode()).decode()
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
                headers={"WWW-Authenticate": 'Basic realm="Brand OS preview"'},
            )
        return harden_response(response, request)
    # The preview credential is shared and carries no personal identity, so the generic preview operator is used. Approval identity is set by
    # the authentication boundary and is never accepted from request content.
    request.state.principal = PREVIEW_PRINCIPAL
    return harden_response(await call_next(request), request)


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
<title>Sign in · Brand OS</title><style>
:root{{color-scheme:dark}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#101314;color:#f4f1e8;font:16px system-ui,sans-serif}}main{{width:min(420px,calc(100% - 40px));background:#1b2021;border:1px solid #394243;border-radius:18px;padding:32px;box-shadow:0 24px 80px #0008}}.eyebrow{{color:#b9a36a;font-size:12px;font-weight:700;letter-spacing:.14em}}h1{{font-size:30px;margin:10px 0}}p{{color:#bdc5c3;line-height:1.5}}label{{display:block;margin:24px 0 8px;font-weight:650}}input{{box-sizing:border-box;width:100%;border:1px solid #566160;border-radius:10px;padding:13px;background:#111515;color:#fff;font:inherit}}button{{width:100%;margin-top:18px;border:0;border-radius:10px;padding:13px;background:#d7bc72;color:#17170f;font:inherit;font-weight:750;cursor:pointer}}.error{{color:#ffb8ae;background:#3a2020;border-radius:8px;padding:10px}}small{{display:block;margin-top:18px;color:#84908e}}
</style></head><body><main><div class="eyebrow">BRAND OS · PRIVATE PREVIEW</div><h1>Sign in</h1><p>BrandOS is in private preview. Enter the preview password you were given to open the operator console. If you were invited but have no password, ask the person who invited you; it is shared with invited users directly.</p>{message}<form method="post" action="/login"><input type="hidden" name="next" value="{escape(_safe_login_destination(destination), quote=True)}"><label for="password">Preview password</label><input id="password" name="password" type="password" autocomplete="current-password" required autofocus><button type="submit">Open BrandOS</button></form><small>Credentials stay in the request body and are never placed in the URL. This session expires automatically.</small></main></body></html>"""


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
    password = os.getenv("BRAND_OS_PREVIEW_PASSWORD", "")
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


@app.get("/api/orchestration/status")
def get_orchestration_status() -> dict:
    return periodic_orchestrator.status()


@app.get("/api/brands/{slug}/readiness")
def get_live_readiness(slug: str) -> dict:
    """Return a provider-safe, read-only launch preflight for one brand."""
    try:
        return LiveReadinessService(store.DATA_PATH).inspect(slug)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Brand not found") from error


@app.get("/api/brands/{slug}/provider-usage")
def get_provider_usage(slug: str) -> dict:
    """Return payload-free API request units and operator-priced estimates."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return provider_usage_ledger.report(brand["id"])


@app.post("/api/brands/{slug}/provider-rate-cards", status_code=201)
def create_provider_rate_card(
    slug: str, input: ProviderRateCardInput, request: Request,
) -> dict:
    """Add a tenant-scoped rate supplied from that customer's provider plan."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return provider_usage_ledger.configure_price(
            brand_id=brand["id"], actor=request.state.principal,
            version=input.version, unit_price=input.unit_price,
            currency=input.currency, effective_at=input.effective_at.isoformat(),
            **RATE_CARD_OPERATIONS[input.operation],
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/brands/{slug}/connection-onboarding")
def get_connection_onboarding(slug: str) -> dict:
    """Explain assisted and standalone connection choices without secrets."""
    if not store.get_brand(slug):
        raise HTTPException(status_code=404, detail="Brand not found")
    configured = False
    try:
        CredentialStore._cipher(os.getenv("BRAND_OS_CREDENTIAL_MASTER_KEY"))
        configured = True
    except CredentialConfigurationError:
        pass
    return onboarding_manifest(encryption_configured=configured)


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


@app.get("/api/brands/{slug}/connections")
def list_brand_connections(slug: str) -> list[dict]:
    """List only redacted credential metadata owned by this brand."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    owned = {
        (row["connector_type"], row["account_key"])
        for row in store.list_connector_accounts(brand["id"])
    }
    return [
        item.as_dict() for item in _credential_store().list()
        if (item.provider, item.account_id) in owned
    ]


@app.put("/api/brands/{slug}/connections/{provider}/{account_id}")
def connect_brand_account(
    slug: str, provider: Literal["x", "beehiiv", "website"], account_id: str,
    input: ConnectionCredentialInput, request: Request,
) -> dict:
    """Store a credential only for an unambiguously brand-owned lane."""
    brand, connector = _brand_connector_owner(slug, provider, account_id)
    role = (connector.get("configuration") or {}).get("connection_role")
    lane = CONNECTION_LANES.get(role)
    if lane is None and provider != "website":
        raise HTTPException(
            status_code=422,
            detail="Choose a supported separate read or write lane before storing a credential.",
        )
    if lane is not None and (
        lane["provider"] != provider
        or set(connector.get("scopes") or []) != set(lane["scopes"])
        or set(connector.get("capabilities") or []) != set(lane["capabilities"])
    ):
        raise HTTPException(
            status_code=422,
            detail="Connection lane access does not match the least-privilege onboarding contract.",
        )
    expected_scopes = set(connector.get("scopes") or [])
    if set(input.required_scopes) != expected_scopes or set(input.granted_scopes) != expected_scopes:
        raise HTTPException(
            status_code=422,
            detail="Credential scopes must exactly match this connection lane; use a separate lane for different access.",
        )
    expected_fields = {
        "beehiiv_read": {"api_key"}, "beehiiv_write": {"api_key"},
        "x_read": {"access_token", "refresh_token", "client_id", "expires_at"},
        "x_write": {"access_token", "refresh_token", "client_id", "expires_at"},
        "website": {"access_token"},
    }.get(role or ("website" if provider == "website" else ""))
    if expected_fields is None or set(input.credentials) != expected_fields:
        raise HTTPException(
            status_code=422,
            detail="Credential fields do not match this connection lane.",
        )
    if any(not name.strip() or not value.get_secret_value() for name, value in input.credentials.items()):
        raise HTTPException(status_code=422, detail="Credential names and values must not be empty.")
    credentials = {name: value.get_secret_value() for name, value in input.credentials.items()}
    try:
        metadata = _credential_store().put(
            provider, account_id, input.display_name, credentials,
            required_scopes=input.required_scopes,
            granted_scopes=input.granted_scopes,
            actor=request.state.principal,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Invalid connector credential metadata.") from error
    if metadata.missing_scopes:
        report_missing_permission(
            brand_id=brand["id"], connector_account_id=connector["id"],
            provider=provider, operation="connect",
            missing_scopes=list(metadata.missing_scopes),
        )
    return metadata.as_dict()


@app.post("/api/brands/{slug}/connections/{provider}/{account_id}/disconnect")
def disconnect_brand_account(
    slug: str, provider: Literal["x", "beehiiv", "website"],
    account_id: str, request: Request,
) -> dict:
    _brand_connector_owner(slug, provider, account_id)
    try:
        return _credential_store().disconnect(
            provider, account_id, actor=request.state.principal,
        ).as_dict()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Connector account not found") from error


@app.get("/api/brands/{slug}/execution-agents")
def list_execution_agents(slug: str) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return execution_agent_registry.list(brand["id"])


@app.post("/api/brands/{slug}/execution-agents")
def configure_execution_agent(slug: str, input: ExecutionAgentInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return execution_agent_registry.configure(
            brand["id"], input.agent_id, input.channel, enabled=input.enabled,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/brands/{slug}/execution-agents/{agent_id}/heartbeat")
def heartbeat_execution_agent(slug: str, agent_id: str) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return execution_agent_registry.heartbeat(brand["id"], agent_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Execution agent not found or disabled") from error


@app.get("/api/orchestration/schedules")
def list_orchestration_schedules() -> list[dict]:
    return periodic_orchestrator.list_schedules()


@app.post("/api/orchestration/tick")
def trigger_orchestration_tick(input: OrchestrationTickInput) -> dict:
    periodic_orchestrator.ensure_defaults()
    return periodic_orchestrator.tick(
        as_of=input.as_of.isoformat() if input.as_of else None,
        max_decisions=input.max_decisions,
    ).as_dict()


@app.get("/api/brands/{slug}/settings")
def get_brand_settings(slug: str) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    settings = BrandSettingsStore(store.DATA_PATH)
    return {
        "brand": brand,
        "audit": settings.audit(brand["id"]),
        "rate_cards": provider_usage_ledger.list_prices(brand["id"]),
        "orchestration": periodic_orchestrator.status(brand_id=brand["id"]),
        "schedules": periodic_orchestrator.list_schedules(brand_id=brand["id"]),
    }


@app.patch("/api/brands/{slug}/settings")
def update_brand_settings(slug: str, input: BrandSettingsUpdateInput, request: Request) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    changes = input.model_dump(exclude={"reason"}, exclude_none=True)
    try:
        return BrandSettingsStore(store.DATA_PATH).update(
            brand["id"], changes, actor=request.state.principal, reason=input.reason,
        )
    except BrandSettingsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.put("/api/brands/{slug}/orchestration/schedules/{schedule_key:path}")
def set_brand_schedule(slug: str, schedule_key: str, input: ScheduleControlInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    schedule = next(
        (item for item in periodic_orchestrator.list_schedules(brand_id=brand["id"])
         if item["schedule_key"] == schedule_key), None,
    )
    if schedule is None:
        raise HTTPException(status_code=404, detail="Brand schedule not found")
    try:
        return periodic_orchestrator.set_schedule_enabled(
            schedule_key, input.enabled, brand_id=brand["id"],
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Brand schedule not found") from error


@app.post("/api/brands/{slug}/orchestration/tick")
def trigger_brand_orchestration_tick(slug: str, input: OrchestrationTickInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    periodic_orchestrator.ensure_defaults()
    return periodic_orchestrator.tick(
        as_of=input.as_of.isoformat() if input.as_of else None,
        max_decisions=input.max_decisions, brand_id=brand["id"],
    ).as_dict()


def _credential_store() -> CredentialStore:
    """Construct the encrypted store only when deployment supplied its key."""

    master_key = os.getenv("BRAND_OS_CREDENTIAL_MASTER_KEY")
    if not master_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "Connector credential encryption is not configured. "
                "Set BRAND_OS_CREDENTIAL_MASTER_KEY before managing connections."
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
    """Serve the built React dashboard (web/ -> app/static/app).

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
        raise HTTPException(status_code=503, detail="Dashboard build is missing; run `npm run build` in web/.")
    return FileResponse(entry)


@app.get("/api/brands")
def list_brands() -> list[dict]:
    return store.rows("SELECT * FROM brands ORDER BY name")


@app.post("/api/brands", status_code=201)
def create_brand(input: BrandInput) -> dict:
    try:
        return store.create_brand(input.model_dump())
    except Exception as error:
        raise HTTPException(status_code=409, detail="Brand slug already exists") from error


@app.get("/api/brands/{slug}/context")
def get_brand_context(slug: str) -> dict:
    context = store.brand_context(slug)
    if not context:
        raise HTTPException(status_code=404, detail="Brand not found")
    context["active_guidelines"] = [
        item for item in brand_guideline_store.list(context["id"])
        if item.get("status") == "active"
    ]
    return context


@app.get("/api/brands/{slug}/guidelines")
def list_brand_guidelines(slug: str, include_archived: bool = False) -> list[dict[str, Any]]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return brand_guideline_store.list(brand["id"], include_archived=include_archived)


@app.get("/api/brands/{slug}/guidelines/active")
def get_active_brand_guideline(slug: str, content_type: str, channel: str) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    result = brand_guideline_store.resolve(brand["id"], content_type, channel)
    if result is None:
        raise HTTPException(status_code=404, detail="No active guideline applies to this scope")
    return result


@app.post("/api/brands/{slug}/guidelines", status_code=201)
def create_brand_guideline(
    slug: str, input: BrandGuidelineCreateInput, request: Request,
) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return brand_guideline_store.create(
            brand_id=brand["id"], actor=request.state.principal, **input.model_dump(),
        )
    except BrandGuidelineError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/brand-guidelines/{guideline_id}/versions", status_code=201)
def create_brand_guideline_version(
    guideline_id: str, input: BrandGuidelineVersionInput, request: Request,
) -> dict[str, Any]:
    try:
        return brand_guideline_store.create_version(
            guideline_id, actor=request.state.principal, **input.model_dump(),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except BrandGuidelineError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/brand-guidelines/{guideline_id}/versions/{version}/activate")
def activate_brand_guideline_version(
    guideline_id: str, version: int, input: GuidelineActionInput, request: Request,
) -> dict[str, Any]:
    try:
        return brand_guideline_store.activate(
            guideline_id, version, actor=request.state.principal, reason=input.reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except BrandGuidelineError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/brand-guidelines/{guideline_id}/audit")
def get_brand_guideline_audit(guideline_id: str) -> list[dict[str, Any]]:
    try:
        return brand_guideline_store.audit(guideline_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.delete("/api/brand-guidelines/{guideline_id}")
def archive_brand_guideline(
    guideline_id: str, input: GuidelineActionInput, request: Request,
) -> dict[str, Any]:
    try:
        return brand_guideline_store.archive(
            guideline_id, actor=request.state.principal, reason=input.reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except BrandGuidelineError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/brands/{slug}/sources", status_code=201)
def create_source(slug: str, input: SourceInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return store.insert("sources", {"brand_id": brand["id"], **input.model_dump(mode="json")})


@app.get("/api/brands/{slug}/canonical-revalidations")
def list_canonical_revalidations(
    slug: str, source_id: str | None = None, limit: int = 100,
) -> list[dict]:
    """Read metadata-only source snapshots; these are not semantic fact checks."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    if source_id and not store.row(
        "SELECT 1 FROM sources WHERE id=? AND brand_id=?", (source_id, brand["id"]),
    ):
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        items = CanonicalSourceRevalidationStore(store.DATA_PATH).list(
            brand["id"], source_id=source_id, limit=limit,
        )
    except CanonicalRevalidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return [
        {**item, "evidence_scope": "canonical_metadata_only", "semantic_fact_check": False}
        for item in items
    ]


@app.post(
    "/api/brands/{slug}/sources/{source_id}/canonical-revalidations",
    status_code=202,
)
def request_canonical_revalidation(
    slug: str, source_id: str, input: CanonicalRevalidationRequest, request: Request,
) -> dict:
    """Queue a bounded public-page read without claiming facts are verified."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return enqueue_canonical_revalidation(
            brand_id=brand["id"], source_id=source_id,
            idempotency_key=input.idempotency_key, actor=request.state.principal,
        )
    except CanonicalRevalidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/brands/{slug}/sources/beehiiv/sync")
def sync_beehiiv_posts(slug: str, posts: list[BeehiivPostInput]) -> dict:
    """Ingest normalized Beehiiv post metadata from an authorized MCP agent."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    synced = []
    for post in posts:
        tags = ", ".join(tag["display"] for tag in post.content_tags if tag.get("display"))
        summary = post.subtitle or post.seo_description or f"Beehiiv {post.status} post"
        if post.subject_line:
            summary += f" · email subject: {post.subject_line}"
        if tags:
            summary += f" · tags: {tags}"
        synced.append(store.upsert_external_source(brand["id"], {
            "title": post.title,
            "url": post.editor_url,
            "source_type": "beehiiv",
            "body_summary": summary,
            "lifecycle_state": post.status,
            "scheduled_for": post.scheduled_at.isoformat() if post.scheduled_at else None,
            "external_source_id": post.id,
        }))
    return {"synced": len(synced), "sources": synced}


@app.post("/api/brands/{slug}/measurements/beehiiv/assisted")
def sync_assisted_beehiiv_measurements(
    slug: str, input: BeehiivMeasurementInput,
) -> dict:
    """Ingest aggregate stats fetched by an authorized Beehiiv helper; no PII."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return ingest_beehiiv_measurements(
            store.DATA_PATH, brand_id=brand["id"], posts=input.posts,
            publication_stats=input.publication_stats,
            observed_at=input.observed_at.isoformat(),
        )
    except BeehiivAssistedSyncError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/brands/{slug}/beehiiv-assisted-pulls")
def list_beehiiv_assisted_pulls(
    slug: str,
    status: Literal["pending", "claimed", "completed", "failed"] | None = None,
) -> list[dict]:
    """List durable read-only work requested for the assisted Beehiiv helper."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return beehiiv_assisted_pull_store.list(brand["id"], status=status)
    except BeehiivAssistedPullError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/brands/{slug}/beehiiv-assisted-pulls", status_code=202)
def request_beehiiv_assisted_pull(
    slug: str, input: AssistedBeehiivPullRequest, request: Request,
) -> dict:
    """Request metadata plus aggregate measurements; never provider writes or PII."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return beehiiv_assisted_pull_store.ensure(
            brand_id=brand["id"], connector_account_id=input.connector_account_id,
            scheduled_for=input.scheduled_for.isoformat(), actor=request.state.principal,
        )
    except BeehiivAssistedPullError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/beehiiv-assisted-pulls/{task_id}/claim")
def claim_beehiiv_assisted_pull(task_id: str, input: AssistedPullClaimInput) -> dict:
    try:
        return beehiiv_assisted_pull_store.claim(task_id, **input.model_dump())
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Assisted Beehiiv pull not found") from error
    except BeehiivAssistedPullError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/beehiiv-assisted-pulls/{task_id}/heartbeat")
def heartbeat_beehiiv_assisted_pull(task_id: str, input: AssistedPullHeartbeatInput) -> dict:
    try:
        return beehiiv_assisted_pull_store.heartbeat(task_id, **input.model_dump())
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Assisted Beehiiv pull not found") from error
    except BeehiivAssistedPullError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/beehiiv-assisted-pulls/{task_id}/receipt")
def submit_beehiiv_assisted_pull_receipt(
    task_id: str, input: AssistedPullReceiptInput,
) -> dict:
    """Reconcile browser-read metadata/aggregates; performs no provider action."""
    try:
        return beehiiv_assisted_pull_store.submit_receipt(
            task_id, **input.model_dump(mode="json"),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Assisted Beehiiv pull not found") from error
    except (BeehiivAssistedPullError, BeehiivAssistedSyncError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/beehiiv-assisted-pulls/{task_id}/failure")
def fail_beehiiv_assisted_pull(task_id: str, input: AssistedPullFailureInput) -> dict:
    try:
        return beehiiv_assisted_pull_store.record_failure(task_id, **input.model_dump())
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Assisted Beehiiv pull not found") from error
    except BeehiivAssistedPullError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/beehiiv-assisted-pulls/{task_id}/audit")
def get_beehiiv_assisted_pull_audit(task_id: str) -> list[dict]:
    try:
        return beehiiv_assisted_pull_store.audit(task_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Assisted Beehiiv pull not found") from error


@app.get("/api/brands/{slug}/calendar")
def get_content_calendar(slug: str) -> list[dict]:
    if not store.get_brand(slug):
        raise HTTPException(status_code=404, detail="Brand not found")
    return store.content_calendar(slug)


@app.get("/api/brands/{slug}/publishing-plan")
def get_publishing_plan(slug: str) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return publishing_planner.view(brand["id"])


@app.put("/api/brands/{slug}/publishing-plan/settings")
def update_publishing_plan_settings(
    slug: str, input: PublishingPlanSettingsInput, request: Request,
) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        payload = input.model_dump()
        return publishing_planner.update_settings(
            brand["id"], actor=request.state.principal, **payload,
        )
    except PublishingPlannerError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.put("/api/brands/{slug}/publishing-plan/items/{item_type}/{item_id}")
def update_publishing_plan_item(
    slug: str, item_type: str, item_id: str,
    input: PublishingPlanItemInput, request: Request,
) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return publishing_planner.update_item(
            brand["id"], item_type, item_id, actor=request.state.principal,
            **input.model_dump(mode="json"),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Planner item not found") from error
    except PublishingPlannerError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/brands/{slug}/publishing-plan/reflow/preview")
def preview_publishing_plan_reflow(
    slug: str, input: PublishingReflowPreviewInput, request: Request,
) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return publishing_planner.preview_reflow(
            brand["id"], start_at=input.start_at.isoformat(), actor=request.state.principal,
        )
    except PublishingPlannerError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/brands/{slug}/publishing-plan/reflow/commit")
def commit_publishing_plan_reflow(
    slug: str, input: PublishingReflowCommitInput, request: Request,
) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return publishing_planner.commit_reflow(
            brand["id"], input.preview_id, actor=request.state.principal,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Reflow preview not found") from error
    except PublishingPlannerError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/brands/{slug}/publishing-plan/reflow/undo")
def undo_publishing_plan_reflow(
    slug: str, input: PublishingReflowUndoInput, request: Request,
) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return publishing_planner.undo_reflow(
            brand["id"], input.commit_id, actor=request.state.principal,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Reflow commit not found") from error
    except PublishingPlannerError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/brands/{slug}/performance", status_code=201)
def record_performance(slug: str, input: PerformanceInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return store.insert("performance_records", {"brand_id": brand["id"], **input.model_dump(mode="json")})


@app.get("/api/brands/{slug}/performance")
def list_performance(slug: str) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return store.rows("""SELECT * FROM performance_records WHERE brand_id=? AND NOT EXISTS (
        SELECT 1 FROM fixture_quarantine_registry q
        WHERE q.table_name='performance_records'
          AND q.record_key_json=json_array(performance_records.id))
        ORDER BY observed_at DESC""", (brand["id"],))


@app.get("/api/brands/{slug}/performance-planning")
def get_performance_planning(
    slug: str, stage: str = "portfolio", channel: str = "x",
    topic: str | None = None, template_key: str | None = None,
    as_of: datetime | None = None,
) -> dict:
    """Explain the bounded measured-performance prior; never mutate planned work."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    scope = {"stage": stage, "channel": channel, "topic": topic, "template_key": template_key}
    return PerformancePlanningEngine(store.DATA_PATH).plan(
        brand["id"], scope, as_of=as_of,
    )


@app.get("/api/brands/{slug}/performance-planning/audit")
def list_performance_planning_audit(slug: str, limit: int = 50) -> list[dict]:
    """Read the tenant-scoped trail of performance evidence used during planning."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return PerformancePlanningEngine(store.DATA_PATH).audit(brand["id"], limit=limit)


@app.get("/api/brands/{slug}/connectors")
def list_connectors(slug: str) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return store.list_connector_accounts(brand["id"])


@app.post("/api/brands/{slug}/connectors", status_code=201)
def register_connector(slug: str, input: ConnectorAccountInput) -> dict:
    """Register non-secret connector metadata. Credentials live outside this payload."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    if input.connector_type == "rss":
        raise HTTPException(
            status_code=409,
            detail="Use the governed third-party RSS source onboarding endpoint.",
        )
    try:
        return store.upsert_connector_account(brand["id"], **input.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/brands/{slug}/connector-health-checks", status_code=202)
def trigger_connector_health_checks(
    slug: str, input: ConnectorHealthTriggerInput, request: Request,
) -> list[dict]:
    """Queue bounded read-only probes; this route never calls a provider inline."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return ConnectorHealthStore(store.DATA_PATH).trigger(
            brand["id"], actor=request.state.principal,
            connector_account_id=input.connector_account_id,
            timeout_seconds=input.timeout_seconds,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Connector account not found") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/brands/{slug}/connector-health-checks")
def list_connector_health_checks(slug: str, limit: int = 100) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return ConnectorHealthStore(store.DATA_PATH).list(brand["id"], limit=limit)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/connector-health-checks/{check_id}")
def get_connector_health_check(check_id: str) -> dict:
    try:
        return ConnectorHealthStore(store.DATA_PATH).get(check_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Connector health check not found") from error


@app.post("/api/brands/{slug}/third-party-sources", status_code=201)
def onboard_third_party_source(
    slug: str, input: ThirdPartyRssSourceInput, request: Request,
) -> dict:
    """Validate and enroll one public syndication feed without fetching it."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return third_party_source_service.onboard(
            brand["id"], publisher_name=input.publisher_name,
            feed_url=input.feed_url, homepage_url=input.homepage_url,
            feed_format=input.feed_format,
            polling_interval_seconds=input.polling_interval_seconds,
            actor=request.state.principal, reason=input.reason,
        )
    except (ThirdPartySourceError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/brands/{slug}/third-party-sources")
def list_third_party_sources(slug: str, include_disabled: bool = True) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return third_party_source_service.list(brand["id"], include_disabled=include_disabled)


@app.get("/api/third-party-sources/{connector_account_id}")
def get_third_party_source(connector_account_id: str) -> dict:
    try:
        return third_party_source_service.get(connector_account_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Third-party source not found") from error


@app.post("/api/third-party-sources/{connector_account_id}/enable")
def enable_third_party_source(
    connector_account_id: str, input: SourceControlInput, request: Request,
) -> dict:
    try:
        return third_party_source_service.set_enabled(
            connector_account_id, True, actor=request.state.principal, reason=input.reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Third-party source not found") from error
    except (ThirdPartySourceError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/third-party-sources/{connector_account_id}/disable")
def disable_third_party_source(
    connector_account_id: str, input: SourceControlInput, request: Request,
) -> dict:
    try:
        return third_party_source_service.set_enabled(
            connector_account_id, False, actor=request.state.principal, reason=input.reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Third-party source not found") from error
    except (ThirdPartySourceError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.put("/api/connections/{provider}/{account_id}")
def connect_account(
    provider: Literal["x", "beehiiv", "website"],
    account_id: str,
    input: ConnectionCredentialInput,
    request: Request,
) -> dict:
    """Connect or rotate one account without ever returning its credentials."""

    if any(not name.strip() or not value.get_secret_value() for name, value in input.credentials.items()):
        raise HTTPException(
            status_code=422, detail="Credential names and values must not be empty."
        )
    credentials = {name: value.get_secret_value() for name, value in input.credentials.items()}
    try:
        metadata = _credential_store().put(
            provider,
            account_id,
            input.display_name,
            credentials,
            required_scopes=input.required_scopes,
            granted_scopes=input.granted_scopes,
            actor=request.state.principal,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Invalid connector credential metadata.") from error
    if metadata.missing_scopes:
        account = store.row(
            "SELECT brand_id FROM connector_accounts WHERE connector_type=? AND account_key=?",
            (provider, account_id),
        )
        report_missing_permission(
            brand_id=account["brand_id"] if account else None,
            connector_account_id=metadata.id, provider=provider,
            operation="connect", missing_scopes=list(metadata.missing_scopes),
        )
    return metadata.as_dict()


@app.get("/api/connections")
def list_connections(provider: Literal["x", "beehiiv", "website"] | None = None) -> list[dict]:
    """List redacted operational metadata; secret access is intentionally absent."""

    return [metadata.as_dict() for metadata in _credential_store().list(provider)]


@app.get("/api/connections/{provider}/{account_id}")
def get_connection(provider: Literal["x", "beehiiv", "website"], account_id: str) -> dict:
    try:
        return _credential_store().get(provider, account_id).as_dict()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Connector account not found") from error


@app.get("/api/connections/{provider}/{account_id}/reconnect-status")
def get_connection_reconnect_status(
    provider: Literal["x", "beehiiv", "website"], account_id: str
) -> dict:
    try:
        metadata = _credential_store().get(provider, account_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Connector account not found") from error
    return {
        "provider": metadata.provider,
        "account_id": metadata.account_id,
        "status": metadata.status,
        "reconnect_required": metadata.reconnect_required,
        "has_credentials": metadata.has_credentials,
        "health_checked_at": metadata.health_checked_at,
        "last_error_code": metadata.last_error_code,
    }


@app.patch("/api/connections/{provider}/{account_id}/health")
def update_connection_health(
    provider: Literal["x", "beehiiv", "website"],
    account_id: str,
    input: ConnectionHealthInput,
    request: Request,
) -> dict:
    try:
        return _credential_store().set_health(
            provider,
            account_id,
            input.status,
            error_code=input.error_code,
            actor=request.state.principal,
        ).as_dict()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Connector account not found") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/connections/{provider}/{account_id}/disconnect")
def disconnect_account(
    provider: Literal["x", "beehiiv", "website"], account_id: str, request: Request
) -> dict:
    try:
        return _credential_store().disconnect(
            provider, account_id, actor=request.state.principal
        ).as_dict()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Connector account not found") from error


@app.get("/api/brands/{slug}/mission")
def get_active_mission(slug: str) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    mission = store.row(
        "SELECT id FROM missions WHERE brand_id=? AND status='active' ORDER BY starts_at DESC LIMIT 1",
        (brand["id"],),
    )
    if not mission:
        raise HTTPException(status_code=404, detail="No active mission")
    return store.mission_progress(mission["id"]) or {}


@app.get("/api/brands/{slug}/mission/morning-plan")
def get_morning_plan(slug: str) -> dict:
    progress = get_active_mission(slug)
    artifact = operating_plan_service.create_morning_plan(
        progress["id"], datetime.now().date().isoformat(), as_of=progress["as_of"],
    )
    return {**artifact["payload"], "artifact_id": artifact["id"], "persisted_at": artifact["updated_at"]}


@app.get("/api/brands/{slug}/mission/scorecard")
def get_mission_scorecard(slug: str) -> dict:
    progress = get_active_mission(slug)
    artifact = operating_plan_service.create_eod_scorecard(
        progress["id"], datetime.now().date().isoformat(), as_of=progress["as_of"],
    )
    return {**artifact["payload"], "artifact_id": artifact["id"], "persisted_at": artifact["updated_at"]}


@app.post("/api/brands/{slug}/tracked-url")
def create_tracked_url(slug: str, input: TrackedUrlInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        link = attribution_store.create_tracked_link(
            brand_id=brand["id"], campaign_id=input.campaign_id,
            artifact_id=input.artifact_id, cta_id=input.cta_id,
            source=input.source, medium=input.medium, destination=input.base_url,
            brand_slug=slug, actor=PREVIEW_PRINCIPAL,
        )
    except AttributionStoreError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {**link, "url": link["tracked_url"]}


@app.get("/api/brands/{slug}/tracked-links")
def list_tracked_links(slug: str) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return attribution_store.list_tracked_links(brand["id"])


@app.post("/api/brands/{slug}/mission/kpis", status_code=201)
def record_mission_kpi(slug: str, input: KpiSnapshotInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    mission = store.row(
        "SELECT id FROM missions WHERE brand_id=? AND status='active' ORDER BY starts_at DESC LIMIT 1",
        (brand["id"],),
    )
    if not mission:
        raise HTTPException(status_code=404, detail="No active mission")
    try:
        evidence = attribution_store.record_kpi_evidence(
            mission_id=mission["id"], metric=input.metric, value=input.value,
            observed_at=input.observed_at.isoformat(), source=input.source,
            connector_account_id=input.connector_account_id,
            connector_event_id=input.connector_event_id,
            human_manual=input.source == "manual",
            human_verified_by=PREVIEW_PRINCIPAL if input.source == "manual" else None,
            human_verification_note=input.verification_note or None,
            dimensions=input.dimensions,
        )
        canonical = attribution_store.promote_kpi_evidence(evidence["id"], actor=PREVIEW_PRINCIPAL)
    except AttributionStoreError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    store.record_kpi_snapshot(
        mission["id"], input.metric, canonical["value"], canonical["observed_at"],
        canonical["source"], dimensions={"evidence_record_id": evidence["id"]},
    )
    return canonical


@app.post("/api/posts/{post_id}/dispatch", status_code=201)
def create_post_dispatch(post_id: str, request: Request) -> dict:
    try:
        return dispatch_item_to_dict(create_dispatch_from_post(
            dispatcher, post_id, actor=request.state.principal, attribution=attribution_store,
        ))
    except CanonicalPostNotFound as error:
        raise HTTPException(status_code=404, detail="Canonical post not found") from error
    except (DispatchError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/dispatch-items/{item_id}/validation")
def validate_dispatch_item(item_id: str) -> dict:
    return _dispatch_operation(lambda: dispatcher.validate(item_id).as_dict())


@app.get("/api/brands/{slug}/execution-tasks")
def list_execution_tasks(
    slug: str,
    status: Literal["pending", "claimed", "needs_attention", "completed", "stale"] | None = None,
) -> list[dict]:
    """Materialize and list exact-approved browser/MCP delivery handoffs."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        execution_handoff_store.ensure_for_brand(brand["id"])
        return execution_handoff_store.list(brand["id"], status=status)
    except (ExecutionHandoffError, EditorialError, DispatchError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/brands/{slug}/execution-console")
def get_execution_console(slug: str) -> dict:
    """Read-only operator context for assisted delivery and aggregate pulls."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        execution_handoff_store.ensure_for_brand(brand["id"])
        tasks = execution_handoff_store.operator_view(brand["id"])
        agents = execution_agent_registry.list(brand["id"])
        now = datetime.now().astimezone()
        for agent in agents:
            heartbeat = agent.get("last_heartbeat_at")
            try:
                heartbeat_age = (now - datetime.fromisoformat(str(heartbeat))).total_seconds()
            except (TypeError, ValueError):
                heartbeat_age = None
            agent["heartbeat_age_seconds"] = heartbeat_age
            agent["ready_to_claim"] = bool(
                agent.get("enabled") and heartbeat_age is not None
                and 0 <= heartbeat_age <= 900
            )
        pulls = beehiiv_assisted_pull_store.list(brand["id"])
    except (
        ExecutionHandoffError, EditorialError, DispatchError, BeehiivAssistedPullError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {
        "schema_version": 1,
        "tasks": tasks,
        "execution_agents": agents,
        "beehiiv_aggregate_pulls": pulls,
        "safety": {
            "provider_write_performed": False,
            "claim_grants_approval": False,
            "receipt_reconciles_existing_result": True,
            "subscriber_data_allowed": False,
        },
    }


@app.get("/api/brands/{slug}/execution-controls")
def list_execution_controls(slug: str) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return {
        "controls": execution_handoff_store.controls(brand["id"]),
        "audit": execution_handoff_store.control_audit(brand["id"]),
    }


@app.put("/api/brands/{slug}/execution-controls/{provider}")
def set_execution_control(
    slug: str, provider: Literal["all", "beehiiv", "x"],
    input: ExecutionControlInput, request: Request,
) -> dict:
    """Authenticated operator kill switch for new assisted-execution claims."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return execution_handoff_store.set_control(
            brand["id"], provider, enabled=input.enabled, actor=request.state.principal,
        )
    except ExecutionHandoffError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/execution-tasks/{task_id}/claim")
def claim_execution_task(task_id: str, input: ExecutionClaimInput) -> dict:
    """Lease one approved action; claiming cannot grant or change approval."""
    try:
        return execution_handoff_store.claim(
            task_id, actor=input.actor, lease_seconds=input.lease_seconds,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Execution task not found") from error
    except (ExecutionHandoffError, EditorialError, DispatchError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.put("/api/execution-tasks/{task_id}/destination")
def bind_execution_task_destination(
    task_id: str, input: ExecutionDestinationInput, request: Request,
) -> dict:
    """Authenticated operator binding for an ambiguous assisted destination."""
    try:
        return execution_handoff_store.bind_destination_account(
            task_id, actor=request.state.principal, **input.model_dump(),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Execution task not found") from error
    except (ExecutionHandoffError, EditorialError, DispatchError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/execution-tasks/{task_id}/begin-external-action")
def begin_external_execution_action(task_id: str, input: ExternalActionStartInput) -> dict:
    """Mark the exact last safe boundary immediately before the provider click."""
    try:
        return execution_handoff_store.begin_external_action(task_id, **input.model_dump())
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Execution task not found") from error
    except (ExecutionHandoffError, EditorialError, DispatchError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/execution-tasks/{task_id}/receipt")
def submit_external_execution_receipt(task_id: str, input: ExternalReceiptInput) -> dict:
    """Reconcile a provider-UI result; this endpoint performs no provider write."""
    try:
        return execution_handoff_store.submit_receipt(task_id, **input.model_dump())
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Execution task not found") from error
    except (ExecutionHandoffError, EditorialError, DispatchError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/execution-tasks/{task_id}/beehiiv-private-draft-manifest")
def prepare_beehiiv_private_draft_manifest(
    task_id: str, input: BeehiivPrivateDraftManifestInput,
) -> dict:
    """Prepare one exact private-draft browser action; never schedules or sends."""
    try:
        return execution_handoff_store.beehiiv_private_draft_manifest(
            task_id, **input.model_dump(),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Execution task not found") from error
    except (ExecutionHandoffError, EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/execution-tasks/{task_id}/confirm-public-action")
def confirm_public_execution_action(
    task_id: str, input: PublicActionConfirmationInput, request: Request,
) -> dict:
    """Confirm one exact claimed X action now; performs no provider write.

    This authenticated human-facing boundary is intentionally not mirrored by
    the MCP execution tools.  A helper may claim and reconcile work, but cannot
    create the separate action-time confirmation through its MCP surface.
    """
    try:
        return execution_handoff_store.confirm_public_action(
            task_id, actor=request.state.principal, **input.model_dump(),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Execution task not found") from error
    except (ExecutionHandoffError, EditorialError, DispatchError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/execution-tasks/{task_id}/audit")
def get_execution_task_audit(task_id: str) -> list[dict]:
    try:
        return execution_handoff_store.audit(task_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Execution task not found") from error


@app.post("/api/brands/{slug}/learnings", status_code=201)
def propose_learning(slug: str, input: LearningInput, request: Request) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    supporting = input.evidence_for or [{"summary": input.evidence}]
    try:
        return BrandLearningEngine(store.DATA_PATH).propose(
            brand["id"], hypothesis=input.hypothesis, proposed_change=input.proposed_change,
            evidence_for=supporting, evidence_against=input.evidence_against,
            effect=input.effect, uncertainty=input.uncertainty, scope=input.scope,
            review_at=input.review_at, actor=request.state.principal,
        )
    except LearningError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/brands/{slug}/learnings")
def list_learnings(slug: str) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return BrandLearningEngine(store.DATA_PATH).list(brand["id"])


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


@app.get("/api/brands/{slug}/learnings/{learning_id}/audit")
def get_brand_learning_audit(slug: str, learning_id: str) -> list[dict]:
    _, engine = _brand_learning(slug, learning_id)
    return engine.audit(learning_id)


@app.post("/api/brands/{slug}/learnings/{learning_id}/{transition}")
def transition_brand_learning(
    slug: str, learning_id: str,
    transition: Literal["testing", "accept", "reject", "supersede", "disable", "enable"],
    request: Request, input: LearningLifecycleInput | None = None,
) -> dict:
    _, engine = _brand_learning(slug, learning_id)
    try:
        if transition in {"disable", "enable"}:
            return engine.set_active(
                learning_id, transition == "enable", actor=request.state.principal,
                reason=(input.reason if input and input.reason else f"{transition.title()}d by reviewer"),
            )
        target = {"accept": "accepted", "reject": "rejected", "supersede": "superseded"}.get(
            transition, transition,
        )
        return engine.transition(learning_id, target, actor=request.state.principal)
    except LearningError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/brands/{slug}/experiments", status_code=201)
def draft_experiment(slug: str, input: ExperimentDraftInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return experiment_store.draft(
            brand_id=brand["id"], campaign_id=input.campaign_id,
            hypothesis=input.hypothesis, metric=input.metric, guardrails=input.guardrails,
            measurement_windows=input.measurement_windows,
        )
    except ExperimentError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/brands/{slug}/experiments")
def list_experiments(slug: str) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return experiment_store.list(brand["id"])


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


@app.get("/api/brands/{slug}/experiments/{experiment_id}")
def get_brand_experiment(slug: str, experiment_id: str) -> dict:
    return _brand_experiment(slug, experiment_id)


@app.post("/api/brands/{slug}/experiments/{experiment_id}/recommendations", status_code=201)
def recommend_brand_experiment(slug: str, experiment_id: str) -> dict:
    _brand_experiment(slug, experiment_id)
    try:
        return experiment_store.recommend(experiment_id)
    except ExperimentError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/brands/{slug}/experiments/{experiment_id}/recommendations/{recommendation_id}/accept")
def accept_brand_experiment_recommendation(
    slug: str, experiment_id: str, recommendation_id: str, request: Request,
) -> dict:
    _brand_experiment(slug, experiment_id)
    try:
        return experiment_store.accept(
            experiment_id, recommendation_id, actor=request.state.principal,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ExperimentError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/experiments/{experiment_id}")
def get_experiment(experiment_id: str) -> dict:
    try:
        return experiment_store.get(experiment_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Experiment not found") from error


@app.get("/api/experiments/{experiment_id}/measurement-windows")
def list_experiment_measurement_windows(experiment_id: str) -> list[dict]:
    try:
        experiment_store.get(experiment_id)
        return experiment_store.list_windows(experiment_id=experiment_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Experiment not found") from error


@app.get("/api/experiment-measurement-windows/{window_id}")
def get_experiment_measurement_window(window_id: str) -> dict:
    try:
        return experiment_store.get_window(window_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Experiment measurement window not found") from error


@app.post("/api/experiments/{experiment_id}/recommendations", status_code=201)
def recommend_experiment_winner(experiment_id: str) -> dict:
    try:
        recommendation = experiment_store.recommend(experiment_id)
        if recommendation["status"] == "insufficient_evidence":
            experiment = experiment_store.get(experiment_id)
            report_stale_metric(
                brand_id=experiment["brand_id"], metric=experiment["metric"],
                evidence_window="experiment-review",
                related_ids=[experiment_id, *[variant["post_id"] for variant in experiment["variants"]]],
            )
        return recommendation
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Experiment not found") from error
    except ExperimentError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/experiments/{experiment_id}/recommendations/{recommendation_id}/accept")
def accept_experiment_winner(
    experiment_id: str, recommendation_id: str, request: Request,
) -> dict:
    try:
        return experiment_store.accept(
            experiment_id, recommendation_id, actor=request.state.principal,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Experiment not found") from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ExperimentError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/learnings/{learning_id}/{transition}")
def transition_learning(learning_id: str, transition: Literal["testing", "accept", "reject", "supersede", "disable", "enable"],
                        request: Request, input: LearningLifecycleInput | None = None) -> dict:
    if transition in {"disable", "enable"}:
        try:
            return BrandLearningEngine(store.DATA_PATH).set_active(
                learning_id, transition == "enable", actor=request.state.principal,
                reason=(input.reason if input and input.reason else f"{transition.title()}d by reviewer"),
            )
        except (KeyError, LearningError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
    target = {"accept": "accepted", "reject": "rejected", "supersede": "superseded"}.get(transition, transition)
    try:
        return BrandLearningEngine(store.DATA_PATH).transition(
            learning_id, target, actor=request.state.principal,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Learning not found") from error
    except LearningError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/brands/{slug}/product-feedback", status_code=201)
def report_product_feedback(slug: str, input: ProductFeedbackInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _feedback_operation(
        lambda: _feedback_store().report(brand_id=brand["id"], **input.model_dump())
    )


@app.get("/api/brands/{slug}/product-feedback")
def list_product_feedback(
    slug: str,
    status: Literal["open", "in_progress", "resolved", "verified"] | None = None,
    component: str | None = None,
    assignee: str | None = None,
    severity: Literal["low", "medium", "high", "critical"] | None = None,
) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _feedback_store().list(
        brand_id=brand["id"], status=status, component=component,
        assignee=assignee, severity=severity,
    )


def _brand_feedback(slug: str, feedback_id: str) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    item = _feedback_operation(lambda: _feedback_store().get(feedback_id))
    if item["brand_id"] != brand["id"]:
        raise HTTPException(status_code=404, detail="Product feedback not found")
    return item


@app.get("/api/brands/{slug}/product-feedback/{feedback_id}")
def get_brand_product_feedback(slug: str, feedback_id: str) -> dict:
    return _brand_feedback(slug, feedback_id)


@app.get("/api/brands/{slug}/product-feedback/{feedback_id}/history")
def get_brand_product_feedback_history(slug: str, feedback_id: str) -> list[dict]:
    _brand_feedback(slug, feedback_id)
    return _feedback_operation(lambda: _feedback_store().history(feedback_id))


@app.post("/api/brands/{slug}/product-feedback/{feedback_id}/comments", status_code=201)
def comment_on_brand_product_feedback(
    slug: str, feedback_id: str, input: FeedbackCommentInput, request: Request,
) -> dict:
    _brand_feedback(slug, feedback_id)
    return _feedback_operation(lambda: _feedback_store().comment(
        feedback_id, input.body, actor=request.state.principal,
    ))


@app.post("/api/brands/{slug}/product-feedback/{feedback_id}/start")
def start_brand_product_feedback(
    slug: str, feedback_id: str, input: FeedbackStartInput, request: Request,
) -> dict:
    _brand_feedback(slug, feedback_id)
    return _feedback_operation(lambda: _feedback_store().start(
        feedback_id, assignee=input.assignee, actor=request.state.principal,
        implementation_links=input.implementation_links,
        implementation_notes=input.implementation_notes,
    ))


@app.post("/api/brands/{slug}/product-feedback/{feedback_id}/resolve")
def resolve_brand_product_feedback(
    slug: str, feedback_id: str, input: FeedbackResolveInput, request: Request,
) -> dict:
    _brand_feedback(slug, feedback_id)
    return _feedback_operation(lambda: _feedback_store().resolve(
        feedback_id, actor=request.state.principal,
        resolution_evidence=input.resolution_evidence,
        implementation_links=input.implementation_links,
        implementation_notes=input.implementation_notes,
    ))


@app.post("/api/brands/{slug}/product-feedback/{feedback_id}/verify")
def verify_brand_product_feedback(
    slug: str, feedback_id: str, input: FeedbackVerifyInput, request: Request,
) -> dict:
    _brand_feedback(slug, feedback_id)
    return _feedback_operation(lambda: _feedback_store().verify(
        feedback_id, actor=request.state.principal, evidence=input.evidence,
    ))


@app.post("/api/brands/{slug}/product-feedback/{feedback_id}/reopen")
def reopen_brand_product_feedback(
    slug: str, feedback_id: str, input: FeedbackReopenInput, request: Request,
) -> dict:
    _brand_feedback(slug, feedback_id)
    return _feedback_operation(lambda: _feedback_store().reopen(
        feedback_id, actor=request.state.principal, reason=input.reason,
    ))


@app.post("/api/product-feedback/reconcile")
def reconcile_product_feedback(input: FeedbackReconcileInput, request: Request) -> list[dict]:
    return _feedback_operation(lambda: _feedback_store().reconcile_shipped_component(
        input.component, keywords=input.keywords,
        implementation_links=input.implementation_links, actor=request.state.principal,
    ))


@app.get("/api/product-feedback/{feedback_id}")
def get_product_feedback(feedback_id: str) -> dict:
    return _feedback_operation(lambda: _feedback_store().get(feedback_id))


@app.get("/api/product-feedback/{feedback_id}/history")
def get_product_feedback_history(feedback_id: str) -> list[dict]:
    return _feedback_operation(lambda: _feedback_store().history(feedback_id))


@app.post("/api/product-feedback/{feedback_id}/comments", status_code=201)
def comment_on_product_feedback(
    feedback_id: str, input: FeedbackCommentInput, request: Request,
) -> dict:
    return _feedback_operation(lambda: _feedback_store().comment(
        feedback_id, input.body, actor=request.state.principal,
    ))


@app.post("/api/product-feedback/{feedback_id}/start")
def start_product_feedback(feedback_id: str, input: FeedbackStartInput, request: Request) -> dict:
    return _feedback_operation(lambda: _feedback_store().start(
        feedback_id, assignee=input.assignee, actor=request.state.principal,
        implementation_links=input.implementation_links,
        implementation_notes=input.implementation_notes,
    ))


@app.post("/api/product-feedback/{feedback_id}/resolve")
def resolve_product_feedback(feedback_id: str, input: FeedbackResolveInput, request: Request) -> dict:
    return _feedback_operation(lambda: _feedback_store().resolve(
        feedback_id, actor=request.state.principal,
        resolution_evidence=input.resolution_evidence,
        implementation_links=input.implementation_links,
        implementation_notes=input.implementation_notes,
    ))


@app.post("/api/product-feedback/{feedback_id}/verify")
def verify_product_feedback(feedback_id: str, input: FeedbackVerifyInput, request: Request) -> dict:
    return _feedback_operation(lambda: _feedback_store().verify(
        feedback_id, actor=request.state.principal, evidence=input.evidence
    ))


@app.post("/api/product-feedback/{feedback_id}/reopen")
def reopen_product_feedback(feedback_id: str, input: FeedbackReopenInput, request: Request) -> dict:
    return _feedback_operation(lambda: _feedback_store().reopen(
        feedback_id, actor=request.state.principal, reason=input.reason
    ))


@app.post("/api/brands/{slug}/editorial-candidates", status_code=201)
def create_editorial_candidate(slug: str, input: EditorialCandidateInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return editorial_store.upsert_candidate(brand["id"], **input.model_dump())


@app.get("/api/brands/{slug}/editorial-candidates")
def list_editorial_candidates(slug: str, include_inactive: bool = False) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return editorial_store.list_candidates(brand["id"], status=None if include_inactive else "open")


@app.post("/api/editorial-candidates/{candidate_id}/abandon")
def abandon_editorial_candidate(
    candidate_id: str, input: EditorialCleanupInput, request: Request,
) -> dict:
    try:
        return editorial_store.abandon_candidate(
            candidate_id, actor=request.state.principal, reason=input.reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Editorial candidate not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/editorial-candidates/{candidate_id}/archive")
def archive_editorial_candidate(
    candidate_id: str, input: EditorialCleanupInput, request: Request,
) -> dict:
    try:
        return editorial_store.archive_candidate(
            candidate_id, actor=request.state.principal, reason=input.reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Editorial candidate not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/editorial-candidates/{candidate_id}/history")
def get_editorial_candidate_history(candidate_id: str) -> list[dict]:
    try:
        return editorial_store.list_candidate_history(candidate_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Editorial candidate not found") from error


@app.post("/api/brands/{slug}/newsletter-issues", status_code=201)
def create_newsletter_issue(slug: str, input: NewsletterIssueInput, request: Request) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return editorial_store.create_issue(
            brand["id"], input.content, created_by=request.state.principal,
            candidate_id=input.candidate_id,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Editorial candidate not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/brands/{slug}/newsletter-issues")
def list_newsletter_issues(slug: str, include_inactive: bool = False) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return [
        {**issue, "approval_scope": approval_snapshot_store.proposed_newsletter(issue)}
        for issue in editorial_store.list_issues(brand["id"], include_inactive=include_inactive)
    ]


@app.post("/api/newsletter-issues/{issue_id}/policy-review", status_code=201)
def record_newsletter_policy_review(
    issue_id: str, input: NewsletterPolicyReviewInput, request: Request,
) -> dict[str, Any]:
    try:
        result = brand_guideline_store.record_policy_review(
            issue_id=issue_id, revision=input.revision,
            reviewer=request.state.principal, checklist=input.checklist,
        )
        return {**result, "governance": editorial_store.get_issue(issue_id)["governance"]}
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except BrandGuidelineError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/newsletter-issues/{issue_id}/quick-hit-authorization", status_code=201)
def authorize_newsletter_quick_hit(
    issue_id: str, input: NewsletterQuickHitInput, request: Request,
) -> dict[str, Any]:
    try:
        result = brand_guideline_store.authorize_quick_hit(
            issue_id=issue_id, revision=input.revision,
            actor=request.state.principal, reason=input.reason,
        )
        return {**result, "governance": editorial_store.get_issue(issue_id)["governance"]}
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except BrandGuidelineError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/brands/{slug}/operator-workflow")
def get_operator_workflow(slug: str) -> dict:
    """Return the one authoritative, coherent nine-step operator journey."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    issues = [
        {**issue, "approval_scope": approval_snapshot_store.proposed_newsletter(issue)}
        for issue in editorial_store.list_issues(brand["id"])
    ]
    fact_checks = {
        issue["id"]: editorial_store.get_fact_check(
            issue["id"], int(issue["current_revision"]),
        )
        for issue in issues
        if issue.get("id") and issue.get("current_revision") is not None
    }
    performance = store.rows(
        """SELECT * FROM performance_records WHERE brand_id=? AND NOT EXISTS (
            SELECT 1 FROM fixture_quarantine_registry q
            WHERE q.table_name='performance_records'
              AND q.record_key_json=json_array(performance_records.id))
            ORDER BY observed_at DESC""",
        (brand["id"],),
    )
    # Beehiiv aggregate records intentionally have no canonical ``posts`` row:
    # the governed asset is the newsletter issue.  Derive campaign attribution
    # from the exact completed draft receipt and its matching provider metric
    # event so the operator loop can recognize real measurement evidence.
    performance.extend(store.rows(
        """SELECT DISTINCT t.campaign_id,NULL AS post_id,e.observed_at
           FROM connector_events e
           JOIN connector_accounts a ON a.id=e.connector_account_id
           JOIN execution_tasks t ON t.brand_id=a.brand_id
             AND t.provider='beehiiv' AND t.status='completed'
             AND t.receipt_external_id=json_extract(e.payload,'$.provider_external_id')
           WHERE a.brand_id=? AND e.event_type='metric_observed'
             AND t.campaign_id IS NOT NULL""",
        (brand["id"],),
    ))
    try:
        return build_operator_workflow(
            candidates=editorial_store.list_candidates(brand["id"]),
            issues=issues,
            fact_checks=fact_checks,
            packages=_distribution_packages().list(brand["id"]),
            handoffs=execution_handoff_store.operator_view(brand["id"]),
            connectors=store.list_connector_accounts(brand["id"]),
            performance=performance,
            assisted_pulls=beehiiv_assisted_pull_store.list(brand["id"]),
        )
    except (
        EditorialError, DistributionPackageError, ExecutionHandoffError,
        BeehiivAssistedPullError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/newsletter-issues/{issue_id}")
def get_newsletter_issue(issue_id: str) -> dict:
    try:
        return editorial_store.get_issue(issue_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error


def _distribution_packages() -> DistributionPackageStore:
    return DistributionPackageStore(store.DATA_PATH, editorial_store, dispatcher)


def _distribution_operation(operation):
    try:
        return operation()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Distribution package not found") from error
    except (DistributionPackageError, EditorialError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/newsletter-issues/{issue_id}/distribution-package", status_code=201)
def create_distribution_package(
    issue_id: str, input: DistributionPackageInput, request: Request,
) -> dict:
    """Create draft-only email, web, and X distribution artifacts."""
    try:
        issue = editorial_store.get_issue(issue_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    payload = input.model_dump(mode="json")
    return _distribution_operation(lambda: _distribution_packages().create(
        issue["brand_id"], issue_id, actor=request.state.principal, **payload,
    ))


@app.get("/api/distribution-packages/{package_id}")
def get_distribution_package(package_id: str) -> dict:
    return _distribution_operation(lambda: _distribution_packages().get(package_id))


@app.get("/api/brands/{slug}/distribution-packages")
def list_distribution_packages(slug: str) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _distribution_packages().list(brand["id"])


@app.get("/api/distribution-packages/{package_id}/measurement")
def get_distribution_measurement(package_id: str) -> dict:
    """Read additive totals and denominator-weighted campaign rates."""
    return _distribution_operation(lambda: _distribution_packages().measurement(package_id))


@app.get("/api/distribution-packages/{package_id}/membership-audit")
def get_distribution_membership_audit(package_id: str) -> list[dict]:
    return _distribution_operation(lambda: _distribution_packages().membership_audit(package_id))


@app.post("/api/distribution-packages/{package_id}/destination")
def bind_distribution_destination(
    package_id: str, input: DistributionDestinationInput, request: Request,
) -> dict:
    """Create governed tracked X URLs and fresh draft revisions; never approve or publish."""
    return _distribution_operation(lambda: _distribution_packages().bind_destination(
        package_id, input.destination_url, actor=request.state.principal,
    ))


@app.post("/api/distribution-memberships/{membership_id}/move")
def move_distribution_membership(
    membership_id: str, input: DistributionMembershipMoveInput, request: Request,
) -> dict:
    """Move attribution membership with an explicit operator and reason; never alter content."""
    return _distribution_operation(lambda: _distribution_packages().move_membership(
        membership_id, input.to_package_id, actor=request.state.principal,
        reason=input.reason,
    ))


@app.get("/api/campaign-templates")
def list_campaign_templates() -> list[dict]:
    """List immutable recipe contracts without hidden AI instructions."""
    return CampaignTemplateStore(store.DATA_PATH).list()


@app.get("/api/campaign-templates/{template_key}")
def get_campaign_template(template_key: str) -> dict:
    try:
        return CampaignTemplateStore(store.DATA_PATH).get(template_key)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Campaign template not found") from error


@app.post("/api/brands/{slug}/campaign-templates/{template_key}/preflight")
def preflight_campaign_template(
    slug: str, template_key: str, input: CampaignTemplatePreflightInput,
) -> dict:
    """Preview the asset graph and hard boundaries; this creates no campaign or content."""
    try:
        return CampaignTemplateStore(store.DATA_PATH).preflight(
            template_key, slug, input.answers, override_reason=input.override_reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Campaign template not found") from error
    except CampaignTemplateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/brands/{slug}/campaign-templates/{template_key}/instantiate", status_code=201)
def instantiate_campaign_template(
    slug: str, template_key: str, input: CampaignTemplateInstantiateInput, request: Request,
) -> dict:
    """Instantiate any recipe as an unapproved draft graph; never publish."""
    try:
        payload = input.model_dump(mode="json")
        return CampaignTemplateStore(store.DATA_PATH).instantiate(
            template_key, slug, actor=request.state.principal, **payload,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Campaign template not found") from error
    except (CampaignTemplateError, CampaignGraphError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/newsletter-issues/{issue_id}/revisions")
def list_newsletter_revisions(issue_id: str) -> list[dict]:
    try:
        editorial_store.get_issue(issue_id)
        return editorial_store.list_revisions(issue_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error


@app.post("/api/newsletter-issues/{issue_id}/abandon")
def abandon_newsletter_issue(
    issue_id: str, input: EditorialCleanupInput, request: Request,
) -> dict:
    try:
        return editorial_store.abandon_issue(
            issue_id, actor=request.state.principal, reason=input.reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/newsletter-issues/{issue_id}/archive")
def archive_newsletter_issue(
    issue_id: str, input: EditorialCleanupInput, request: Request,
) -> dict:
    try:
        return editorial_store.archive_issue(
            issue_id, actor=request.state.principal, reason=input.reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/newsletter-issues/{issue_id}/history")
def get_newsletter_issue_history(issue_id: str) -> list[dict]:
    try:
        return editorial_store.list_issue_history(issue_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error


@app.get("/api/newsletter-issues/{issue_id}/provider-reconciliations")
def get_newsletter_provider_reconciliations(issue_id: str) -> list[dict]:
    try:
        editorial_store.get_issue(issue_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    return beehiiv_lifecycle_projector.list(issue_id)


@app.get("/api/newsletter-issues/{issue_id}/fact-check")
def get_newsletter_fact_check(issue_id: str, revision: int | None = None) -> dict | None:
    try:
        return editorial_store.get_fact_check(issue_id, revision)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error


@app.patch("/api/newsletter-issues/{issue_id}")
def revise_newsletter_issue(
    issue_id: str, input: NewsletterRevisionInput, request: Request,
) -> dict:
    try:
        revised = editorial_store.revise_issue(
            issue_id, input.changes, created_by=request.state.principal,
            change_note=input.change_note,
        )
        approval_snapshot_store.invalidate_resource(
            issue_id, actor=request.state.principal,
            reason="newsletter revised; approval invalidated",
        )
        return revised
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/newsletter-issues/{issue_id}/transition")
def transition_newsletter_issue(issue_id: str, input: NewsletterTransitionInput) -> dict:
    try:
        return editorial_store.transition(issue_id, input.target)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except EditorialError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/newsletter-issues/{issue_id}/fact-check")
def fact_check_newsletter_issue(
    issue_id: str, input: NewsletterFactCheckInput, request: Request,
) -> dict:
    try:
        return editorial_store.record_fact_check(
            issue_id, expected_revision=input.revision, reviewer=request.state.principal,
            verdicts=[verdict.model_dump() for verdict in input.verdicts], notes=input.notes,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/newsletter-issues/{issue_id}/approve")
def approve_newsletter_issue(issue_id: str, input: NewsletterApprovalInput, request: Request) -> dict:
    try:
        approved, snapshot = approval_snapshot_store.approve_newsletter(
            issue_id, approver=request.state.principal, revision=input.revision,
            review_token=input.review_token,
        )
        return {**approved, "approval_snapshot": snapshot}
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/newsletter-issues/{issue_id}/reject")
def reject_newsletter_issue(
    issue_id: str, input: NewsletterRejectionInput, request: Request,
) -> dict:
    """Reject one displayed revision and return a fresh working copy for changes."""
    try:
        rejected = editorial_store.reject_issue(
            issue_id, actor=request.state.principal, reason=input.reason,
            expected_revision=input.revision,
        )
        approval_snapshot_store.invalidate_resource(
            issue_id, actor=request.state.principal,
            reason=f"newsletter revision {input.revision} rejected: {input.reason}",
        )
        return rejected
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/newsletter-issues/{issue_id}/export-preview")
def prepare_newsletter_export(issue_id: str) -> dict:
    try:
        return editorial_store.prepare_export(issue_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except EditorialError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/newsletter-issues/{issue_id}/export-draft", status_code=202)
def enqueue_newsletter_draft_export(
    issue_id: str, input: NewsletterExportInput
) -> dict:
    """Queue exact approved content for Beehiiv draft creation, never publication."""

    try:
        # Editorial governance is the first boundary.  Reject stale or
        # unapproved content before consulting connector or secret state.
        editorial_store.prepare_export(issue_id)
        issue = editorial_store.get_issue(issue_id)
        account = resolve_beehiiv_export_account(
            issue["brand_id"], input.connector_account_id,
            credentials=_credential_store(),
        )
        return enqueue_newsletter_export(
            editorial_store,
            issue_id,
            brand_id=issue["brand_id"],
            connector_account_id=account["id"],
            run_after=input.run_after.isoformat() if input.run_after else None,
            priority=input.priority,
            max_attempts=input.max_attempts,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except (NewsletterExportJobError, EditorialError) as error:
        try:
            issue = editorial_store.get_issue(issue_id)
            report_approval_dead_end(
                brand_id=issue["brand_id"], resource_id=issue_id,
                resource_type="newsletter_issue", operation="export-draft",
            )
        except Exception:
            pass
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/newsletter-issues/{issue_id}/export-jobs")
def get_newsletter_issue_export_jobs(issue_id: str) -> list[dict]:
    try:
        editorial_store.get_issue(issue_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    return list_newsletter_export_jobs(issue_id)


@app.get("/api/newsletter-export-jobs/{job_id}")
def get_newsletter_draft_export_job(job_id: str) -> dict:
    try:
        return get_newsletter_export_job(job_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter export job not found") from error


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


@app.post("/api/brands/{slug}/operator-proposals/preview", status_code=201)
def preview_operator_proposal(
    slug: str, input: OperatorProposalPreviewInput, request: Request,
) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _operator_proposal_operation(lambda: operator_proposal_store.preview(
        brand_id=brand["id"], actor=request.state.principal, **input.model_dump(),
    ))


@app.get("/api/brands/{slug}/operator-proposals/{proposal_id}")
def get_operator_proposal(slug: str, proposal_id: str) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _operator_proposal_operation(
        lambda: operator_proposal_store.get(proposal_id, brand_id=brand["id"])
    )


@app.post("/api/brands/{slug}/operator-proposals/{proposal_id}/confirm")
def confirm_operator_proposal(
    slug: str, proposal_id: str, input: OperatorProposalConfirmInput, request: Request,
) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _operator_proposal_operation(lambda: operator_proposal_store.confirm(
        proposal_id, brand_id=brand["id"], actor=request.state.principal,
    ))


@app.post("/api/brands/{slug}/campaigns", status_code=201)
def create_campaign(slug: str, input: CampaignInput, request: Request) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _campaign_graph_operation(lambda: _campaign_graph().create_campaign(
        brand["id"], input.name, input.objective, source_id=input.source_id,
        actor=request.state.principal,
    ))


@app.get("/api/campaigns/{campaign_id}/graph")
def get_campaign_graph(campaign_id: str) -> dict:
    return _campaign_graph_operation(lambda: _campaign_graph().get(campaign_id))


@app.get("/api/brands/{slug}/campaign-graphs")
def list_campaign_graphs(slug: str) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _campaign_graph().list(brand["id"])


@app.post("/api/campaigns/{campaign_id}/memberships", status_code=201)
def attach_campaign_membership(
    campaign_id: str, input: CampaignMembershipAttachInput, request: Request,
) -> dict:
    payload = input.model_dump(mode="json")
    reason = payload.pop("reason")
    return _campaign_graph_operation(lambda: _campaign_graph().attach(
        campaign_id, actor=request.state.principal, reason=reason, **payload,
    ))


@app.post("/api/campaign-memberships/{membership_id}/detach")
def detach_campaign_membership(
    membership_id: str, input: CampaignReasonInput, request: Request,
) -> dict:
    return _campaign_graph_operation(lambda: _campaign_graph().detach(
        membership_id, actor=request.state.principal, reason=input.reason,
    ))


@app.post("/api/campaign-memberships/{membership_id}/anchor")
def set_campaign_anchor(
    membership_id: str, input: CampaignReasonInput, request: Request,
) -> dict:
    return _campaign_graph_operation(lambda: _campaign_graph().set_anchor(
        membership_id, actor=request.state.principal, reason=input.reason,
    ))


@app.post("/api/campaigns/{campaign_id}/memberships/reorder")
def reorder_campaign_memberships(
    campaign_id: str, input: CampaignReorderInput, request: Request,
) -> dict:
    return _campaign_graph_operation(lambda: _campaign_graph().reorder(
        campaign_id, input.membership_ids, actor=request.state.principal, reason=input.reason,
    ))


@app.post("/api/campaigns/{campaign_id}/relationships", status_code=201)
def add_campaign_relationship(
    campaign_id: str, input: CampaignRelationshipInput, request: Request,
) -> dict:
    payload = input.model_dump()
    reason = payload.pop("reason")
    return _campaign_graph_operation(lambda: _campaign_graph().add_relationship(
        campaign_id, actor=request.state.principal, reason=reason, **payload,
    ))


@app.post("/api/campaigns/{campaign_id}/flights", status_code=201)
def add_campaign_flight(
    campaign_id: str, input: CampaignFlightInput, request: Request,
) -> dict:
    payload = input.model_dump(mode="json")
    reason = payload.pop("reason")
    return _campaign_graph_operation(lambda: _campaign_graph().add_flight(
        campaign_id, actor=request.state.principal, reason=reason, **payload,
    ))


@app.post("/api/campaign-memberships/{membership_id}/metrics", status_code=201)
def record_campaign_asset_metric(membership_id: str, input: CampaignMetricInput) -> dict:
    return _campaign_graph_operation(lambda: _campaign_graph().record_metric(
        membership_id, **input.model_dump(mode="json"),
    ))


@app.get("/api/campaigns/{campaign_id}/measurement")
def get_campaign_measurement(campaign_id: str) -> dict:
    return _campaign_graph_operation(lambda: _campaign_graph().measurement(campaign_id))


@app.get("/api/campaigns/{campaign_id}/graph-audit")
def get_campaign_graph_audit(campaign_id: str) -> list[dict]:
    return _campaign_graph_operation(lambda: _campaign_graph().audit(campaign_id))


@app.get("/api/campaigns/{campaign_id}/posts")
def list_posts(campaign_id: str) -> list[dict]:
    return store.rows(
        """SELECT p.*,c.brand_id,cpp.candidate_id,cpp.created_by
           FROM posts p JOIN campaigns c ON c.id=p.campaign_id
           LEFT JOIN campaign_post_provenance cpp ON cpp.post_id=p.id
           WHERE p.campaign_id=? ORDER BY p.created_at""",
        (campaign_id,),
    )


@app.post("/api/campaigns/{campaign_id}/posts", status_code=201)
def draft_post(campaign_id: str, input: PostInput, request: Request) -> dict:
    try:
        return store.create_campaign_post(
            campaign_id, channel=input.channel, body=input.body,
            scheduled_for=input.scheduled_for.isoformat() if input.scheduled_for else None,
            actor=request.state.principal,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Campaign not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.patch("/api/posts/{post_id}")
def edit_post(post_id: str, input: PostEditInput, request: Request) -> dict:
    try:
        return store.edit_campaign_post(
            post_id, body=input.body, actor=request.state.principal,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Campaign post not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/posts/{post_id}/audit")
def get_post_audit(post_id: str) -> list[dict]:
    try:
        return store.campaign_post_audit(post_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Campaign post not found") from error


@app.post(
    "/api/brands/{slug}/editorial-candidates/{candidate_id}/campaign-post",
    status_code=201,
)
def promote_candidate_to_campaign_post(
    slug: str, candidate_id: str, input: CandidateCampaignPostInput, request: Request,
) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    # Initialize the campaign audit schema before the single promotion transaction.
    _campaign_graph()
    try:
        return store.promote_candidate_to_campaign_post(
            brand["id"], candidate_id, channel=input.channel, body=input.body,
            actor=request.state.principal, campaign_id=input.campaign_id,
            campaign_name=input.campaign_name, objective=input.objective,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Editorial candidate not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/posts/{post_id}/approve")
def approve_post(post_id: str, request: Request) -> dict:
    raise HTTPException(
        status_code=410,
        detail=("Legacy post approval is retired. Use the canonical dispatch-item "
                "exact-review approval flow."),
    )


@app.post("/api/posts/{post_id}/schedule")
def schedule_post(post_id: str, scheduled_for: datetime) -> dict:
    raise HTTPException(
        status_code=410,
        detail=("Legacy post scheduling is retired. Use an approved canonical dispatch "
                "handoff with immutable review evidence."),
    )


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


@app.get("/api/brands/{slug}/engagement")
def list_engagement_opportunities(
    slug: str, state: str | None = None, limit: int = 100
) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _engagement_operation(
        lambda: engagement_store.list(brand_id=brand["id"], state=state, limit=limit)
    )


@app.get("/api/brands/{slug}/engagement/{opportunity_id}")
def get_engagement_opportunity(slug: str, opportunity_id: str) -> dict:
    return _engagement_operation(lambda: _brand_engagement(slug, opportunity_id)[1])


@app.get("/api/brands/{slug}/engagement/{opportunity_id}/history")
def get_engagement_history(slug: str, opportunity_id: str) -> list[dict]:
    def operation():
        _brand_engagement(slug, opportunity_id)
        return engagement_store.history(opportunity_id)
    return _engagement_operation(operation)


@app.post("/api/brands/{slug}/engagement/{opportunity_id}/draft-action")
def draft_engagement_action(
    slug: str, opportunity_id: str, input: EngagementDraftInput, request: Request,
) -> dict:
    def operation():
        _brand_engagement(slug, opportunity_id)
        opportunity, dispatch = engagement_store.draft_action(
            opportunity_id, input.action_type, dispatcher,
            actor=request.state.principal, text=input.text,
        )
        return {"opportunity": opportunity, "dispatch_item": dispatch_item_to_dict(dispatch)}
    return _engagement_operation(operation)


@app.post("/api/brands/{slug}/engagement/{opportunity_id}/submit-action")
def submit_engagement_action(
    slug: str, opportunity_id: str, input: EngagementActorInput, request: Request,
) -> dict:
    def operation():
        _brand_engagement(slug, opportunity_id)
        opportunity, dispatch = engagement_store.submit_action_for_approval(
            opportunity_id, dispatcher, actor=request.state.principal
        )
        return {"opportunity": opportunity, "dispatch_item": dispatch_item_to_dict(dispatch)}
    return _engagement_operation(operation)


@app.post("/api/brands/{slug}/engagement/{opportunity_id}/dismiss")
def dismiss_engagement_opportunity(
    slug: str, opportunity_id: str, input: EngagementDismissInput, request: Request,
) -> dict:
    def operation():
        _brand_engagement(slug, opportunity_id)
        return engagement_store.dismiss(
            opportunity_id, actor=request.state.principal, reason=input.reason
        )
    return _engagement_operation(operation)


def _dispatch_operation(operation):
    try:
        return operation()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Dispatch item not found") from error
    except (DispatchError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/brands/{slug}/dispatch-items")
def list_dispatch_items(slug: str, status: Lifecycle | None = None) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return [
        {**dispatch_item_to_dict(item), "approval_scope": approval_snapshot_store.proposed_dispatch(item)}
        for item in dispatch_store.list_items(brand_id=brand["id"], status=status)
    ]


@app.get("/api/brands/{slug}/approval-snapshots")
def list_approval_snapshots(slug: str, active_only: bool = False) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return approval_snapshot_store.list(brand["id"], active_only=active_only)


@app.get("/api/approval-snapshots/{snapshot_id}")
def get_approval_snapshot(snapshot_id: str) -> dict:
    try:
        return approval_snapshot_store.get(snapshot_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Approval snapshot not found") from error


@app.post("/api/brands/{slug}/dispatch-items", status_code=201)
def create_dispatch_item(slug: str, input: DispatchCreateInput, request: Request) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return dispatch_item_to_dict(dispatcher.create(
        input.connector, input.payload, brand_id=brand["id"], actor=request.state.principal,
    ))


@app.get("/api/dispatch-items/{item_id}")
def get_dispatch_item(item_id: str) -> dict:
    return _dispatch_operation(lambda: dispatch_item_to_dict(dispatch_store.get(item_id)))


@app.get("/api/dispatch-items/{item_id}/revisions/{revision}")
def get_dispatch_revision(item_id: str, revision: int) -> dict:
    try:
        return dispatch_store.get_revision(item_id, revision)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Dispatch revision not found") from error


@app.patch("/api/dispatch-items/{item_id}")
def edit_dispatch_item(item_id: str, input: DispatchEditInput, request: Request) -> dict:
    def operation():
        edited = dispatcher.edit(item_id, input.payload, actor=request.state.principal)
        approval_snapshot_store.invalidate_resource(
            item_id, actor=request.state.principal, reason="dispatch edited; approval invalidated",
        )
        return dispatch_item_to_dict(edited)
    return _dispatch_operation(operation)


@app.post("/api/dispatch-items/{item_id}/submit")
def submit_dispatch_item(item_id: str, input: DispatchActorInput, request: Request) -> dict:
    return _dispatch_operation(lambda: dispatch_item_to_dict(
        dispatcher.submit_for_approval(item_id, actor=request.state.principal)
    ))


@app.post("/api/dispatch-items/{item_id}/approve")
def approve_dispatch_item(item_id: str, input: DispatchApprovalInput, request: Request) -> dict:
    def operation():
        approved, snapshot = approval_snapshot_store.approve_dispatch(
            item_id, revision=input.revision, review_token=input.review_token,
            approver=request.state.principal,
        )
        return {
            **dispatch_item_to_dict(approved),
            "approval_snapshot": snapshot,
        }
    return _dispatch_operation(operation)


@app.post("/api/brands/{slug}/dispatch-items/approve-batch")
def approve_dispatch_batch(slug: str, input: DispatchBatchApprovalInput, request: Request) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")

    def operation():
        member_ids = {member.id for member in input.items}
        if len(member_ids) != len(input.items):
            raise ValueError("batch item ids must be unique")
        for item_id in member_ids:
            item = dispatch_store.get(item_id)
            if item.brand_id != brand["id"]:
                raise KeyError(item_id)
        batch_id = input.batch_id or secrets.token_hex(16)
        approved = approval_snapshot_store.approve_dispatch_batch(
            [member.model_dump() for member in input.items],
            approver=request.state.principal, batch_id=batch_id,
        )
        return [{
            **dispatch_item_to_dict(item),
            "approval_snapshot": snapshot,
        } for item, snapshot in approved]

    return _dispatch_operation(operation)


@app.post("/api/dispatch-items/{item_id}/reject")
def reject_dispatch_item(item_id: str, input: DispatchRevisionInput, request: Request) -> dict:
    return _dispatch_operation(lambda: dispatch_item_to_dict(dispatcher.reject(
        item_id, revision=input.revision, actor=request.state.principal
    )))


@app.post("/api/dispatch-items/{item_id}/queue")
def queue_dispatch_item(item_id: str) -> dict:
    """Queue an already-approved item under a server-controlled system identity."""
    try:
        return dispatch_item_to_dict(dispatcher.queue(item_id, actor=SYSTEM_QUEUE_ACTOR))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Dispatch item not found") from error
    except (DispatchError, ValueError) as error:
        try:
            item = dispatch_store.get(item_id)
            report_approval_dead_end(
                brand_id=item.brand_id, resource_id=item_id,
                resource_type="dispatch_item", operation="queue",
            )
        except Exception:
            pass
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/dispatch-items/{item_id}/audit")
def get_dispatch_audit(item_id: str) -> list[dict]:
    return _dispatch_operation(lambda: [audit_event_to_dict(event) for event in dispatch_store.list_audit(item_id)])


app.mount("/mcp", hosted_mcp_application)
