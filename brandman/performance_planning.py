"""Conservative, explainable performance priors for editorial planning.

Measured outcomes are observations, not instructions.  This module keeps native
channel denominators separate, time-decays old evidence, shrinks small samples,
caps influence, and records every retrieval.  It never changes content,
approvals, schedules, templates, or provider state.
"""
from __future__ import annotations

from datetime import UTC, datetime
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from uuid import uuid4

from . import store


_MAX_AGE_DAYS = 90.0
_HALF_LIFE_DAYS = 30.0
_MAX_SCORE_POINTS = 3.0
_MIN_MATCHED_OBSERVATIONS = 2
_MIN_EFFECTIVE_DENOMINATOR = 100.0
_PRIOR_DENOMINATOR = 500.0
_PROTECTED = [
    "canonical_brand_voice", "canonical_compliance", "brand_safety",
    "governed_sources", "exact_revision_approval", "tenant_privacy",
    "credential_safety", "destination_binding", "no_auto_publish",
]
_CONFIDENCE_WEIGHT = {
    "verified": 1.0, "high": 0.9, "medium": 0.7, "low": 0.45,
    "mixed": 0.45, "reported": 0.35,
    "reported_not_independently_verified": 0.35,
}


class PerformancePlanningEngine:
    """Compose recent outcomes into a bounded planning prior for one brand."""

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS performance_planning_audit (
                  id TEXT PRIMARY KEY,brand_id TEXT NOT NULL,scope_json TEXT NOT NULL,
                  as_of TEXT NOT NULL,status TEXT NOT NULL,included_ids_json TEXT NOT NULL,
                  excluded_json TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS performance_planning_audit_brand
                  ON performance_planning_audit(brand_id,created_at DESC);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def plan(
        self, brand_id: str, scope: Mapping[str, Any], *,
        as_of: str | datetime | None = None, audit: bool = True,
    ) -> dict[str, Any]:
        moment = _moment(as_of)
        normalized_scope = {
            key: str(value).strip().casefold()
            for key, value in scope.items()
            if key in {"stage", "channel", "topic", "template_key"}
            and str(value or "").strip() not in {"", "*"}
        }
        raw_observations = self._observations(brand_id)
        observations, duplicate_ids = _deduplicate(raw_observations)
        included: list[dict[str, Any]] = []
        compatible: list[dict[str, Any]] = []
        excluded = {"future": [], "stale": [], "incompatible_channel": [], "scope_mismatch": [],
                    "invalid_denominator": [], "invalid_timestamp": [],
                    "duplicate_measurement": duplicate_ids}
        channel = normalized_scope.get("channel")
        for item in observations:
            try:
                observed = _moment(item["observed_at"])
                age_days = (moment - observed).total_seconds() / 86_400
            except (TypeError, ValueError):
                excluded["invalid_timestamp"].append(item["id"])
                continue
            if age_days < 0:
                excluded["future"].append(item["id"])
                continue
            if age_days > _MAX_AGE_DAYS:
                excluded["stale"].append(item["id"])
                continue
            if channel and item["channel"].casefold() != channel:
                excluded["incompatible_channel"].append(item["id"])
                continue
            if item["denominator"] <= 0:
                excluded["invalid_denominator"].append(item["id"])
                continue
            item = dict(item)
            item["age_days"] = round(age_days, 4)
            item["decay_weight"] = round(math.pow(0.5, age_days / _HALF_LIFE_DAYS), 8)
            item["evidence_weight"] = round(
                item["decay_weight"] * _confidence(item["confidence"]), 8,
            )
            compatible.append(item)
            if not _scope_match(item, normalized_scope):
                excluded["scope_mismatch"].append(item["id"])
                continue
            included.append(item)

        result = self._summarize(
            brand_id, normalized_scope, moment, observations, compatible, included, excluded,
        )
        if audit:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO performance_planning_audit
                       (id,brand_id,scope_json,as_of,status,included_ids_json,excluded_json,result_json,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (str(uuid4()), brand_id, _json(normalized_scope), moment.isoformat(),
                     result["status"], _json(result["evidence"]["included_ids"]),
                     _json(result["evidence"]["excluded"]), _json(result), store.now()),
                )
        return result

    def audit(self, brand_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM performance_planning_audit WHERE brand_id=?
                   ORDER BY created_at DESC,id DESC LIMIT ?""", (brand_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in ("scope_json", "included_ids_json", "excluded_json", "result_json"):
                item[key.removesuffix("_json")] = json.loads(item.pop(key))
            result.append(item)
        return result

    def _observations(self, brand_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            tables = {
                row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            rows: list[dict[str, Any]] = []
            if {"campaign_asset_metric_observations", "campaign_asset_memberships", "campaigns"} <= tables:
                template_join = (
                    "LEFT JOIN campaign_template_instances ti ON ti.campaign_id=c.id"
                    if "campaign_template_instances" in tables else ""
                )
                template_select = "ti.template_key" if "campaign_template_instances" in tables else "NULL"
                topic_select = (
                    "(SELECT si.cluster_key FROM source_intelligence_records si "
                    "WHERE si.brand_id=o.brand_id AND si.source_id=c.source_id "
                    "ORDER BY si.updated_at DESC,si.id LIMIT 1)"
                    if "source_intelligence_records" in tables else "NULL"
                )
                native = connection.execute(
                    f"""SELECT o.*,m.channel,m.campaign_id,c.source_id,
                              {template_select} AS template_key,{topic_select} AS cluster_key
                       FROM campaign_asset_metric_observations o
                       JOIN campaign_asset_memberships m ON m.id=o.membership_id
                       JOIN campaigns c ON c.id=m.campaign_id AND c.brand_id=o.brand_id
                       {template_join}
                       WHERE o.brand_id=?
                       ORDER BY o.observed_at,o.id""", (brand_id,),
                ).fetchall()
                for row in native:
                    metrics = json.loads(row["native_metrics_json"] or "{}")
                    numerator, denominator, metric = _native_rate(row["channel"], metrics)
                    rows.append({
                        "id": f"native:{row['id']}", "record_id": row["id"],
                        "kind": "campaign_native", "channel": row["channel"],
                        "campaign_id": row["campaign_id"], "source_id": row["source_id"],
                        "template_key": row["template_key"], "topic": row["cluster_key"],
                        "observed_at": row["observed_at"], "metric": metric,
                        "numerator": numerator, "denominator": denominator,
                        "confidence": row["attribution_confidence"],
                    })
            if "performance_records" in tables:
                template_join = (
                    "LEFT JOIN campaign_template_instances ti ON ti.campaign_id=c.id"
                    if "campaign_template_instances" in tables else ""
                )
                template_select = "ti.template_key" if "campaign_template_instances" in tables else "NULL"
                topic_select = (
                    "(SELECT si.cluster_key FROM source_intelligence_records si "
                    "WHERE si.brand_id=r.brand_id AND si.source_id=COALESCE(r.source_id,c.source_id) "
                    "ORDER BY si.updated_at DESC,si.id LIMIT 1)"
                    if "source_intelligence_records" in tables else "NULL"
                )
                normalized = connection.execute(
                    f"""SELECT r.*,p.campaign_id,c.source_id AS campaign_source_id,
                              {template_select} AS template_key,{topic_select} AS cluster_key
                       FROM performance_records r
                       LEFT JOIN posts p ON p.id=r.post_id
                       LEFT JOIN campaigns c ON c.id=p.campaign_id AND c.brand_id=r.brand_id
                       {template_join}
                       WHERE r.brand_id=? AND NOT EXISTS (
                         SELECT 1 FROM fixture_quarantine_registry q
                         WHERE q.table_name='performance_records'
                           AND q.record_key_json=json_array(r.id)
                       ) ORDER BY r.observed_at,r.id""", (brand_id,),
                ).fetchall()
                for row in normalized:
                    numerator, denominator, metric = _normalized_rate(row)
                    rows.append({
                        "id": f"normalized:{row['id']}", "record_id": row["id"],
                        "kind": "normalized_performance", "channel": row["channel"],
                        "campaign_id": row["campaign_id"],
                        "source_id": row["source_id"] or row["campaign_source_id"],
                        "template_key": row["template_key"], "topic": row["cluster_key"],
                        "observed_at": row["observed_at"], "metric": metric,
                        "numerator": numerator, "denominator": denominator,
                        "confidence": "reported_not_independently_verified",
                    })
        # Stable ordering makes planning repeatable for an identical as-of snapshot.
        return sorted(rows, key=lambda item: (item["observed_at"], item["id"]))

    def _summarize(
        self, brand_id: str, scope: Mapping[str, str], moment: datetime,
        all_rows: list[dict[str, Any]], compatible: list[dict[str, Any]],
        included: list[dict[str, Any]], excluded: Mapping[str, list[str]],
    ) -> dict[str, Any]:
        recent_repetition = self._recent_repetition(brand_id, scope, moment)
        evidence = {
            "included_ids": sorted(item["id"] for item in included),
            "included_count": len(included), "compatible_count": len(compatible),
            "available_count": len(all_rows),
            "excluded": {key: sorted(values) for key, values in excluded.items()},
        }
        base = {
            "brand_id": brand_id, "scope": dict(scope), "as_of": moment.isoformat(),
            "policy": {
                "role": "bounded_prior_not_formula", "max_score_adjustment_points": _MAX_SCORE_POINTS,
                "half_life_days": _HALF_LIFE_DAYS, "maximum_age_days": _MAX_AGE_DAYS,
                "minimum_matched_observations": _MIN_MATCHED_OBSERVATIONS,
                "minimum_effective_denominator": _MIN_EFFECTIVE_DENOMINATOR,
                "channel_native_denominators_only": True,
                "protected_invariants": list(_PROTECTED),
                "side_effects": "read_and_audit_only; never approves, schedules, sends, or publishes",
            },
            "evidence": evidence, "repetition_guard": recent_repetition,
        }
        if not all_rows:
            return {**base, "status": "no_data", "metric": None,
                    "bounded_prior": _zero_prior("No measured outcomes exist for this brand."),
                    "explanation": ["No performance prior was applied; planning uses source evidence and governed learnings only."]}
        if not compatible:
            return {**base, "status": "no_compatible_data", "metric": None,
                    "bounded_prior": _zero_prior("No fresh channel-compatible outcome has a valid denominator."),
                    "explanation": ["Metrics from other channels were excluded rather than compared or summed."]}
        if not included:
            return {**base, "status": "no_scoped_evidence", "metric": compatible[0]["metric"],
                    "bounded_prior": _zero_prior("No fresh outcome matches the requested topic/template scope."),
                    "explanation": ["Unmatched performance cannot influence this plan."]}

        # Never mix unlike native rate semantics, even within a channel.
        metric = included[0]["metric"]
        same_metric = [item for item in included if item["metric"] == metric]
        baseline_rows = [item for item in compatible if item["metric"] == metric]
        metric_mismatch = [item["id"] for item in included if item["metric"] != metric]
        if metric_mismatch:
            evidence["excluded"]["incompatible_metric"] = sorted(metric_mismatch)
            evidence["included_ids"] = sorted(item["id"] for item in same_metric)
            evidence["included_count"] = len(same_metric)
        selected_num = sum(item["numerator"] * item["evidence_weight"] for item in same_metric)
        selected_den = sum(item["denominator"] * item["evidence_weight"] for item in same_metric)
        baseline_num = sum(item["numerator"] * item["evidence_weight"] for item in baseline_rows)
        baseline_den = sum(item["denominator"] * item["evidence_weight"] for item in baseline_rows)
        selected_rate = selected_num / selected_den if selected_den else 0.0
        baseline_rate = baseline_num / baseline_den if baseline_den else 0.0
        enough = len(same_metric) >= _MIN_MATCHED_OBSERVATIONS and selected_den >= _MIN_EFFECTIVE_DENOMINATOR
        if not enough:
            return {**base, "status": "insufficient_evidence", "metric": metric,
                    "estimate": _estimate(selected_rate, baseline_rate, selected_den, len(same_metric)),
                    "bounded_prior": _zero_prior("The scoped sample is below the anti-overfitting floor."),
                    "explanation": ["The evidence remains visible but has zero planning influence until the sample floor is met."]}

        shrunk = ((selected_rate * selected_den) + (baseline_rate * _PRIOR_DENOMINATOR)) / (
            selected_den + _PRIOR_DENOMINATOR
        )
        relative = (shrunk - baseline_rate) / max(abs(baseline_rate), 0.01)
        confidence_factor = min(1.0, selected_den / 1000.0) * min(1.0, len(same_metric) / 5.0)
        raw = max(-_MAX_SCORE_POINTS, min(_MAX_SCORE_POINTS, relative * _MAX_SCORE_POINTS * confidence_factor))
        fatigue_factor = recent_repetition["positive_prior_multiplier"] if raw > 0 else 1.0
        adjustment = round(raw * fatigue_factor, 2)
        confidence = "moderate" if confidence_factor >= 0.6 else "low"
        return {
            **base, "status": "applied", "metric": metric,
            "estimate": _estimate(selected_rate, baseline_rate, selected_den, len(same_metric), shrunk),
            "bounded_prior": {
                "score_adjustment_points": adjustment, "unfatigued_adjustment_points": round(raw, 2),
                "cap_points": _MAX_SCORE_POINTS, "confidence": confidence,
                "applies_to": "editorial ranking or template planning explanation only",
                "reason": "Time-decayed, confidence-weighted, baseline-shrunk channel-native evidence.",
            },
            "explanation": [
                f"{len(same_metric)} scoped {metric} observations were compared with the same-channel baseline.",
                "Small samples are shrunk toward baseline and cannot move ranking by more than three points.",
                "Recent repetition reduces only positive exploitation; deterministic exploration remains unchanged.",
            ],
        }

    def _recent_repetition(
        self, brand_id: str, scope: Mapping[str, str], moment: datetime,
    ) -> dict[str, Any]:
        topic, template = scope.get("topic"), scope.get("template_key")
        with self._connect() as connection:
            tables = {row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "campaigns" not in tables:
                count = 0
            else:
                template_join = (
                    "LEFT JOIN campaign_template_instances ti ON ti.campaign_id=c.id"
                    if "campaign_template_instances" in tables else ""
                )
                template_select = "ti.template_key" if "campaign_template_instances" in tables else "NULL"
                topic_select = (
                    "(SELECT si.cluster_key FROM source_intelligence_records si "
                    "WHERE si.brand_id=c.brand_id AND si.source_id=c.source_id "
                    "ORDER BY si.updated_at DESC,si.id LIMIT 1)"
                    if "source_intelligence_records" in tables else "NULL"
                )
                query = f"""SELECT c.created_at,{template_select} AS template_key,
                    {topic_select} AS cluster_key FROM campaigns c
                    {template_join} WHERE c.brand_id=?"""
                count = 0
                for row in connection.execute(query, (brand_id,)).fetchall():
                    try:
                        age = (moment - _moment(row["created_at"])).total_seconds() / 86_400
                    except (TypeError, ValueError):
                        continue
                    if not 0 <= age <= 30:
                        continue
                    if template and str(row["template_key"] or "").casefold() != template:
                        continue
                    if topic and str(row["cluster_key"] or "").casefold() != topic:
                        continue
                    count += 1
        multiplier = 1.0 if count < 2 else 0.5 if count == 2 else 0.0
        return {
            "matching_campaigns_last_30_days": count,
            "positive_prior_multiplier": multiplier,
            "rule": "third recent repeat receives no positive performance boost",
            "does_not_change_exploration_bucket": True,
        }


def _scope_match(item: Mapping[str, Any], scope: Mapping[str, str]) -> bool:
    for key in ("channel", "topic", "template_key"):
        required = scope.get(key)
        if required and str(item.get(key) or "").casefold() != required:
            return False
    return True


def _deduplicate(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Prefer native campaign observations over duplicate normalized projections."""
    ordered = sorted(
        rows,
        key=lambda item: (
            item["observed_at"], item.get("campaign_id") or item["id"], item["channel"],
            0 if item["kind"] == "campaign_native" else 1, item["id"],
        ),
    )
    unique, duplicates, seen = [], [], set()
    for item in ordered:
        campaign = item.get("campaign_id")
        key = (campaign, item["channel"], item["observed_at"], item["metric"])
        if campaign and key in seen:
            duplicates.append(item["id"])
            continue
        if campaign:
            seen.add(key)
        unique.append(item)
    return unique, sorted(duplicates)


def _native_rate(channel: str, metrics: Mapping[str, Any]) -> tuple[int, int, str]:
    choices = {
        "newsletter": ("clicks", "delivered", "click_rate"),
        "email": ("clicks", "delivered", "click_rate"),
        "web": ("clicks", "sessions", "click_rate"),
        "youtube": ("clicks", "views", "click_rate"),
        "x": ("clicks", "impressions", "click_rate"),
    }
    numerator, denominator, metric = choices.get(channel, ("clicks", "impressions", "click_rate"))
    return int(metrics.get(numerator, 0)), int(metrics.get(denominator, 0)), metric


def _normalized_rate(row: Mapping[str, Any]) -> tuple[int, int, str]:
    return int(row["clicks"]), int(row["impressions"]), "click_rate"


def _confidence(value: Any) -> float:
    normalized = str(value or "").strip().casefold()
    return _CONFIDENCE_WEIGHT.get(normalized, 0.25)


def _moment(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("as_of and observed_at must be ISO-8601 timestamps") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("as_of and observed_at must include a timezone")
    return parsed.astimezone(UTC)


def _zero_prior(reason: str) -> dict[str, Any]:
    return {
        "score_adjustment_points": 0.0, "unfatigued_adjustment_points": 0.0,
        "cap_points": _MAX_SCORE_POINTS, "confidence": "none",
        "applies_to": "nothing", "reason": reason,
    }


def _estimate(
    selected_rate: float, baseline_rate: float, denominator: float,
    observations: int, shrunk_rate: float | None = None,
) -> dict[str, Any]:
    return {
        "scoped_rate": round(selected_rate, 8), "same_channel_baseline_rate": round(baseline_rate, 8),
        "shrunk_rate": round(shrunk_rate, 8) if shrunk_rate is not None else None,
        "effective_denominator": round(denominator, 4), "observation_count": observations,
    }


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


__all__ = ["PerformancePlanningEngine"]
