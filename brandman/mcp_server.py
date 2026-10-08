from __future__ import annotations

import os

from . import seed_packs, store
from .beehiiv_runtime import (
    enqueue_newsletter_export,
    get_newsletter_export_job,
    list_newsletter_export_jobs,
    resolve_beehiiv_export_account,
)
from .beehiiv_assisted_sync import ingest_beehiiv_measurements
from .content_dispatch import CanonicalPostNotFound, create_dispatch_from_post
from .dispatch import DispatchError, Lifecycle, audit_event_to_dict, dispatch_item_to_dict
from .distribution_package import DistributionPackageError, DistributionPackageStore
from .campaign_graph import CampaignGraphError, CampaignGraphStore
from .campaign_templates import CampaignTemplateError, CampaignTemplateStore
from .canonical_revalidation import CanonicalSourceRevalidationStore
from .credentials import CredentialStore
from .connector_health import ConnectorHealthStore
from .feedback import FeedbackStore
from .learning_engine import BrandLearningEngine
from .performance_planning import PerformancePlanningEngine
from .engagement import EngagementError
from .main import (
    CampaignInput, PostInput, approval_snapshot_store, attribution_store, dispatch_store, dispatcher,
    editorial_store, engagement_store, experiment_store, execution_handoff_store,
    beehiiv_assisted_pull_store, beehiiv_lifecycle_projector,
    get_mission_scorecard as build_current_scorecard,
    get_morning_plan as build_current_morning_plan, initialize_application_services,
    third_party_source_service,
)
from .readiness import LiveReadinessService
from .provider_usage import ProviderUsageLedger
from .execution_agents import ExecutionAgentRegistry

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# The stdio transport ignores this path.  The hosted application mounts this
# ASGI app at ``/mcp``, so its inner Streamable HTTP route must be root-relative.
mcp = FastMCP(
    "Brand OS",
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            *[h.strip() for h in os.environ.get("BRANDMAN_ALLOWED_HOSTS", "").split(",") if h.strip()],
            "localhost", "testserver",
        ],
    ),
)


def _initialize_runtime() -> None:
    """Explicit MCP runtime boundary; module import itself remains read-only."""
    initialize_application_services()


@mcp.tool()
def list_brands() -> list[dict]:
    """List available brand workspaces."""
    _initialize_runtime()
    return store.rows("SELECT slug, name, mission, approval_policy FROM brands ORDER BY name")


@mcp.tool()
def get_live_readiness(slug: str) -> dict:
    """Read code readiness, account connectivity, scopes, schedules, and mission health without secrets or provider writes."""
    _initialize_runtime()
    try:
        return LiveReadinessService(store.DATA_PATH).inspect(slug)
    except KeyError as error:
        raise ValueError(f"Unknown brand: {slug}") from error


@mcp.tool()
def get_provider_api_usage(slug: str) -> dict:
    """Read payload-free provider API usage and operator-configured cost estimates."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return ProviderUsageLedger(store.DATA_PATH).report(brand["id"])


@mcp.tool()
def heartbeat_execution_agent(slug: str, agent_id: str) -> dict:
    """Record liveness for a preconfigured assisted execution agent; this grants no approval."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    try:
        return ExecutionAgentRegistry(store.DATA_PATH).heartbeat(brand["id"], agent_id)
    except KeyError as error:
        raise ValueError("Unknown or disabled execution agent") from error


@mcp.tool()
def get_active_mission(slug: str) -> dict:
    """Read current goals, progress, remaining time, and required pace."""
    _initialize_runtime()
    if seed_packs.growth_mission(slug):
        return store.ensure_growth_mission(slug)
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    mission = store.row(
        "SELECT id FROM missions WHERE brand_id=? AND status='active' ORDER BY starts_at DESC LIMIT 1",
        (brand["id"],),
    )
    if not mission:
        raise ValueError(f"No active mission for {slug}")
    return store.mission_progress(mission["id"]) or {}


@mcp.tool()
def get_morning_plan(slug: str) -> dict:
    """Persist today's goal trajectory and prioritized operating actions."""
    return build_current_morning_plan(slug)


@mcp.tool()
def get_end_of_day_scorecard(slug: str) -> dict:
    """Persist the current KPI, execution, failure, and approval scorecard."""
    return build_current_scorecard(slug)


