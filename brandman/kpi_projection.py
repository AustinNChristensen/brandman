"""Project trustworthy connector observations into mission KPI progress.

The projector is intentionally connector-agnostic infrastructure: sync persists a
normalized event first, then passes that canonical event row here.  Only a small
allow-list of provider/metric/evidence combinations can affect mission progress.
Unrecognized analytics remain available as performance data without silently
becoming a mission KPI.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import math
from typing import Any, Mapping

from brandman import store
from brandman.attribution_store import AttributionStore
from brandman.connectors import ConnectorEvent, ConnectorKind, EventKind


_SUBSCRIBER_ALIASES = frozenset({
    "active_beehiiv_subscribers",
    "active_subscribers",
    "newsletter_subscribers",
})
_BEEHIIV_EVIDENCE = frozenset({
    "beehiiv_subscriber_count",
    "beehiiv_publication_stats",
    "beehiiv_active_subscribers",
})
_WEBSITE_EVIDENCE = frozenset({
    "website_subscriber_count",
    "website_newsletter_subscribers",
})


@dataclass(frozen=True, slots=True)
class KpiProjectionResult:
    """Outcome for one connector event; blocked evidence is never promoted."""

    status: str
    metric: str | None = None
    missions_matched: int = 0
    promoted: int = 0
    evidence_record_ids: tuple[str, ...] = ()
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class MissionKpiProjector:
    """Promote verified count snapshots to matching active mission goals."""

    def __init__(
        self,
        attribution: AttributionStore,
        *,
        repository: Any = store,
        actor: str = "connector-kpi-projector",
    ) -> None:
        self.attribution = attribution
        self.repository = repository
        self.actor = actor

    def project(
        self,
        *,
        brand_id: str,
        connector_account_id: str,
        connector_event: Mapping[str, Any],
        event: ConnectorEvent,
    ) -> KpiProjectionResult:
        normalized = _normalized_kpi(event)
        if normalized is None:
            return KpiProjectionResult("ignored", reason="not_an_allowlisted_kpi_observation")
        metric, value = normalized

        account = self.repository.row(
            "SELECT * FROM connector_accounts WHERE id=?",
            (connector_account_id,),
        )
        if account is None:
            return KpiProjectionResult("blocked", metric=metric, reason="unknown_connector_account")
        if account["brand_id"] != brand_id or account["connector_type"] != event.connector.value:
            return KpiProjectionResult("blocked", metric=metric, reason="connector_identity_mismatch")
        if str(account["status"]).lower() not in {"healthy", "connected", "active"}:
            return KpiProjectionResult("blocked", metric=metric, reason="connector_not_verified")
        if connector_event.get("connector_account_id") != connector_account_id:
            return KpiProjectionResult("blocked", metric=metric, reason="event_identity_mismatch")
        connector_event_id = str(connector_event.get("id") or "").strip()
        if not connector_event_id:
            return KpiProjectionResult("blocked", metric=metric, reason="event_not_persisted")

        missions = self.repository.rows(
            """SELECT m.*, g.direction FROM missions m
               JOIN mission_goals g ON g.mission_id=m.id
               WHERE m.brand_id=? AND m.status='active' AND g.metric=?
               ORDER BY m.starts_at, m.id""",
            (brand_id, metric),
        )
        promoted = 0
        evidence_ids: list[str] = []
        blocked_reasons: list[str] = []
        for mission in missions:
            reason = self._promotion_block_reason(mission, metric, value, event.occurred_at)
            if reason:
                blocked_reasons.append(reason)
                continue
            dimensions = {
                "connector": event.connector.value,
                "connector_stream": connector_event.get("stream"),
                "connector_event_type": connector_event.get("event_type"),
                "provider_external_id": event.external_id,
                "provider_dedup_key": event.dedup_key,
                "evidence_type": event.payload.get("evidence_type"),
            }
            evidence = self.attribution.record_kpi_evidence(
                mission_id=mission["id"],
                metric=metric,
                value=value,
                observed_at=str(event.occurred_at),
                source=event.connector.value,
                connector_account_id=connector_account_id,
                connector_event_id=connector_event_id,
                dimensions=dimensions,
                idempotency_key=f"connector-kpi:{mission['id']}:{metric}:{connector_event_id}",
            )
            evidence_ids.append(evidence["id"])
            current = self.attribution.get_canonical_kpi(mission["id"], metric)
            if current and current["evidence_record_id"] == evidence["id"]:
                blocked_reasons.append("duplicate")
                continue
            canonical = self.attribution.promote_kpi_evidence(evidence["id"], actor=self.actor)
            self.repository.record_kpi_snapshot(
                mission["id"], metric, canonical["value"], canonical["observed_at"],
                canonical["source"],
                dimensions={
                    "evidence_record_id": evidence["id"],
                    "connector_account_id": connector_account_id,
                    "connector_event_id": connector_event_id,
                },
            )
            promoted += 1

        if promoted:
            return KpiProjectionResult(
                "promoted", metric, len(missions), promoted, tuple(evidence_ids)
            )
        if missions:
            reasons = sorted(set(blocked_reasons))
            status = "duplicate" if reasons == ["duplicate"] else "blocked"
            return KpiProjectionResult(
                status, metric, len(missions), 0, tuple(evidence_ids),
                ",".join(reasons) or "promotion_policy",
            )
        return KpiProjectionResult("ignored", metric=metric, reason="no_matching_active_goal")

    def _promotion_block_reason(
        self,
        mission: Mapping[str, Any],
        metric: str,
        value: float,
        observed_at: str | None,
    ) -> str | None:
        if not observed_at:
            return "missing_observed_at"
        observed = _aware_datetime(observed_at)
        starts = _aware_datetime(str(mission["starts_at"]))
        ends = _aware_datetime(str(mission["ends_at"]))
        if observed < starts or observed > ends:
            return "outside_mission_window"
        current = self.attribution.get_canonical_kpi(mission["id"], metric)
        if current is None:
            return None
        current_at = _aware_datetime(current["observed_at"])
        if observed < current_at:
            return "stale"
        if observed == current_at:
            return "duplicate" if value == current["value"] else "same_timestamp_conflict"
        direction = mission.get("direction", "increase")
        if direction == "increase" and value < current["value"]:
            return "regressive"
        if direction == "decrease" and value > current["value"]:
            return "regressive"
        return None


def _normalized_kpi(event: ConnectorEvent) -> tuple[str, float] | None:
    if event.kind not in {EventKind.METRIC_OBSERVED, EventKind.SUBSCRIBER_CHANGED}:
        return None
    metric = str(event.payload.get("metric") or "").strip().lower()
    evidence_type = str(event.payload.get("evidence_type") or "").strip().lower()
    if (
        event.connector == ConnectorKind.X
        and metric == "x_followers"
        and evidence_type == "x_profile_metrics"
    ):
        canonical_metric = "x_followers"
    elif (
        event.connector == ConnectorKind.BEEHIIV
        and metric in _SUBSCRIBER_ALIASES
        and evidence_type in _BEEHIIV_EVIDENCE
    ) or (
        event.connector == ConnectorKind.WEBSITE
        and metric in _SUBSCRIBER_ALIASES
        and evidence_type in _WEBSITE_EVIDENCE
    ):
        canonical_metric = "active_beehiiv_subscribers"
    else:
        return None
    value = event.payload.get("value")
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return canonical_metric, number


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("KPI timestamps must include a timezone")
    return parsed


def build_mission_kpi_projector(
    database: str,
    *,
    repository: Any = store,
    actor: str = "connector-kpi-projector",
) -> MissionKpiProjector:
    """Small composition hook for service runtimes and tests."""

    return MissionKpiProjector(
        AttributionStore(database), repository=repository, actor=actor
    )
