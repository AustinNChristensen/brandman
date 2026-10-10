"""Durable, idempotent orchestration for read-side connector syncs.

Connectors normalize remote responses; this module applies those responses to the
BrandMan system of record.  It deliberately receives connectors and persistence
as dependencies, so importing it never opens a network connection or requires a
credential.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any, Mapping

from app import store
from app.connectors import (
    ConnectorError,
    ConnectorEvent,
    ConnectorKind,
    ConnectorResult,
    EventKind,
    ReadConnector,
    SyncCursor,
)
from app.operational_feedback import mark_feedback_reported


SYNC_JOB_TYPE = "connector.sync"


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    connector_account_id: str
    stream: str
    received: int
    recorded: int
    sources_upserted: int
    performance_recorded: int
    next_cursor: str | None
    has_more: bool
    continuation_job_id: str | None = None
    engagement_projected: int = 0
    kpis_projected: int = 0
    editorial_candidates_projected: int = 0
    campaigns_projected: int = 0
    post_drafts_projected: int = 0
    campaign_metrics_projected: int = 0
    newsletter_lifecycle_reconciled: int = 0
    website_sessions_recorded: int = 0
    website_tool_uses_recorded: int = 0
    website_deployments_recorded: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SyncOrchestrator:
    """Apply one connector batch and advance its cursor only after success."""

    def __init__(
        self, *, repository: Any = store, engagement_inbox: Any | None = None,
        kpi_projector: Any | None = None, source_campaign_operator: Any | None = None,
        campaign_metric_projector: Any | None = None,
        canonical_revalidation_store: Any | None = None,
        newsletter_lifecycle_projector: Any | None = None,
    ) -> None:
        self.repository = repository
        self.engagement_inbox = engagement_inbox
        self.kpi_projector = kpi_projector
        self.source_campaign_operator = source_campaign_operator
        self.campaign_metric_projector = campaign_metric_projector
        self.canonical_revalidation_store = canonical_revalidation_store
        self.newsletter_lifecycle_projector = newsletter_lifecycle_projector

    def sync_once(
        self,
        connector: ReadConnector,
        *,
        brand_id: str,
        connector_account_id: str,
        stream: str = "content",
        enqueue_continuation: bool = True,
    ) -> SyncOutcome:
        cursor_record = self.repository.get_sync_cursor(connector_account_id, stream)
        cursor = SyncCursor(cursor_record["cursor"]) if cursor_record and cursor_record.get("cursor") else None
        try:
            result = connector.sync(cursor)
            return self.apply_result(
                result,
                connector_kind=connector.kind,
                brand_id=brand_id,
                connector_account_id=connector_account_id,
                stream=stream,
                enqueue_continuation=enqueue_continuation,
            )
        except Exception as exc:
            self._report_failure(
                exc,
                connector_kind=connector.kind,
                brand_id=brand_id,
                connector_account_id=connector_account_id,
                stream=stream,
                cursor=cursor.value if cursor else None,
            )
            raise

    def apply_result(
        self,
        result: ConnectorResult,
        *,
        connector_kind: ConnectorKind,
        brand_id: str,
        connector_account_id: str,
        stream: str = "content",
        enqueue_continuation: bool = True,
    ) -> SyncOutcome:
        """Persist an already-fetched batch, useful for push and MCP adapters."""
        if result.has_more and result.next_cursor is None:
            raise ValueError("a paginated connector batch must provide its next cursor")

        recorded = sources = performance = engagement = kpis = 0
        candidates = campaigns = post_drafts = campaign_metrics = newsletter_lifecycle = 0
        website_sessions = website_tool_uses = website_deployments = 0
        shortlist_candidate_ids: list[str] = []
        observed: list[str] = []
        # Validate the complete provider page before projecting its first item.
        # This makes a mixed valid/forged attribution page fail as one unit and
        # keeps its cursor replayable without partially credited conversions.
        page_validator = getattr(self.campaign_metric_projector, "validate_page", None)
        validator = getattr(self.campaign_metric_projector, "validate", None)
        for event in result.events:
            if event.connector != connector_kind:
                raise ValueError("connector result contains an event from a different connector")
        if page_validator is not None:
            page_validator(
                brand_id=brand_id, connector_account_id=connector_account_id,
                stream=stream,
                events=result.events,
            )
        elif validator is not None:
            for event in result.events:
                validator(
                    brand_id=brand_id,
                    connector_account_id=connector_account_id,
                    event=event,
                )
        for event in result.events:
            if self.newsletter_lifecycle_projector is not None:
                self.newsletter_lifecycle_projector.validate(
                    brand_id=brand_id, connector_account_id=connector_account_id,
                    event=event,
                )
        for event in result.events:
            existing_event = self._stored_event(connector_account_id, stream, event)
            was_recorded = existing_event is not None
            owned_issue = bool(
                self.newsletter_lifecycle_projector is not None
                and self.newsletter_lifecycle_projector.binding(
                    brand_id=brand_id, connector_account_id=connector_account_id,
                    event=event,
                ) is not None
            )
            source_count, performance_count = (
                (0, 0) if owned_issue else self._project_event(
                    event, brand_id, connector_account_id
                )
            )
            sources += source_count
            performance += performance_count
            stored_event = existing_event or self.repository.record_connector_event(
                connector_account_id,
                stream,
                event.dedup_key,
                event.kind.value,
                {**dict(event.payload), "provider_external_id": event.external_id},
                event.occurred_at or self.repository.now(),
            )
            if self.newsletter_lifecycle_projector is not None:
                newsletter_lifecycle += int(self.newsletter_lifecycle_projector.project(
                    brand_id=brand_id, connector_account_id=connector_account_id,
                    connector_event=stored_event, event=event,
                ))
            if self.campaign_metric_projector is not None:
                campaign_metrics += int(self.campaign_metric_projector.project(
                    brand_id=brand_id,
                    connector_account_id=connector_account_id,
                    connector_event=stored_event,
                    event=event,
                ))
            if (self.source_campaign_operator is not None and not owned_issue
                    and event.kind == EventKind.SOURCE_ITEM):
                content_projection = self.source_campaign_operator.project(
                    brand_id=brand_id,
                    connector_account_id=connector_account_id,
                    connector_event=stored_event,
                    event=event,
                    promote=False,
                )
                if content_projection is not None:
                    candidates += int(content_projection.candidate_created)
                    # Replays refresh provenance but cannot consume another
                    # shortlist slot or gradually fan out the backlog.
                    if not was_recorded:
                        shortlist_candidate_ids.append(content_projection.candidate_id)
            if self.kpi_projector is not None:
                projection = self.kpi_projector.project(
                    brand_id=brand_id,
                    connector_account_id=connector_account_id,
                    connector_event=stored_event,
                    event=event,
                )
                kpis += projection.promoted
            if (
                self.engagement_inbox is not None
                and event.connector == ConnectorKind.X
                and event.payload.get("evidence_type") == "x_engagement_opportunity"
            ):
                self.engagement_inbox.project(
                    brand_id=brand_id,
                    connector_account_id=connector_account_id,
                    event=event,
                )
                engagement += 1
            recorded += int(not was_recorded)
            if event.occurred_at:
                observed.append(event.occurred_at)
            if not was_recorded and event.connector == ConnectorKind.WEBSITE:
                website_event_type = str(event.payload.get("event_type") or "metric")
                website_sessions += int(website_event_type == "authenticated_session")
                website_tool_uses += int(website_event_type == "tool_use")
                website_deployments += int(event.kind == EventKind.DEPLOYMENT_CHANGED)

        if self.source_campaign_operator is not None and shortlist_candidate_ids:
            promoted = self.source_campaign_operator.promote_shortlist(
                brand_id, shortlist_candidate_ids, limit=3,
            )
            campaigns += sum(int(item.campaign_created) for item in promoted)
            post_drafts += sum(int(item.post_created) for item in promoted)

        # This is intentionally last: a failed projection leaves the previous
        # cursor intact, making the whole batch safe to replay.
        next_cursor = result.next_cursor.value if result.next_cursor else None
        self.repository.set_sync_cursor(
            connector_account_id,
            stream,
            next_cursor,
            watermark=max(observed) if observed else None,
        )

        continuation_id = None
        if result.has_more and enqueue_continuation:
            continuation = enqueue_sync_job(
                brand_id=brand_id,
                connector_account_id=connector_account_id,
                stream=stream,
                idempotency_key=f"{connector_account_id}:{stream}:cursor:{next_cursor}",
                repository=self.repository,
            )
            continuation_id = continuation["id"]

        return SyncOutcome(
            connector_account_id=connector_account_id,
            stream=stream,
            received=len(result.events),
            recorded=recorded,
            sources_upserted=sources,
            performance_recorded=performance,
            next_cursor=next_cursor,
            has_more=result.has_more,
            continuation_job_id=continuation_id,
            engagement_projected=engagement,
            kpis_projected=kpis,
            editorial_candidates_projected=candidates,
            campaigns_projected=campaigns,
            post_drafts_projected=post_drafts,
            campaign_metrics_projected=campaign_metrics,
            newsletter_lifecycle_reconciled=newsletter_lifecycle,
            website_sessions_recorded=website_sessions,
            website_tool_uses_recorded=website_tool_uses,
            website_deployments_recorded=website_deployments,
        )

    def _event_exists(self, account_id: str, stream: str, event: ConnectorEvent) -> bool:
        return self._stored_event(account_id, stream, event) is not None

    def _stored_event(
        self, account_id: str, stream: str, event: ConnectorEvent,
    ) -> dict[str, Any] | None:
        # Website provider identities belong to the connector account, not to a
        # caller-selected polling stream. This prevents the same provider event
        # from being counted once as `metrics` and again as `events`.
        if event.connector == ConnectorKind.WEBSITE:
            found = self.repository.row(
                """SELECT * FROM connector_events WHERE connector_account_id=?
                   AND external_id=? ORDER BY created_at,id LIMIT 1""",
                (account_id, event.dedup_key),
            )
        else:
            found = self.repository.row(
                """SELECT * FROM connector_events WHERE connector_account_id=? AND stream=?
                   AND external_id=? AND event_type=?""",
                (account_id, stream, event.dedup_key, event.kind.value),
            )
        if found is not None and isinstance(found.get("payload"), str):
            found = {**found, "payload": json.loads(found["payload"])}
        return found

    def _project_event(
        self, event: ConnectorEvent, brand_id: str, connector_account_id: str
    ) -> tuple[int, int]:
        if event.kind == EventKind.SOURCE_ITEM:
            source = self.repository.upsert_external_source(
                brand_id, _source_payload(event, connector_account_id)
            )
            snapshot = event.payload.get("canonical_revalidation")
            if self.canonical_revalidation_store is not None and isinstance(snapshot, Mapping):
                latest = self.canonical_revalidation_store.latest(brand_id, source["id"])
                # Replaying an unchanged connector item must not create a second
                # observation merely because the network fetch happened later.
                same_evidence = bool(
                    latest
                    and latest.get("snapshot_fingerprint") == snapshot.get("snapshot_fingerprint")
                    and latest.get("feed_fingerprint") == snapshot.get("feed_fingerprint")
                    and latest.get("status") == snapshot.get("status")
                )
                if not same_evidence:
                    self.canonical_revalidation_store.record(
                        brand_id, source["id"], snapshot, actor="rss-connector",
                    )
            return 1, 0
        if (
            event.connector == ConnectorKind.WEBSITE
            and event.payload.get("event_type") in {"authenticated_session", "tool_use"}
        ):
            # Typed first-party funnel events have their own exact-attribution
            # projection; do not manufacture zero-valued generic performance rows.
            return 0, 0
        if event.kind == EventKind.METRIC_OBSERVED:
            notes = f"connector_event={connector_account_id}:{event.dedup_key}"
            existing = self.repository.row(
                "SELECT id FROM performance_records WHERE brand_id=? AND channel=? AND notes=?",
                (brand_id, event.connector.value, notes),
            )
            if existing:
                return 0, 0
            self.repository.insert(
                "performance_records",
                _performance_payload(event, brand_id=brand_id, notes=notes),
            )
            return 0, 1
        return 0, 0

    def _report_failure(
        self,
        exc: Exception,
        *,
        connector_kind: ConnectorKind,
        brand_id: str,
        connector_account_id: str,
        stream: str,
        cursor: str | None,
    ) -> None:
        # ConnectorError is designed to be credential-safe. Avoid copying text
        # from arbitrary transport exceptions into durable feedback.
        actual = str(exc) if isinstance(exc, ConnectorError) else type(exc).__name__
        operation = exc.operation if isinstance(exc, ConnectorError) else "sync"
        feedback = self.repository.report_product_feedback(
            brand_id=brand_id,
            reporter="sync-orchestrator",
            summary=f"{connector_kind.value} {stream} sync failed",
            details=f"The {operation} operation failed before its cursor could advance.",
            component=f"{connector_kind.value}.{stream}.sync",
            severity="high",
            fingerprint=f"connector-sync:{connector_account_id}:{stream}:{operation}",
            reproduction=f"Run {stream} sync from cursor {cursor or '<start>'}.",
            expected_behavior="The batch is persisted once and its cursor advances.",
            actual_behavior=actual,
            workaround="Reconnect or correct connector permissions, then retry the durable job.",
            related_ids=[connector_account_id],
        )
        mark_feedback_reported(exc, feedback)


def enqueue_sync_job(
    *,
    brand_id: str,
    connector_account_id: str,
    stream: str,
    idempotency_key: str,
    repository: Any = store,
    run_after: str | None = None,
    priority: int = 0,
) -> dict[str, Any]:
    """Enqueue a sync once. Callers supply a stable schedule/cycle identity."""
    return repository.enqueue_job(
        SYNC_JOB_TYPE,
        idempotency_key,
        {"brand_id": brand_id, "connector_account_id": connector_account_id, "stream": stream},
        brand_id=brand_id,
        connector_account_id=connector_account_id,
        run_after=run_after,
        priority=priority,
    )


def make_sync_job_handler(
    connectors: Mapping[str, ReadConnector], *, repository: Any = store,
    engagement_inbox: Any | None = None,
    kpi_projector: Any | None = None,
    source_campaign_operator: Any | None = None,
    campaign_metric_projector: Any | None = None,
    canonical_revalidation_store: Any | None = None,
    newsletter_lifecycle_projector: Any | None = None,
):
    """Create a JobWorker handler resolving connectors by account ID."""
    orchestrator = SyncOrchestrator(
        repository=repository, engagement_inbox=engagement_inbox,
        kpi_projector=kpi_projector, source_campaign_operator=source_campaign_operator,
        campaign_metric_projector=campaign_metric_projector,
        canonical_revalidation_store=canonical_revalidation_store,
        newsletter_lifecycle_projector=newsletter_lifecycle_projector,
    )

    def handle(job: dict[str, Any]) -> dict[str, Any]:
        payload = job["payload"]
        account_id = payload["connector_account_id"]
        connector = connectors.get(account_id)
        if connector is None:
            raise LookupError(f"no connector registered for account {account_id}")
        return orchestrator.sync_once(
            connector,
            brand_id=payload["brand_id"],
            connector_account_id=account_id,
            stream=payload["stream"],
        ).as_dict()

    return handle


def _source_payload(event: ConnectorEvent, connector_account_id: str) -> dict[str, Any]:
    payload = event.payload
    scheduled_for = payload.get("scheduled_for")
    status = str(payload.get("status") or "").lower()
    if event.connector == ConnectorKind.RSS or status in {"confirmed", "published", "sent"}:
        lifecycle = "published"
    elif scheduled_for or status == "scheduled":
        lifecycle = "scheduled"
    else:
        lifecycle = "draft"
    return {
        "title": str(payload.get("title") or "Untitled source"),
        "url": payload.get("url"),
        "source_type": event.connector.value,
        "body_summary": str(payload.get("summary") or payload.get("subtitle") or ""),
        "lifecycle_state": lifecycle,
        "scheduled_for": scheduled_for,
        # The normalized identity prevents collisions across feeds/accounts even
        # when a provider omits an ID or reuses a short GUID.
        "external_source_id": f"{connector_account_id}:{event.dedup_key}",
    }


def _performance_payload(event: ConnectorEvent, *, brand_id: str, notes: str) -> dict[str, Any]:
    payload = event.payload
    values = {name: 0 for name in ("impressions", "clicks", "engagements", "conversions", "revenue_cents")}
    for name in values:
        if name in payload:
            values[name] = int(payload[name])
    metric = str(payload.get("metric") or "")
    aliases = {
        "impression": "impressions",
        "click": "clicks",
        "engagement": "engagements",
        "conversion": "conversions",
        "newsletter_conversion": "conversions",
        "revenue": "revenue_cents",
    }
    column = aliases.get(metric, metric if metric in values else None)
    if column:
        values[column] = int(payload.get("value", values[column]))
    return {
        "brand_id": brand_id,
        "post_id": payload.get("post_id"),
        "source_id": payload.get("source_id"),
        "channel": event.connector.value,
        "observed_at": event.occurred_at or store.now(),
        **values,
        "notes": notes,
    }