@mcp.tool()
def create_tracked_url(slug: str, base_url: str, source: str, medium: str,
                       campaign_id: str, artifact_id: str, cta_id: str) -> dict:
    """Create a canonical Brand OS attribution URL for an exact campaign artifact and CTA."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    link = attribution_store.create_tracked_link(
        brand_id=brand["id"], campaign_id=campaign_id, artifact_id=artifact_id,
        cta_id=cta_id, source=source, medium=medium, destination=base_url,
        brand_slug=slug, actor="mcp-agent",
    )
    return {**link, "url": link["tracked_url"]}


@mcp.tool()
def record_mission_kpi(slug: str, metric: str, value: float, observed_at: str,
                       source: str, connector_account_id: str,
                       connector_event_id: str, dimensions: dict | None = None) -> dict:
    """Promote a KPI only when backed by an exact persisted connector event."""
    _initialize_runtime()
    if seed_packs.growth_mission(slug):
        active = store.ensure_growth_mission(slug)
    else:
        brand = store.get_brand(slug)
        if not brand:
            raise ValueError(f"Unknown brand: {slug}")
        mission = store.row(
            "SELECT id FROM missions WHERE brand_id=? AND status='active' ORDER BY starts_at DESC LIMIT 1",
            (brand["id"],),
        )
        if not mission:
            raise ValueError(f"No active mission for {slug}")
        active = mission
    allowed = {goal["metric"] for goal in store.rows(
        "SELECT metric FROM mission_goals WHERE mission_id=?", (active["id"],)
    )}
    if metric not in allowed:
        raise ValueError(f"Unknown mission metric: {metric}")
    evidence = attribution_store.record_kpi_evidence(
        mission_id=active["id"], metric=metric, value=value,
        observed_at=observed_at, source=source,
        connector_account_id=connector_account_id,
        connector_event_id=connector_event_id, dimensions=dimensions,
    )
    canonical = attribution_store.promote_kpi_evidence(evidence["id"], actor="connector-evidence")
    store.record_kpi_snapshot(
        active["id"], metric, canonical["value"], canonical["observed_at"],
        canonical["source"], dimensions={"evidence_record_id": evidence["id"]},
    )
    return canonical


@mcp.tool()
def list_connector_accounts(slug: str) -> list[dict]:
    """List non-secret connector status, scopes, and capabilities for a brand."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return store.list_connector_accounts(brand["id"])


@mcp.tool()
def list_connector_health_checks(slug: str, limit: int = 100) -> list[dict]:
    """Read durable redacted connector probe status; health checks can only be triggered through authenticated REST."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return ConnectorHealthStore(store.DATA_PATH).list(brand["id"], limit=limit)


@mcp.tool()
def register_connector_account(slug: str, connector_type: str, account_key: str,
                               display_name: str, status: str = "disconnected",
                               scopes: list[str] | None = None,
                               capabilities: list[str] | None = None,
                               configuration: dict | None = None) -> dict:
    """Register connector metadata only. Never pass tokens or credentials here."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    if connector_type == "rss":
        raise ValueError("RSS sources must be onboarded through authenticated Brand OS REST")
    return store.upsert_connector_account(
        brand["id"], connector_type, account_key, display_name, status=status,
        scopes=scopes, capabilities=capabilities, configuration=configuration,
    )


@mcp.tool()
def list_third_party_sources(slug: str, include_disabled: bool = True) -> list[dict]:
    """Read governed public RSS/Atom source status and polling configuration."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return third_party_source_service.list(
        brand["id"], include_disabled=include_disabled,
    )


@mcp.tool()
def list_canonical_revalidations(
    slug: str, source_id: str | None = None, limit: int = 100,
) -> list[dict]:
    """Read canonical-page metadata evidence. It is never a semantic fact check."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    if source_id and not store.row(
        "SELECT 1 FROM sources WHERE id=? AND brand_id=?", (source_id, brand["id"]),
    ):
        raise ValueError("Unknown source for this brand")
    return [
        {**item, "evidence_scope": "canonical_metadata_only", "semantic_fact_check": False}
        for item in CanonicalSourceRevalidationStore(store.DATA_PATH).list(
            brand["id"], source_id=source_id, limit=limit,
        )
    ]


@mcp.tool()
def get_third_party_source_status(connector_account_id: str) -> dict:
    """Read one source's safe configuration, schedule, run state, and audit history."""
    return third_party_source_service.get(connector_account_id)


@mcp.tool()
def enqueue_connector_job(slug: str, job_type: str, idempotency_key: str,
                          payload: dict, connector_account_id: str | None = None,
                          run_after: str | None = None) -> dict:
    """Durably enqueue a connector sync or measurement job exactly once."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return store.enqueue_job(
        job_type, idempotency_key, payload, brand_id=brand["id"],
        connector_account_id=connector_account_id, run_after=run_after,
    )


@mcp.tool()
def get_brand_context(slug: str) -> dict:
    """Fetch canonical brand voice, policy, personas, sources, and campaigns."""
    _initialize_runtime()
    context = store.brand_context(slug)
    if not context:
        raise ValueError(f"Unknown brand: {slug}")
    return context


@mcp.tool()
def list_approval_snapshots(slug: str, active_only: bool = False) -> list[dict]:
    """Read immutable exact-material human approval evidence; MCP cannot create approvals."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return approval_snapshot_store.list(brand["id"], active_only=active_only)


