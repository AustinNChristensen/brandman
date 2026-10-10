"""Deterministic mission operating plans composed from canonical BrandMan state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
import json
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.mission_artifacts import END_OF_DAY_SCORECARD, MORNING_PLAN
from app.mission_ops import build_end_of_day_scorecard, build_morning_plan


class OperatingPlanRepository(Protocol):
    """Read seam implemented by the API/store integration layer."""

    def mission_progress(self, mission_id: str, as_of: str | None = None) -> Mapping[str, Any] | None: ...
    def scheduled_work(self, brand_id: str, plan_date: str) -> Sequence[Mapping[str, Any]]: ...
    def approval_queue(self, brand_id: str) -> Sequence[Mapping[str, Any]]: ...
    def connector_health(self, brand_id: str) -> Sequence[Mapping[str, Any]]: ...
    def recent_performance(self, brand_id: str) -> Sequence[Mapping[str, Any]]: ...
    def editorial_candidates(self, brand_id: str) -> Sequence[Mapping[str, Any]]: ...
    def open_gaps(self, brand_id: str) -> Sequence[Mapping[str, Any]]: ...
    def job_failures(self, brand_id: str) -> Sequence[Mapping[str, Any]]: ...


class ArtifactRepository(Protocol):
    def upsert(self, mission_id: str, artifact_date: str, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]: ...
    def previous_scorecard(self, mission_id: str, before_date: str) -> Mapping[str, Any] | None: ...


def _day(value: str | date | datetime) -> str:
    candidate = value.date().isoformat() if isinstance(value, datetime) else value.isoformat() if isinstance(value, date) else str(value)
    try:
        parsed = date.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("plan_date must be an ISO date (YYYY-MM-DD)") from exc
    if parsed.isoformat() != candidate:
        raise ValueError("plan_date must be an ISO date (YYYY-MM-DD)")
    return candidate


def _mission_day(value: str | date | datetime, timezone_name: str | None) -> str:
    """Resolve instants in the mission's civil timezone; preserve explicit dates."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and "T" not in value:
        return _day(value)
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    if parsed.tzinfo is None:
        raise ValueError("plan timestamp must include a timezone")
    try:
        mission_zone = ZoneInfo(timezone_name or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown mission timezone: {timezone_name}") from exc
    return parsed.astimezone(mission_zone).date().isoformat()


def _ids(*values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            for nested in value:
                if nested is not None and str(nested).strip() and str(nested) not in result:
                    result.append(str(nested))
        elif str(value).strip() and str(value) not in result:
            result.append(str(value))
    return result


def _metric_for_channel(progress: Mapping[str, Any], channel: str) -> str | None:
    metrics = [str(goal.get("metric", "")) for goal in progress.get("goals", [])]
    channel = channel.casefold()
    preferred = (
        ("x_followers",) if channel == "x"
        else ("active_beehiiv_subscribers", "active_subscribers") if channel in {"beehiiv", "newsletter"}
        else tuple(metrics)
    )
    return next((metric for metric in preferred if metric in metrics), metrics[0] if metrics else None)


def _contribution(metric: str | None, mechanism: str, *, confidence: str = "expected") -> dict[str, Any]:
    return {"metric": metric, "direction": "increase", "amount": None, "confidence": confidence, "mechanism": mechanism}


def _action(
    *, action_type: str, title: str, owner: str, reason: str, due_window: str,
    dependency: str | None, blocker: str | None, related_ids: Sequence[Any],
    contribution: Mapping[str, Any], priority: int,
) -> dict[str, Any]:
    return {
        "type": action_type, "title": title, "owner": owner, "reason": reason,
        "due_window": due_window, "dependency": dependency, "blocker": blocker,
        "related_ids": _ids(list(related_ids)),
        "expected_kpi_contribution": dict(contribution), "priority": priority,
    }


def build_prioritized_actions(
    progress: Mapping[str, Any], *, scheduled_work: Sequence[Mapping[str, Any]],
    approvals: Sequence[Mapping[str, Any]], connectors: Sequence[Mapping[str, Any]],
    performance: Sequence[Mapping[str, Any]], candidates: Sequence[Mapping[str, Any]],
    gaps: Sequence[Mapping[str, Any]], failures: Sequence[Mapping[str, Any]],
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Turn current operating state into a stable, explainable action queue."""

    actions: list[dict[str, Any]] = []
    for connector in connectors:
        if connector.get("required_for_operating_plan") is False:
            continue
        status = str(connector.get("status") or ("healthy" if connector.get("healthy") else "unhealthy")).casefold()
        if status not in {"healthy", "connected", "ok", "active"}:
            name = str(connector.get("connector_type") or connector.get("name") or "connector")
            actions.append(_action(
                action_type="restore_connector", title=f"Restore {name} connector", owner="Chris",
                reason="Mission execution and measurement cannot be trusted while this connector is unhealthy.",
                due_window="immediately", dependency="Valid least-privilege connection",
                blocker=str(connector.get("last_error") or connector.get("detail") or status),
                related_ids=_ids(connector.get("id"), connector.get("account_id")),
                contribution=_contribution(_metric_for_channel(progress, name), "restores publishing or measurement", confidence="indirect"),
                priority=0,
            ))

    grouped_failures: dict[tuple[str, str, str], dict[str, Any]] = {}
    for failure in failures:
        connector_id = str(
            failure.get("connector_account_id")
            or (failure.get("payload") or {}).get("connector_account_id")
            or ""
        )
        error = str(
            failure.get("last_error") or failure.get("error")
            or "Failure reason unavailable"
        )
        fingerprint = str(failure.get("failure_fingerprint") or error).casefold()
        key = (
            connector_id,
            str(failure.get("job_type") or failure.get("name") or "unknown"),
            fingerprint,
        )
        incident = grouped_failures.setdefault(key, {
            "sample": failure, "count": 0, "related_ids": [],
        })
        incident["count"] += int(failure.get("occurrence_count") or 1)
        incident["related_ids"] = _ids(
            incident["related_ids"], failure.get("id"), connector_id,
            failure.get("related_ids"), failure.get("related_job_ids"),
        )

    # Bound recurring incidents so editorial opportunities cannot be crowded out.
    for incident in list(grouped_failures.values())[:3]:
        failure = incident["sample"]
        count = incident["count"]
        actions.append(_action(
            action_type="resolve_job_failure", title=f"Resolve failed job: {failure.get('job_type') or failure.get('name') or 'unknown'}",
            owner="BrandMan", reason=(
                f"A recurring connector incident represents {count} failed durable operation(s)."
                if count > 1 else "A durable operation exhausted retries or needs attention."
            ),
            due_window="immediately", dependency="Connector health and reproducible job input",
            blocker=str(failure.get("last_error") or failure.get("error") or "Failure reason unavailable"),
            related_ids=incident["related_ids"],
            contribution=_contribution(_metric_for_channel(progress, str(failure.get("connector") or "")), "unblocks mission work", confidence="indirect"),
            priority=1,
        ))
        actions[-1]["incident_occurrence_count"] = count

    severity_order = {"critical": 0, "high": 1}
    for gap in gaps:
        severity = str(gap.get("severity") or "medium").casefold()
        if severity not in severity_order:
            continue
        actions.append(_action(
            action_type="close_product_gap", title=f"Close {severity} gap: {gap.get('summary') or gap.get('title') or 'unnamed gap'}",
            owner="BrandMan", reason="This known product gap can block or distort the 30-day mission.",
            due_window="today", dependency=str(gap.get("component") or "affected component"),
            blocker=str(gap.get("actual_behavior") or gap.get("details") or "Known gap"),
            related_ids=_ids(gap.get("id"), gap.get("related_ids")),
            contribution=_contribution(None, "removes an execution or measurement constraint", confidence="indirect"),
            priority=2 + severity_order[severity],
        ))

    for approval in approvals:
        if approval.get("kind") == "experiment_recommendation":
            actions.append(_action(
                action_type="review_experiment_winner",
                title="Review evidence-backed X experiment winner",
                owner="Chris",
                reason="BrandMan recommends a pattern but cannot accept or apply the learning.",
                due_window="this_morning",
                dependency="Connector-backed performance and experiment guardrails",
                blocker=None,
                related_ids=_ids(
                    approval.get("id"), approval.get("experiment_id"), approval.get("artifact_id"),
                ),
                contribution=_contribution(
                    _metric_for_channel(progress, "x"),
                    "allows a human-approved learning to inform future drafts",
                    confidence="evidence_backed",
                ),
                priority=4,
            ))
            continue
        channel = str(approval.get("channel") or approval.get("connector") or "content")
        actions.append(_action(
            action_type="review_approval", title=f"Review {channel} draft", owner="Chris",
            reason="Nothing public can execute until the exact revision is explicitly approved or rejected.",
            due_window="this_morning", dependency="Current revision and source verification",
            blocker=None,
            related_ids=_ids(approval.get("id"), approval.get("campaign_id"), approval.get("artifact_id"), approval.get("issue_id")),
            contribution=_contribution(_metric_for_channel(progress, channel), "releases approved content into the queue"),
            priority=4,
        ))

    approval_targets = {
        value
        for approval in approvals
        for value in _ids(approval.get("artifact_id"), approval.get("issue_id"), approval.get("post_id"))
    }
    for work in scheduled_work:
        status = str(work.get("status") or work.get("lifecycle") or "").casefold()
        # Terminal work can remain in the dated repository result so the EOD
        # scorecard can report genuine completion. It must never reappear as a
        # forward-looking preparation action merely because scheduled_for was
        # retained for audit/history.
        if status in {
            "published", "completed", "measured", "rejected", "cancelled",
            "stale", "archived", "abandoned",
        }:
            continue
        if any(value in approval_targets for value in _ids(work.get("id"), work.get("artifact_id"), work.get("issue_id"), work.get("post_id"))):
            # The approval action already carries the precise blocker and is the
            # next executable step; adding a generic preparation action would
            # duplicate work in the operator's brief.
            continue
        channel = str(work.get("channel") or work.get("connector") or work.get("kind") or "content")
        actions.append(_action(
            action_type="prepare_scheduled_work", title=f"Prepare scheduled {channel} item",
            owner="BrandMan", reason="Scheduled work must be ready and approved before its delivery window.",
            due_window=str(work.get("scheduled_for") or "today"),
            dependency="Explicit approval" if status in {"draft", "awaiting_approval"} else "Healthy delivery connector",
            blocker="Awaiting explicit approval" if status in {"draft", "awaiting_approval"} else None,
            related_ids=_ids(work.get("id"), work.get("campaign_id"), work.get("artifact_id"), work.get("issue_id")),
            contribution=_contribution(_metric_for_channel(progress, channel), "delivers scheduled campaign output"),
            priority=5,
        ))

    ranked_candidates = sorted(
        candidates, key=lambda item: (-float(item.get("score") or 0), str(item.get("id") or ""))
    )
    for candidate in ranked_candidates[:3]:
        actions.append(_action(
            action_type="develop_editorial_candidate", title=f"Develop editorial candidate: {candidate.get('title') or 'untitled'}",
            owner="BrandMan", reason=f"Candidate score {float(candidate.get('score') or 0):g} merits editorial review.",
            due_window="today", dependency="Source verification and editorial selection", blocker=None,
            related_ids=_ids(candidate.get("id"), candidate.get("supporting_source_ids"), [s.get("source_id") for s in candidate.get("supporting_sources", []) if isinstance(s, Mapping)]),
            contribution=_contribution(_metric_for_channel(progress, "newsletter"), "creates a source-grounded newsletter or campaign opportunity"),
            priority=6,
        ))

    winners = sorted(
        performance,
        key=lambda item: (
            -float(item.get("conversions") or 0), -float(item.get("clicks") or 0),
            -float(item.get("engagements") or 0), str(item.get("id") or ""),
        ),
    )
    if winners and any(float(winners[0].get(key) or 0) > 0 for key in ("conversions", "clicks", "engagements")):
        winner = winners[0]
        channel = str(winner.get("channel") or "content")
        actions.append(_action(
            action_type="reuse_winning_pattern", title="Adapt the strongest recent content pattern",
            owner="BrandMan", reason="Recent measured performance provides evidence for the next experiment.",
            due_window="next_24_hours", dependency="Preserve source grounding and create a distinct draft", blocker=None,
            related_ids=_ids(winner.get("id"), winner.get("post_id"), winner.get("campaign_id"), winner.get("source_id")),
            contribution=_contribution(_metric_for_channel(progress, channel), "reuses an evidence-backed hook or format", confidence="evidence_backed"),
            priority=7,
        ))

    actions.sort(key=lambda item: (item["priority"], item["due_window"], item["title"], item["related_ids"]))
    for index, action in enumerate(actions[:limit], start=1):
        action["rank"] = index
    return actions[:limit]


def build_operating_morning_plan(
    progress: Mapping[str, Any], *, plan_date: str,
    scheduled_work: Sequence[Mapping[str, Any]], approvals: Sequence[Mapping[str, Any]],
    connectors: Sequence[Mapping[str, Any]], performance: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]], gaps: Sequence[Mapping[str, Any]],
    failures: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result = build_morning_plan(progress, plan_date=plan_date)
    result["schema_version"] = 2
    result["actions"] = build_prioritized_actions(
        progress, scheduled_work=scheduled_work, approvals=approvals, connectors=connectors,
        performance=performance, candidates=candidates, gaps=gaps, failures=failures,
    )
    result["context_snapshot"] = {
        "scheduled_work": len(scheduled_work), "approvals_waiting": len(approvals),
        "unhealthy_connectors": sum(1 for item in connectors if item.get("required_for_operating_plan") is not False and str(item.get("status") or ("healthy" if item.get("healthy") else "unhealthy")).casefold() not in {"healthy", "connected", "ok", "active"}),
        "editorial_candidates": len(candidates),
        "high_severity_gaps": sum(1 for item in gaps if str(item.get("severity", "")).casefold() in {"high", "critical"}),
        "job_failures": len(failures), "recent_performance_records": len(performance),
    }
    result["blockers"] = [
        {"action_rank": action["rank"], "blocker": action["blocker"], "related_ids": action["related_ids"]}
        for action in result["actions"] if action["blocker"]
    ]
    json.dumps(result, allow_nan=False)
    return result


def build_operating_eod_scorecard(
    progress: Mapping[str, Any], *, scorecard_date: str,
    previous_progress: Mapping[str, Any] | None,
    scheduled_work: Sequence[Mapping[str, Any]], approvals: Sequence[Mapping[str, Any]],
    connectors: Sequence[Mapping[str, Any]], performance: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]], gaps: Sequence[Mapping[str, Any]],
    failures: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result = build_end_of_day_scorecard(
        progress, previous_progress=previous_progress, scorecard_date=scorecard_date
    )
    result["schema_version"] = 2
    result["completed_work"] = [
        {
            "id": str(item["id"]),
            "type": str(item.get("kind") or item.get("channel") or "work"),
            "outcome": str(item.get("status") or item.get("lifecycle")),
            "related_ids": _ids(item.get("id"), item.get("campaign_id"), item.get("artifact_id"), item.get("issue_id")),
        }
        for item in scheduled_work
        if item.get("id") and str(item.get("status") or item.get("lifecycle") or "").casefold() in {"published", "completed", "measured"}
    ]
    result["failures"] = [
        {
            "id": str(item.get("id") or ""), "job_type": str(item.get("job_type") or "unknown"),
            "error": str(item.get("last_error") or item.get("error") or "Failure reason unavailable"),
            "related_ids": _ids(item.get("id"), item.get("connector_account_id"), item.get("related_ids")),
        }
        for item in failures
    ]
    result["approvals_waiting"] = [
        {
            "id": str(item.get("id") or ""),
            "revision": item.get("revision"),
            "channel": item.get("channel") or item.get("connector"),
            "related_ids": _ids(item.get("id"), item.get("campaign_id"), item.get("issue_id")),
        }
        for item in approvals
    ]
    result["next_priorities"] = build_prioritized_actions(
        progress, scheduled_work=scheduled_work, approvals=approvals, connectors=connectors,
        performance=performance, candidates=candidates, gaps=gaps, failures=failures, limit=5,
    )
    json.dumps(result, allow_nan=False)
    return result


class OperatingPlanService:
    """Load canonical state and persist composed plans through mission artifacts."""

    def __init__(self, repository: OperatingPlanRepository, artifacts: ArtifactRepository) -> None:
        self.repository = repository
        self.artifacts = artifacts

    def _inputs(
        self, mission_id: str, plan_date: str, as_of: str | None, *,
        progress: Mapping[str, Any] | None = None,
    ) -> tuple[Mapping[str, Any], dict[str, Sequence[Mapping[str, Any]]]]:
        progress = progress or self.repository.mission_progress(mission_id, as_of)
        if progress is None:
            raise LookupError(f"mission not found: {mission_id}")
        brand_id = str(progress.get("brand_id") or "")
        if not brand_id:
            raise ValueError("mission progress must include brand_id")
        return progress, {
            "scheduled_work": self.repository.scheduled_work(brand_id, plan_date),
            "approvals": self.repository.approval_queue(brand_id),
            "connectors": self.repository.connector_health(brand_id),
            "performance": self.repository.recent_performance(brand_id),
            "candidates": self.repository.editorial_candidates(brand_id),
            "gaps": self.repository.open_gaps(brand_id),
            "failures": self.repository.job_failures(brand_id),
        }

    def create_morning_plan(
        self, mission_id: str, plan_date: str | date | datetime, *,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        progress = self.repository.mission_progress(mission_id, as_of)
        if progress is None:
            raise LookupError(f"mission not found: {mission_id}")
        day = _mission_day(plan_date, str(progress.get("timezone") or "UTC"))
        progress, inputs = self._inputs(mission_id, day, as_of, progress=progress)
        payload = build_operating_morning_plan(progress, plan_date=day, **inputs)
        return self.artifacts.upsert(mission_id, day, MORNING_PLAN, payload)

    def create_eod_scorecard(
        self, mission_id: str, plan_date: str | date | datetime, *,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        progress = self.repository.mission_progress(mission_id, as_of)
        if progress is None:
            raise LookupError(f"mission not found: {mission_id}")
        day = _mission_day(plan_date, str(progress.get("timezone") or "UTC"))
        progress, inputs = self._inputs(mission_id, day, as_of, progress=progress)
        previous = self.artifacts.previous_scorecard(mission_id, day)
        previous_payload = previous.get("payload") if previous else None
        payload = build_operating_eod_scorecard(
            progress, scorecard_date=day, previous_progress=previous_payload, **inputs
        )
        return self.artifacts.upsert(mission_id, day, END_OF_DAY_SCORECARD, payload)
