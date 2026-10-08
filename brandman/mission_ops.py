"""Pure mission operations and canonical campaign attribution helpers.

This module deliberately does not know about the database.  Its outputs contain
only JSON-persistable values so API, MCP, worker, and dashboard integrations can
all use the same calculations.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
import math
import re
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_cta", "utm_brand")


class AttributionConfidence(StrEnum):
    """Confidence that a conversion can be assigned to Brand OS content."""

    DIRECT = "direct"
    ASSISTED = "assisted"
    UNATTRIBUTED = "unattributed"


@dataclass(frozen=True, slots=True)
class UtmIdentity:
    """Canonical, persistable identity carried by a Brand OS link."""

    source: str
    medium: str
    campaign_id: str
    artifact_id: str
    cta_id: str
    brand_slug: str | None = None

    def as_params(self) -> dict[str, str]:
        params = {
            "utm_source": self.source,
            "utm_medium": self.medium,
            "utm_campaign": self.campaign_id,
            "utm_content": self.artifact_id,
            "utm_cta": self.cta_id,
        }
        if self.brand_slug:
            params["utm_brand"] = self.brand_slug
        return params

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _clean_number(value: float, digits: int = 4) -> int | float:
    rounded = round(value, digits)
    return int(rounded) if rounded.is_integer() else rounded


def _directional_delta(current: float, expected: float, direction: str) -> float:
    if direction == "increase":
        return current - expected
    if direction == "decrease":
        return expected - current
    raise ValueError("direction must be 'increase' or 'decrease'")


def _goal_reached(current: float, target: float, direction: str) -> bool:
    return current >= target if direction == "increase" else current <= target


def calculate_goal_trajectory(
    goal: Mapping[str, Any], *, tolerance: float = 1e-9, remaining_days: float | None = None
) -> dict[str, Any]:
    """Return explicit schedule and pace information for one mission goal.

    ``trajectory_amount`` is always positive when ahead and negative when behind,
    including for goals whose desired direction is a decrease.
    """

    current = _finite_number(goal.get("current", goal.get("baseline")), "current")
    target = _finite_number(goal["target"], "target")
    expected = _finite_number(goal.get("expected_current", current), "expected_current")
    direction = str(goal.get("direction", "increase"))
    delta = _directional_delta(current, expected, direction)
    reached = _goal_reached(current, target, direction)
    if reached:
        status = "achieved"
        status_text = f"Goal achieved by {_clean_number(abs(current - target))}"
    elif delta > tolerance:
        status = "ahead"
        status_text = f"Ahead of pace by {_clean_number(delta)}"
    elif delta < -tolerance:
        status = "behind"
        status_text = f"Behind pace by {_clean_number(abs(delta))}"
    else:
        status = "on_track"
        status_text = "On pace"

    days = remaining_days if remaining_days is not None else goal.get("remaining_days")
    if days is None:
        required_pace = goal.get("required_daily_change", 0)
    else:
        days_number = max(_finite_number(days, "remaining_days"), 0.0)
        if reached or days_number == 0:
            required_pace = 0.0
        else:
            required_pace = abs(target - current) / days_number

    return {
        "metric": str(goal.get("metric", "")),
        "status": status,
        "status_text": status_text,
        "trajectory_amount": _clean_number(delta),
        "current": _clean_number(current),
        "expected_current": _clean_number(expected),
        "target": _clean_number(target),
        "remaining": _clean_number(max(0.0, abs(target - current)) if not reached else 0.0),
        "required_daily_change": _clean_number(max(0.0, _finite_number(required_pace, "required_daily_change"))),
    }


def enrich_mission_progress(progress: Mapping[str, Any]) -> dict[str, Any]:
    """Add consistent trajectory fields to every goal without mutating input."""

    result = deepcopy(dict(progress))
    days = max(_finite_number(result.get("remaining_days", 0), "remaining_days"), 0.0)
    enriched_goals = []
    for original in result.get("goals", []):
        goal = dict(original)
        trajectory = calculate_goal_trajectory(goal, remaining_days=days)
        goal.update(trajectory)
        enriched_goals.append(goal)
    result["goals"] = enriched_goals
    return result


def _iso_day(value: str | date | datetime | None, *, fallback: str | None = None) -> str:
    if value is None:
        if fallback:
            return fallback[:10]
        return datetime.now(timezone.utc).date().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def build_morning_plan(
    progress: Mapping[str, Any], *, plan_date: str | date | datetime | None = None
) -> dict[str, Any]:
    """Build the deterministic KPI portion of a morning operating plan."""

    mission = enrich_mission_progress(progress)
    goals = []
    for goal in mission.get("goals", []):
        pace = _finite_number(goal["required_daily_change"], "required_daily_change")
        current = _finite_number(goal["current"], "current")
        direction = str(goal.get("direction", "increase"))
        target = _finite_number(goal["target"], "target")
        today_target = current + pace if direction == "increase" else current - pace
        today_target = min(today_target, target) if direction == "increase" else max(today_target, target)
        goals.append({
            "metric": goal.get("metric", ""),
            "current": goal["current"],
            "target": goal["target"],
            "trajectory_status": goal["status"],
            "trajectory_status_text": goal["status_text"],
            "trajectory_amount": goal["trajectory_amount"],
            "required_today": _clean_number(pace),
            "end_of_day_target": _clean_number(today_target),
        })
    return {
        "schema_version": 1,
        "kind": "morning_plan",
        "mission_id": mission.get("id"),
        "mission_name": mission.get("name"),
        "date": _iso_day(plan_date, fallback=str(mission.get("as_of", "")) or None),
        "remaining_days": _clean_number(_finite_number(mission.get("remaining_days", 0), "remaining_days")),
        "goals": goals,
    }


def build_end_of_day_scorecard(
    progress: Mapping[str, Any], *, previous_progress: Mapping[str, Any] | None = None,
    scorecard_date: str | date | datetime | None = None,
) -> dict[str, Any]:
    """Build a persistable daily result card, optionally including daily change."""

    mission = enrich_mission_progress(progress)
    prior_by_metric = {
        str(goal.get("metric")): goal for goal in (previous_progress or {}).get("goals", [])
    }
    goals = []
    for goal in mission.get("goals", []):
        metric = str(goal.get("metric", ""))
        prior = prior_by_metric.get(metric)
        change = None
        if prior is not None:
            change = _finite_number(goal["current"], "current") - _finite_number(prior.get("current"), "previous current")
        goals.append({
            "metric": metric,
            "current": goal["current"],
            "target": goal["target"],
            "daily_change": None if change is None else _clean_number(change),
            "trajectory_status": goal["status"],
            "trajectory_status_text": goal["status_text"],
            "trajectory_amount": goal["trajectory_amount"],
            "remaining": goal["remaining"],
            "required_daily_change": goal["required_daily_change"],
        })
    statuses = [goal["trajectory_status"] for goal in goals]
    overall = "behind" if "behind" in statuses else "ahead" if "ahead" in statuses else "on_track"
    if statuses and all(status == "achieved" for status in statuses):
        overall = "achieved"
    return {
        "schema_version": 1,
        "kind": "end_of_day_scorecard",
        "mission_id": mission.get("id"),
        "mission_name": mission.get("name"),
        "date": _iso_day(scorecard_date, fallback=str(mission.get("as_of", "")) or None),
        "overall_status": overall,
        "goals": goals,
    }


def _validate_identifier(value: str, name: str) -> str:
    cleaned = str(value).strip()
    if not _IDENTIFIER.fullmatch(cleaned):
        raise ValueError(f"{name} must be 1-128 URL-safe identifier characters")
    return cleaned


def build_utm_identity(
    *, source: str, medium: str, campaign_id: str, artifact_id: str, cta_id: str,
    brand_slug: str | None = None,
) -> UtmIdentity:
    """Validate and construct a canonical Brand OS attribution identity."""

    return UtmIdentity(
        source=_validate_identifier(source.lower(), "source"),
        medium=_validate_identifier(medium.lower(), "medium"),
        campaign_id=_validate_identifier(campaign_id, "campaign_id"),
        artifact_id=_validate_identifier(artifact_id, "artifact_id"),
        cta_id=_validate_identifier(cta_id, "cta_id"),
        brand_slug=_validate_identifier(brand_slug.lower(), "brand_slug") if brand_slug else None,
    )


def generate_utm_url(base_url: str, identity: UtmIdentity) -> str:
    """Return a canonical URL, replacing stale UTM values and preserving other query data."""

    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    existing = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key.lower() not in _UTM_KEYS]
    query = sorted(existing) + list(identity.as_params().items())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), ""))


def parse_utm_identity(url: str) -> dict[str, Any]:
    """Parse an attribution identity and report direct/assisted/unattributed confidence."""

    values = {key.lower(): value for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=False)}
    params = {key: values.get(key) for key in _UTM_KEYS}
    identity_fields = (params["utm_campaign"], params["utm_content"], params["utm_cta"])
    if all(identity_fields):
        confidence = AttributionConfidence.DIRECT
    elif any(identity_fields) or params["utm_source"] or params["utm_medium"]:
        confidence = AttributionConfidence.ASSISTED
    else:
        confidence = AttributionConfidence.UNATTRIBUTED
    return {
        "source": params["utm_source"],
        "medium": params["utm_medium"],
        "campaign_id": params["utm_campaign"],
        "artifact_id": params["utm_content"],
        "cta_id": params["utm_cta"],
        "brand_slug": params["utm_brand"],
        "confidence": confidence.value,
    }


def attribution_confidence(attribution: Mapping[str, Any] | None) -> AttributionConfidence:
    """Classify stored attribution fields without requiring a URL."""

    values = attribution or {}
    identity_fields = (values.get("campaign_id"), values.get("artifact_id"), values.get("cta_id"))
    if all(identity_fields):
        return AttributionConfidence.DIRECT
    if any(identity_fields) or values.get("source") or values.get("medium"):
        return AttributionConfidence.ASSISTED
    return AttributionConfidence.UNATTRIBUTED