@mcp.tool()
def get_dispatch_revision(item_id: str, revision: int) -> dict:
    """Read immutable safe material for one historical X/engagement revision."""
    return dispatch_store.get_revision(item_id, revision)


@mcp.tool()
def get_content_calendar(slug: str) -> list[dict]:
    """Read one brand's canonical calendar: imported content plus governed social drafts."""
    _initialize_runtime()
    if not store.get_brand(slug):
        raise ValueError(f"Unknown brand: {slug}")
    return store.content_calendar(slug)


@mcp.tool()
def sync_beehiiv_posts(slug: str, posts: list[dict]) -> dict:
    """Upsert normalized Beehiiv post metadata fetched by an authorized Beehiiv MCP agent."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    synced = []
    for post in posts:
        tags = ", ".join(tag.get("display", "") for tag in post.get("content_tags", []) if tag.get("display"))
        summary = post.get("subtitle") or post.get("seo_description") or f"Beehiiv {post['status']} post"
        if post.get("subject_line"):
            summary += f" · email subject: {post['subject_line']}"
        if tags:
            summary += f" · tags: {tags}"
        synced.append(store.upsert_external_source(brand["id"], {
            "title": post["title"], "url": post.get("editor_url"), "source_type": "beehiiv",
            "body_summary": summary,
            "lifecycle_state": post["status"], "scheduled_for": post.get("scheduled_at"),
            "external_source_id": post["id"],
        }))
    return {"synced": len(synced), "sources": synced}


@mcp.tool()
def sync_beehiiv_measurements(
    slug: str, observed_at: str, posts: list[dict],
    publication_stats: dict | None = None,
) -> dict:
    """Persist aggregate Beehiiv stats fetched by MCP; subscriber PII is discarded."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return ingest_beehiiv_measurements(
        store.DATA_PATH, brand_id=brand["id"], posts=posts,
        publication_stats=publication_stats, observed_at=observed_at,
    )


@mcp.tool()
def create_campaign(slug: str, name: str, objective: str, source_id: str | None = None) -> dict:
    """Create a draft content campaign for a brand."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return store.insert("campaigns", {"brand_id": brand["id"], "name": name, "objective": objective, "source_id": source_id, "status": "draft"})


@mcp.tool()
def draft_post(campaign_id: str, channel: str, body: str) -> dict:
    """Create a draft social post. It must be approved before it can be scheduled."""
    _initialize_runtime()
    if not store.row("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)):
        raise ValueError("Unknown campaign")
    return store.insert("posts", {"campaign_id": campaign_id, "channel": channel, "body": body, "status": "draft", "scheduled_for": None, "external_post_id": None})


@mcp.tool()
def record_performance(slug: str, channel: str, observed_at: str, impressions: int = 0,
                       clicks: int = 0, engagements: int = 0, conversions: int = 0,
                       revenue_cents: int = 0, notes: str = "", post_id: str | None = None,
                       source_id: str | None = None) -> dict:
    """Record measured content outcomes so future plans can learn from real performance."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return store.insert("performance_records", {
        "brand_id": brand["id"], "post_id": post_id, "source_id": source_id,
        "channel": channel, "observed_at": observed_at, "impressions": impressions,
        "clicks": clicks, "engagements": engagements, "conversions": conversions,
        "revenue_cents": revenue_cents, "notes": notes,
    })


@mcp.tool()
def get_performance(slug: str, limit: int = 50) -> list[dict]:
    """Read recent measured outcomes for a brand."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return store.rows(
        """SELECT * FROM performance_records WHERE brand_id=? AND NOT EXISTS (
             SELECT 1 FROM fixture_quarantine_registry q
             WHERE q.table_name='performance_records'
               AND q.record_key_json=json_array(performance_records.id))
           ORDER BY observed_at DESC LIMIT ?""",
        (brand["id"], limit),
    )


@mcp.tool()
def get_performance_planning(
    slug: str, stage: str = "portfolio", channel: str = "x",
    topic: str | None = None, template_key: str | None = None,
    as_of: str | None = None,
) -> dict:
    """Explain fresh, scoped measured outcomes as a capped planning prior; never publish or approve."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return PerformancePlanningEngine(store.DATA_PATH).plan(
        brand["id"], {"stage": stage, "channel": channel, "topic": topic,
                      "template_key": template_key}, as_of=as_of,
    )


@mcp.tool()
def draft_x_experiment(
    slug: str, campaign_id: str, hypothesis: str, metric: str,
    guardrails: dict | None = None, measurement_windows: list[dict] | None = None,
) -> dict:
    """Create source-grounded X draft variants; never approve, schedule, or publish them."""
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return experiment_store.draft(
        brand_id=brand["id"], campaign_id=campaign_id, hypothesis=hypothesis,
        metric=metric, guardrails=guardrails, measurement_windows=measurement_windows,
    )


@mcp.tool()
def list_x_experiments(slug: str) -> list[dict]:
    """Read governed X experiments, variants, evidence, and recommendation state."""
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return experiment_store.list(brand["id"])


@mcp.tool()
def get_x_experiment(experiment_id: str) -> dict:
    """Read one governed X experiment without changing its state."""
    return experiment_store.get(experiment_id)


@mcp.tool()
def list_experiment_measurement_windows(experiment_id: str) -> list[dict]:
    """Read persisted collection/evaluation windows and evidence states; performs no collection or write."""
    experiment_store.get(experiment_id)
    return experiment_store.list_windows(experiment_id=experiment_id)


@mcp.tool()
def get_experiment_measurement_window(window_id: str) -> dict:
    """Read one measurement window, evidence provenance, and durable event history."""
    return experiment_store.get_window(window_id)


@mcp.tool()
def recommend_x_experiment_winner(experiment_id: str) -> dict:
    """Recommend from connector-backed evidence; never accept or apply a winner."""
    return experiment_store.recommend(experiment_id)


@mcp.tool()
def propose_brand_learning(
    slug: str, hypothesis: str, evidence: str, proposed_change: str,
    evidence_for: list[dict] | None = None, evidence_against: list[dict] | None = None,
    effect: dict | None = None, uncertainty: dict | None = None,
    scope: dict | None = None, review_at: str | None = None,
) -> dict:
    """Create an audited structured proposal; it remains inert until tested and accepted."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return BrandLearningEngine(store.DATA_PATH).propose(
        brand["id"], hypothesis=hypothesis, proposed_change=proposed_change,
        evidence_for=evidence_for or [{"summary": evidence}],
        evidence_against=evidence_against or [], effect=effect or {},
        uncertainty=uncertainty or {}, scope=scope or {}, review_at=review_at,
        actor="mcp:propose_brand_learning",
    )


@mcp.tool()
def report_product_gap(slug: str, summary: str, details: str, reporter: str = "agent") -> dict:
    """Report a Brand OS capability gap discovered while doing real brand work."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return FeedbackStore(store.DATA_PATH).report(
        brand_id=brand["id"], reporter=reporter, summary=summary, details=details,
        component="agent-workflow",
    )


@mcp.tool()
def list_product_gaps(slug: str, status: str = "open") -> list[dict]:
    """List visible product and workflow gaps reported during brand operations."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return FeedbackStore(store.DATA_PATH).list(brand_id=brand["id"], status=status)


@mcp.tool()
def comment_on_product_gap(feedback_id: str, body: str, actor: str = "agent") -> dict:
    """Add agent evidence or reproduction detail without changing gap lifecycle."""
    return FeedbackStore(store.DATA_PATH).comment(feedback_id, body, actor=actor)


def _dispatch_call(operation):
    try:
        return operation()
    except KeyError as error:
        raise ValueError("Dispatch item not found") from error
    except DispatchError as error:
        raise ValueError(str(error)) from error


@mcp.tool()
def list_dispatch_items(slug: str, status: str | None = None) -> list[dict]:
    """List governed external-action drafts and their approval state. This never executes them."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    lifecycle = Lifecycle(status) if status else None
    return [dispatch_item_to_dict(item) for item in dispatch_store.list_items(brand_id=brand["id"], status=lifecycle)]


@mcp.tool()
def create_dispatch_item(slug: str, connector: str, payload: dict) -> dict:
    """Create a governed draft external action. Creation does not approve, queue, or publish it."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return dispatch_item_to_dict(dispatcher.create(connector, payload, brand_id=brand["id"]))


@mcp.tool()
def create_post_dispatch_item(canonical_post_id: str) -> dict:
    """Create or refresh the one governed X dispatch revision linked to a canonical campaign post."""
    try:
        return dispatch_item_to_dict(create_dispatch_from_post(
            dispatcher, canonical_post_id, attribution=attribution_store,
        ))
    except CanonicalPostNotFound as error:
        raise ValueError("Canonical post not found") from error


@mcp.tool()
def validate_dispatch_item(item_id: str) -> dict:
    """Validate channel payload and effective length before requesting human approval."""
    return _dispatch_call(lambda: dispatcher.validate(item_id).as_dict())


@mcp.tool()
def edit_dispatch_item(item_id: str, payload: dict, actor: str) -> dict:
    """Edit a draft/action; any prior approval is invalidated and the revision increases."""
    return _dispatch_call(lambda: dispatch_item_to_dict(dispatcher.edit(item_id, payload, actor=actor)))


@mcp.tool()
def submit_dispatch_item(item_id: str, actor: str) -> dict:
    """Submit a draft revision for explicit human approval."""
    return _dispatch_call(lambda: dispatch_item_to_dict(dispatcher.submit_for_approval(item_id, actor=actor)))


@mcp.tool()
def get_dispatch_audit(item_id: str) -> list[dict]:
    """Inspect the durable audit history for a governed external action."""
    return _dispatch_call(lambda: [audit_event_to_dict(event) for event in dispatch_store.list_audit(item_id)])


def _mcp_brand_engagement(slug: str, opportunity_id: str | None = None):
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    if opportunity_id is None:
        return brand, None
    try:
        opportunity = engagement_store.get(opportunity_id)
    except KeyError as error:
        raise ValueError("Engagement opportunity not found") from error
    if opportunity["brand_id"] != brand["id"]:
        raise ValueError("Engagement opportunity not found")
    return brand, opportunity


def _engagement_call(operation):
    try:
        return operation()
    except KeyError as error:
        raise ValueError("Engagement opportunity not found") from error
    except (EngagementError, DispatchError, ValueError) as error:
        raise ValueError(str(error)) from error


@mcp.tool()
def list_engagement_opportunities(
    slug: str, state: str | None = None, limit: int = 100
) -> list[dict]:
    """List ranked X engagement opportunities for one brand. This performs no external action."""
    brand, _ = _mcp_brand_engagement(slug)
    return engagement_store.list(brand_id=brand["id"], state=state, limit=limit)


@mcp.tool()
def get_engagement_opportunity(slug: str, opportunity_id: str) -> dict:
    """Inspect one X opportunity, its context, score, and governed action state."""
    _, opportunity = _mcp_brand_engagement(slug, opportunity_id)
    return opportunity


@mcp.tool()
def get_engagement_history(slug: str, opportunity_id: str) -> list[dict]:
    """Inspect the stored interaction and workflow history for an X opportunity."""
    _mcp_brand_engagement(slug, opportunity_id)
    return engagement_store.history(opportunity_id)


@mcp.tool()
def draft_engagement_action(
    slug: str,
    opportunity_id: str,
    action_type: str,
    actor: str,
    text: str | None = None,
) -> dict:
    """Draft a reply, like, or follow as a governed dispatch; this does not approve or execute it."""
    _mcp_brand_engagement(slug, opportunity_id)
    opportunity, dispatch = _engagement_call(lambda: engagement_store.draft_action(
        opportunity_id, action_type, dispatcher, actor=actor, text=text
    ))
    return {"opportunity": opportunity, "dispatch_item": dispatch_item_to_dict(dispatch)}


@mcp.tool()
def submit_engagement_action(slug: str, opportunity_id: str, actor: str) -> dict:
    """Submit a linked engagement draft for human approval; this cannot approve or queue it."""
    _mcp_brand_engagement(slug, opportunity_id)
    opportunity, dispatch = _engagement_call(
        lambda: engagement_store.submit_action_for_approval(
            opportunity_id, dispatcher, actor=actor
        )
    )
    return {"opportunity": opportunity, "dispatch_item": dispatch_item_to_dict(dispatch)}


@mcp.tool()
def dismiss_engagement_opportunity(
    slug: str, opportunity_id: str, actor: str, reason: str = ""
) -> dict:
    """Dismiss a non-actionable X opportunity without performing an external action."""
    _mcp_brand_engagement(slug, opportunity_id)
    return _engagement_call(
        lambda: engagement_store.dismiss(opportunity_id, actor=actor, reason=reason)
    )


@mcp.tool()
def create_editorial_candidate(slug: str, title: str, dimensions: dict[str, float],
                               summary: str = "", recommended_treatment: str = "monitor",
                               rationale: list[str] | None = None,
                               supporting_sources: list[dict] | None = None) -> dict:
    """Create or update a ranked, source-backed editorial candidate."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return editorial_store.upsert_candidate(
        brand["id"], title, dimensions, summary=summary,
        recommended_treatment=recommended_treatment,
        rationale=rationale or (), supporting_sources=supporting_sources or (),
    )


@mcp.tool()
def list_editorial_candidates(slug: str, include_inactive: bool = False) -> list[dict]:
    """List prioritized open editorial opportunities."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return editorial_store.list_candidates(brand["id"], status=None if include_inactive else "open")


@mcp.tool()
def abandon_editorial_candidate(candidate_id: str, actor: str, reason: str) -> dict:
    """Dismiss an open editorial opportunity with a durable actor/reason audit; never deletes it."""
    return editorial_store.abandon_candidate(candidate_id, actor=actor, reason=reason)


@mcp.tool()
def archive_editorial_candidate(candidate_id: str, actor: str, reason: str) -> dict:
    """Archive inactive candidate work when it has no active downstream newsletter issue."""
    return editorial_store.archive_candidate(candidate_id, actor=actor, reason=reason)


@mcp.tool()
def get_editorial_candidate_history(candidate_id: str) -> list[dict]:
    """Read the governed abandon/archive history for one editorial candidate."""
    return editorial_store.list_candidate_history(candidate_id)


@mcp.tool()
def create_newsletter_issue(slug: str, content: dict, created_by: str,
                            candidate_id: str | None = None) -> dict:
    """Create the canonical Brand OS newsletter issue; this does not approve or export it."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return editorial_store.create_issue(
        brand["id"], content, created_by=created_by, candidate_id=candidate_id,
    )


@mcp.tool()
def create_newsletter_distribution_package(
    issue_id: str, expected_revision: int, primary_source_id: str,
    campaign_name: str, objective: str, email: dict, web: dict,
    distribution: dict, x_drafts: list[dict], idempotency_key: str,
    actor: str, flight_name: str = "primary", flight_start: str | None = None,
    flight_end: str | None = None,
) -> dict:
    """Create draft-only email/web/X artifacts grounded in exact-revision primary evidence.

    Candidate discovery lineage remains distinct. A same-brand canonical source
    cited by the exact newsletter revision may be primary evidence even when it
    was not the discovery source; explicit drift/conflict fails closed.
    """
    _initialize_runtime()
    try:
        issue = editorial_store.get_issue(issue_id)
        return DistributionPackageStore(
            store.DATA_PATH, editorial_store, dispatcher,
        ).create(
            issue["brand_id"], issue_id, expected_revision=expected_revision,
            primary_source_id=primary_source_id, campaign_name=campaign_name,
            objective=objective, email=email, web=web, distribution=distribution,
            x_drafts=x_drafts, idempotency_key=idempotency_key, actor=actor,
            flight_name=flight_name, flight_start=flight_start, flight_end=flight_end,
        )
    except KeyError as error:
        raise ValueError("Newsletter issue not found") from error
    except DistributionPackageError as error:
        raise ValueError(str(error)) from error


@mcp.tool()
def list_newsletter_distribution_packages(slug: str) -> list[dict]:
    """List draft distribution packages with complete source-to-artifact lineage."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return DistributionPackageStore(
        store.DATA_PATH, editorial_store, dispatcher,
    ).list(brand["id"])


@mcp.tool()
def get_newsletter_distribution_package(package_id: str) -> dict:
    """Read one newsletter-led package and its governed draft artifacts."""
    try:
        return DistributionPackageStore(
            store.DATA_PATH, editorial_store, dispatcher,
        ).get(package_id)
    except KeyError as error:
        raise ValueError("Distribution package not found") from error


@mcp.tool()
def bind_newsletter_distribution_destination(
    package_id: str, destination_url: str, actor: str,
) -> dict:
    """Bind attributed X CTA URLs as new drafts only; never approve, schedule, or publish."""
    try:
        return DistributionPackageStore(
            store.DATA_PATH, editorial_store, dispatcher,
        ).bind_destination(package_id, destination_url, actor=actor)
    except (KeyError, DistributionPackageError) as error:
        raise ValueError(str(error)) from error


@mcp.tool()
def get_distribution_campaign_measurement(package_id: str) -> dict:
    """Read campaign totals, denominator-weighted rates, channel/asset detail, and attribution confidence."""
    try:
        return DistributionPackageStore(
            store.DATA_PATH, editorial_store, dispatcher,
        ).measurement(package_id)
    except KeyError as error:
        raise ValueError("Distribution package not found") from error


@mcp.tool()
def get_distribution_membership_audit(package_id: str) -> list[dict]:
    """Read audited campaign asset attachment and movement history; this cannot mutate membership."""
    try:
        return DistributionPackageStore(
            store.DATA_PATH, editorial_store, dispatcher,
        ).membership_audit(package_id)
    except KeyError as error:
        raise ValueError("Distribution package not found") from error


@mcp.tool()
def list_campaign_graphs(slug: str) -> list[dict]:
    """List generic campaign graphs with anchors, touchpoints, relationships, and flights."""
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    DistributionPackageStore(store.DATA_PATH, editorial_store, dispatcher)
    return CampaignGraphStore(store.DATA_PATH).list(brand["id"])


@mcp.tool()
def get_campaign_graph(campaign_id: str) -> dict:
    """Read one generic campaign graph without changing content or approval."""
    try:
        DistributionPackageStore(store.DATA_PATH, editorial_store, dispatcher)
        return CampaignGraphStore(store.DATA_PATH).get(campaign_id)
    except KeyError as error:
        raise ValueError("Campaign not found") from error


@mcp.tool()
def get_campaign_measurement(campaign_id: str) -> dict:
    """Read channel-native asset metrics and the safe cross-channel outcome rollup."""
    try:
        DistributionPackageStore(store.DATA_PATH, editorial_store, dispatcher)
        return CampaignGraphStore(store.DATA_PATH).measurement(campaign_id)
    except KeyError as error:
        raise ValueError("Campaign not found") from error


@mcp.tool()
def instantiate_campaign_template(
    slug: str, template_key: str, answers: dict, name: str, objective: str,
    source_id: str, idempotency_key: str, actor: str, flight_name: str = "primary",
    flight_start: str | None = None, flight_end: str | None = None,
) -> dict:
    """Instantiate any immutable recipe as a draft-only graph; never approve or publish."""
    try:
        DistributionPackageStore(store.DATA_PATH, editorial_store, dispatcher)
        return CampaignTemplateStore(store.DATA_PATH).instantiate(
            template_key, slug, answers, name=name, objective=objective, source_id=source_id,
            idempotency_key=idempotency_key, actor=actor, flight_name=flight_name,
            flight_start=flight_start, flight_end=flight_end,
        )
    except (CampaignTemplateError, CampaignGraphError, KeyError) as error:
        raise ValueError(str(error)) from error


@mcp.tool()
def list_newsletter_issues(slug: str, include_inactive: bool = False) -> list[dict]:
    """List canonical newsletter issues and their current revisions/lifecycle."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return editorial_store.list_issues(brand["id"], include_inactive=include_inactive)


@mcp.tool()
def abandon_newsletter_issue(issue_id: str, actor: str, reason: str) -> dict:
    """Abandon pre-publication editorial work without approving, publishing, or deleting it."""
    return editorial_store.abandon_issue(issue_id, actor=actor, reason=reason)


@mcp.tool()
def archive_newsletter_issue(issue_id: str, actor: str, reason: str) -> dict:
    """Archive only abandoned or published newsletter work; active delivery remains visible."""
    return editorial_store.archive_issue(issue_id, actor=actor, reason=reason)


@mcp.tool()
def get_newsletter_issue_history(issue_id: str) -> list[dict]:
    """Read the governed abandon/archive history for one newsletter issue."""
    return editorial_store.list_issue_history(issue_id)


@mcp.tool()
def get_newsletter_provider_reconciliations(issue_id: str) -> list[dict]:
    """Read exact-revision Beehiiv lifecycle observations; this cannot mutate content."""
    try:
        editorial_store.get_issue(issue_id)
    except KeyError as error:
        raise ValueError("Newsletter issue not found") from error
    return beehiiv_lifecycle_projector.list(issue_id)


@mcp.tool()
def revise_newsletter_issue(issue_id: str, changes: dict, created_by: str,
                            change_note: str = "") -> dict:
    """Create a new immutable newsletter revision and invalidate any previous approval."""
    return editorial_store.revise_issue(
        issue_id, changes, created_by=created_by, change_note=change_note,
    )


@mcp.tool()
def advance_newsletter_issue(issue_id: str, target: str) -> dict:
    """Advance an issue through outline, draft, or fact_checked; human approval remains REST-only."""
    if target not in {"outline", "draft", "fact_checked"}:
        raise ValueError("MCP may advance only to outline, draft, or fact_checked")
    return editorial_store.transition(issue_id, target)


@mcp.tool()
def enqueue_newsletter_draft_export(
    issue_id: str, connector_account_id: str | None = None,
    run_after: str | None = None,
) -> dict:
    """Queue an already human-approved exact revision as a Beehiiv draft; never approve, schedule, or publish."""

    issue = editorial_store.get_issue(issue_id)
    account = resolve_beehiiv_export_account(
        issue["brand_id"], connector_account_id,
        credentials=CredentialStore.from_environment(store.DATA_PATH),
    )
    return enqueue_newsletter_export(
        editorial_store,
        issue_id,
        brand_id=issue["brand_id"],
        connector_account_id=account["id"],
        run_after=run_after,
    )


@mcp.tool()
def get_newsletter_draft_export_job(job_id: str) -> dict:
    """Read durable Beehiiv draft-export status and its safe provider receipt."""

    return get_newsletter_export_job(job_id)


@mcp.tool()
def list_newsletter_draft_export_jobs(issue_id: str) -> list[dict]:
    """List Beehiiv draft-export jobs for one canonical newsletter issue."""

    editorial_store.get_issue(issue_id)
    return list_newsletter_export_jobs(issue_id)


@mcp.tool()
def list_execution_handoffs(slug: str, status: str | None = None) -> list[dict]:
    """List provider-UI work for exact human-approved revisions; never approves or executes it."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    if status not in {None, "pending", "claimed", "needs_attention", "completed", "stale"}:
        raise ValueError("invalid execution handoff status")
    execution_handoff_store.ensure_for_brand(brand["id"])
    return execution_handoff_store.list(brand["id"], status=status)


@mcp.tool()
def get_execution_handoff_controls(slug: str) -> dict:
    """Read assisted-delivery kill switches and their immutable audit; MCP cannot change them."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    return {
        "controls": execution_handoff_store.controls(brand["id"]),
        "audit": execution_handoff_store.control_audit(brand["id"]),
    }


@mcp.tool()
def claim_execution_handoff(task_id: str, actor: str, lease_seconds: int = 900) -> dict:
    """Lease one exact-approved browser task; this never grants approval or performs a provider write."""
    return execution_handoff_store.claim(task_id, actor=actor, lease_seconds=lease_seconds)


@mcp.tool()
def begin_execution_handoff_action(
    task_id: str, actor: str, claim_token: str,
) -> dict:
    """Mark the exact pre-click boundary after UI comparison; performs no provider write.

    For X this succeeds only while the separate human action-time confirmation
    is current. Once marked, an expired task cannot be reclaimed and duplicated.
    """
    return execution_handoff_store.begin_external_action(
        task_id, actor=actor, claim_token=claim_token,
    )


@mcp.tool()
def submit_execution_handoff_receipt(
    task_id: str, claim_token: str, external_id: str,
    status: str, external_url: str | None = None,
    content_fingerprint: str | None = None,
    asset_fingerprint: str | None = None,
) -> dict:
    """Record a UI-created Beehiiv draft or an already posted approved X action; never sends content."""
    return execution_handoff_store.submit_receipt(
        task_id, claim_token=claim_token, external_id=external_id,
        external_url=external_url, status=status,
        content_fingerprint=content_fingerprint, asset_fingerprint=asset_fingerprint,
    )


@mcp.tool()
def get_execution_handoff_audit(task_id: str) -> list[dict]:
    """Read claim, expiry, invalidation, and receipt history without capability tokens."""
    return execution_handoff_store.audit(task_id)


@mcp.tool()
def list_beehiiv_assisted_pulls(slug: str, status: str | None = None) -> list[dict]:
    """List due Beehiiv metadata/aggregate-read tasks; no subscriber data or provider writes."""
    _initialize_runtime()
    brand = store.get_brand(slug)
    if not brand:
        raise ValueError(f"Unknown brand: {slug}")
    if status not in {None, "pending", "claimed", "completed", "failed"}:
        raise ValueError("invalid assisted Beehiiv pull status")
    return beehiiv_assisted_pull_store.list(brand["id"], status=status)


@mcp.tool()
def claim_beehiiv_assisted_pull(
    task_id: str, actor: str, lease_seconds: int = 900,
) -> dict:
    """Lease one read-only Beehiiv metadata/aggregate task; performs no provider action."""
    _initialize_runtime()
    return beehiiv_assisted_pull_store.claim(
        task_id, actor=actor, lease_seconds=lease_seconds,
    )


@mcp.tool()
def heartbeat_beehiiv_assisted_pull(
    task_id: str, actor: str, claim_token: str, lease_seconds: int = 900,
) -> dict:
    """Renew a live assisted-read lease after the configured agent heartbeat."""
    _initialize_runtime()
    return beehiiv_assisted_pull_store.heartbeat(
        task_id, actor=actor, claim_token=claim_token, lease_seconds=lease_seconds,
    )


@mcp.tool()
def submit_beehiiv_assisted_pull_receipt(
    task_id: str, actor: str, claim_token: str, observed_at: str,
    posts: list[dict], publication_stats: dict | None = None,
) -> dict:
    """Persist only validated post metadata and aggregate Beehiiv measurements."""
    _initialize_runtime()
    return beehiiv_assisted_pull_store.submit_receipt(
        task_id, actor=actor, claim_token=claim_token, observed_at=observed_at,
        posts=posts, publication_stats=publication_stats,
    )


@mcp.tool()
def fail_beehiiv_assisted_pull(
    task_id: str, actor: str, claim_token: str, failure_code: str,
) -> dict:
    """Release a failed read task for bounded retry using a non-sensitive code."""
    _initialize_runtime()
    return beehiiv_assisted_pull_store.record_failure(
        task_id, actor=actor, claim_token=claim_token, failure_code=failure_code,
    )


@mcp.tool()
def get_beehiiv_assisted_pull_audit(task_id: str) -> list[dict]:
    """Read the request, lease, retry, and aggregate-receipt audit trail."""
    _initialize_runtime()
    return beehiiv_assisted_pull_store.audit(task_id)


# Installed ``brandman.plugins`` may add MCP tools.
from .extensions import load_plugins  # noqa: E402

load_plugins("mcp", mcp)


def main() -> None:
    _initialize_runtime()
    mcp.run(transport="stdio")
