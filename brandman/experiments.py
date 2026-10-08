"""Governed, evidence-backed experiments for source-grounded X drafts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from uuid import uuid4

from brandman import store
from brandman.principals import is_privileged
from brandman.learning_engine import BrandLearningEngine
from brandman.source_campaign import source_grounded_x_draft


class ExperimentError(ValueError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS content_experiments (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL REFERENCES brands(id),
  campaign_id TEXT NOT NULL REFERENCES campaigns(id), source_id TEXT NOT NULL REFERENCES sources(id),
  hypothesis TEXT NOT NULL, metric TEXT NOT NULL, guardrails_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', accepted_recommendation_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_content_experiment_per_brand
  ON content_experiments(brand_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS content_experiment_variants (
  id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES content_experiments(id),
  variant_key TEXT NOT NULL, post_id TEXT NOT NULL REFERENCES posts(id),
  rationale TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(experiment_id,variant_key), UNIQUE(experiment_id,post_id)
);
CREATE TABLE IF NOT EXISTS content_experiment_recommendations (
  id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES content_experiments(id),
  measurement_window_id TEXT,
  status TEXT NOT NULL, winner_variant_id TEXT, metric TEXT NOT NULL,
  evidence_fingerprint TEXT NOT NULL, evidence_json TEXT NOT NULL,
  rationale TEXT NOT NULL, learning_id TEXT, accepted_by TEXT, accepted_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(experiment_id,evidence_fingerprint)
);
CREATE TABLE IF NOT EXISTS content_experiment_measurement_windows (
  id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL REFERENCES content_experiments(id),
  window_key TEXT NOT NULL,
  metric TEXT NOT NULL,
  opens_at TEXT NOT NULL,
  closes_at TEXT NOT NULL,
  evaluate_at TEXT NOT NULL,
  late_evidence_until TEXT NOT NULL,
  freshness_seconds INTEGER NOT NULL,
  retry_interval_seconds INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'scheduled',
  evidence_state TEXT NOT NULL DEFAULT 'not_collected',
  collection_round INTEGER NOT NULL DEFAULT 0,
  collection_requested_at TEXT,
  last_evaluated_at TEXT,
  evidence_fingerprint TEXT,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  recommendation_id TEXT,
  has_late_evidence INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(experiment_id,window_key)
);
CREATE TABLE IF NOT EXISTS content_experiment_measurement_events (
  id TEXT PRIMARY KEY,
  measurement_window_id TEXT NOT NULL REFERENCES content_experiment_measurement_windows(id),
  event_key TEXT NOT NULL,
  event_type TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(measurement_window_id,event_key)
);
CREATE INDEX IF NOT EXISTS experiment_measurement_windows_due
  ON content_experiment_measurement_windows(status,evaluate_at,late_evidence_until);
CREATE TRIGGER IF NOT EXISTS experiment_measurement_window_definition_immutable
BEFORE UPDATE OF experiment_id,window_key,metric,opens_at,closes_at,evaluate_at,
                 late_evidence_until,freshness_seconds,retry_interval_seconds
ON content_experiment_measurement_windows
BEGIN
  SELECT RAISE(ABORT, 'experiment measurement window definition is immutable');
END;
"""

_METRICS = {"impressions", "clicks", "engagements", "conversions"}
_GUARDRAILS = {"min_observations_per_variant", "min_impressions_per_variant"}
EXPERIMENT_WINDOW_COLLECT_JOB_TYPE = "experiment.measurement.collect"
EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE = "experiment.measurement.evaluate"
_WINDOW_TERMINAL_STATES = frozenset({"evaluated", "closed"})


class ExperimentStore:
    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute(
                "PRAGMA table_info(content_experiment_recommendations)"
            )}
            if "measurement_window_id" not in columns:
                connection.execute(
                    "ALTER TABLE content_experiment_recommendations ADD COLUMN measurement_window_id TEXT"
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def draft(
        self, *, brand_id: str, campaign_id: str, hypothesis: str, metric: str,
        guardrails: Mapping[str, Any] | None = None,
        measurement_windows: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if metric not in _METRICS:
            raise ExperimentError(f"metric must be one of {', '.join(sorted(_METRICS))}")
        if not hypothesis.strip():
            raise ExperimentError("hypothesis is required")
        normalized_guardrails = _guardrails(guardrails)
        timestamp = store.now()
        normalized_windows = _measurement_windows(measurement_windows, timestamp)
        experiment_id = str(uuid4())
        with self._connect() as connection:
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE id=? AND brand_id=?", (campaign_id, brand_id),
            ).fetchone()
            if campaign is None:
                raise ExperimentError("campaign does not belong to the brand")
            if not campaign["source_id"]:
                raise ExperimentError("a source-grounded campaign is required")
            source = connection.execute(
                "SELECT * FROM sources WHERE id=? AND brand_id=?", (campaign["source_id"], brand_id),
            ).fetchone()
            if source is None:
                raise ExperimentError("campaign source was not found")
            try:
                connection.execute(
                    """INSERT INTO content_experiments
                       (id,brand_id,campaign_id,source_id,hypothesis,metric,guardrails_json,
                        status,accepted_recommendation_id,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,'active',NULL,?,?)""",
                    (experiment_id, brand_id, campaign_id, source["id"], hypothesis.strip(), metric,
                     json.dumps(normalized_guardrails, sort_keys=True), timestamp, timestamp),
                )
            except sqlite3.IntegrityError as exc:
                raise ExperimentError("the brand already has an active experiment") from exc
            variants = (
                ("control", source_grounded_x_draft(
                    title=source["title"], summary=source["body_summary"], url=source["url"], owned=False,
                ), "Lead with the source title and summary."),
                ("source_update", _source_update_draft(source),
                 "Lead with an explicit source-update frame while preserving the same facts."),
            )
            for key, body, rationale in variants:
                variant_id = _stable_id("experiment-variant", experiment_id, key)
                post_id = _stable_id("experiment-post", experiment_id, key)
                connection.execute(
                    """INSERT INTO posts
                       (id,campaign_id,channel,body,status,scheduled_for,external_post_id,created_at,updated_at)
                       VALUES (?,?,'x',?,'draft',NULL,NULL,?,?)""",
                    (post_id, campaign_id, body, timestamp, timestamp),
                )
                connection.execute(
                    """INSERT INTO content_experiment_variants
                       (id,experiment_id,variant_key,post_id,rationale,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (variant_id, experiment_id, key, post_id, rationale, timestamp),
                )
            for window in normalized_windows:
                window_id = _stable_id("experiment-window", experiment_id, window["window_key"])
                connection.execute(
                    """INSERT INTO content_experiment_measurement_windows
                       (id,experiment_id,window_key,metric,opens_at,closes_at,evaluate_at,
                        late_evidence_until,freshness_seconds,retry_interval_seconds,status,
                        evidence_state,collection_round,collection_requested_at,last_evaluated_at,
                        evidence_fingerprint,evidence_json,recommendation_id,has_late_evidence,
                        created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,'scheduled','not_collected',0,NULL,NULL,NULL,
                               '[]',NULL,0,?,?)""",
                    (window_id, experiment_id, window["window_key"], metric,
                     window["opens_at"], window["closes_at"], window["evaluate_at"],
                     window["late_evidence_until"], window["freshness_seconds"],
                     window["retry_interval_seconds"], timestamp, timestamp),
                )
                _enqueue_job(
                    connection, EXPERIMENT_WINDOW_COLLECT_JOB_TYPE,
                    f"experiment-window:{window_id}:collect:1",
                    {"experiment_id": experiment_id, "measurement_window_id": window_id,
                     "collection_round": 1},
                    brand_id=brand_id, run_after=window["evaluate_at"],
                )
        return self.get(experiment_id)

    def get(self, experiment_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM content_experiments WHERE id=?", (experiment_id,)).fetchone()
            if row is None:
                raise KeyError("experiment not found")
            variants = connection.execute(
                """SELECT v.*,p.body,p.status AS post_status,p.scheduled_for,p.external_post_id
                   FROM content_experiment_variants v JOIN posts p ON p.id=v.post_id
                   WHERE v.experiment_id=? ORDER BY v.variant_key""", (experiment_id,),
            ).fetchall()
            recommendations = connection.execute(
                "SELECT * FROM content_experiment_recommendations WHERE experiment_id=? ORDER BY created_at DESC,id DESC",
                (experiment_id,),
            ).fetchall()
            windows = connection.execute(
                """SELECT * FROM content_experiment_measurement_windows
                   WHERE experiment_id=? ORDER BY evaluate_at,window_key""", (experiment_id,),
            ).fetchall()
        result = dict(row)
        result["guardrails"] = json.loads(result.pop("guardrails_json"))
        result["variants"] = [dict(item) for item in variants]
        result["recommendations"] = [_decode_recommendation(item) for item in recommendations]
        result["measurement_windows"] = [_decode_window(item) for item in windows]
        return result

    def get_window(self, window_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM content_experiment_measurement_windows WHERE id=?", (window_id,),
            ).fetchone()
            if row is None:
                raise KeyError("experiment measurement window not found")
            events = connection.execute(
                """SELECT * FROM content_experiment_measurement_events
                   WHERE measurement_window_id=? ORDER BY created_at,id""", (window_id,),
            ).fetchall()
        result = _decode_window(row)
        result["events"] = [_decode_event(item) for item in events]
        return result

    def list_windows(self, *, experiment_id: str | None = None,
                     brand_id: str | None = None) -> list[dict[str, Any]]:
        if bool(experiment_id) == bool(brand_id):
            raise ValueError("provide exactly one of experiment_id or brand_id")
        with self._connect() as connection:
            if experiment_id:
                rows = connection.execute(
                    """SELECT w.id FROM content_experiment_measurement_windows w
                       WHERE w.experiment_id=? ORDER BY w.evaluate_at,w.window_key""",
                    (experiment_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT w.id FROM content_experiment_measurement_windows w
                       JOIN content_experiments e ON e.id=w.experiment_id
                       WHERE e.brand_id=? ORDER BY w.evaluate_at,w.window_key""", (brand_id,),
                ).fetchall()
        return [self.get_window(row["id"]) for row in rows]

    def record_collection_request(
        self, window_id: str, *, collection_round: int, connector_account_ids: list[str],
        as_of: str | None = None,
    ) -> dict[str, Any]:
        timestamp = _iso(as_of or store.now())
        if collection_round < 1:
            raise ExperimentError("collection_round must be positive")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            window = connection.execute(
                """SELECT w.*,e.brand_id FROM content_experiment_measurement_windows w
                   JOIN content_experiments e ON e.id=w.experiment_id WHERE w.id=?""", (window_id,),
            ).fetchone()
            if window is None:
                raise KeyError("experiment measurement window not found")
            if window["status"] in _WINDOW_TERMINAL_STATES:
                return self.get_window(window_id)
            effective_round = max(int(window["collection_round"]), collection_round)
            connection.execute(
                """UPDATE content_experiment_measurement_windows
                   SET status='collecting',collection_round=?,collection_requested_at=?,updated_at=?
                   WHERE id=?""", (effective_round, timestamp, timestamp, window_id),
            )
            _record_window_event(
                connection, window_id, f"collection:{collection_round}", "collection_requested",
                {"collection_round": collection_round,
                 "connector_account_ids": sorted(set(connector_account_ids))}, timestamp,
            )
            for account_id in sorted(set(connector_account_ids)):
                account = connection.execute(
                    """SELECT id FROM connector_accounts
                       WHERE id=? AND brand_id=? AND connector_type='x'
                         AND status IN ('healthy','connected')""",
                    (account_id, window["brand_id"]),
                ).fetchone()
                if account is None:
                    continue
                _enqueue_job(
                    connection, "connector.sync",
                    f"experiment-window:{window_id}:collect:{collection_round}:x:{account_id}",
                    {"brand_id": window["brand_id"], "connector_account_id": account_id,
                     "stream": "metrics"}, brand_id=window["brand_id"],
                    connector_account_id=account_id, run_after=timestamp, priority=10,
                )
            _enqueue_job(
                connection, EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE,
                f"experiment-window:{window_id}:evaluate:{collection_round}",
                {"experiment_id": window["experiment_id"], "measurement_window_id": window_id,
                 "collection_round": collection_round},
                # Give cursor-chained metric sync jobs one bounded minute to
                # finish before evaluating the snapshot they just collected.
                brand_id=window["brand_id"],
                run_after=(datetime.fromisoformat(timestamp) + timedelta(seconds=60)).isoformat(),
                priority=0,
            )
        return self.get_window(window_id)

    def evaluate_window(self, window_id: str, *, as_of: str | None = None) -> dict[str, Any]:
        timestamp = _iso(as_of or store.now())
        window = self.get_window(window_id)
        if datetime.fromisoformat(timestamp) < datetime.fromisoformat(window["evaluate_at"]):
            raise ExperimentError("measurement window cannot be evaluated before evaluate_at")
        experiment = self.get(window["experiment_id"])
        if experiment["status"] != "active" or window["status"] in _WINDOW_TERMINAL_STATES:
            return self.get_window(window_id)
        evidence, diagnostics = self._window_evidence(experiment, window)
        guardrails = experiment["guardrails"]
        sufficient = all(
            item["observation_count"] >= guardrails["min_observations_per_variant"]
            and item["impressions"] >= guardrails["min_impressions_per_variant"]
            for item in evidence
        )
        scores = [item["metric_value"] for item in evidence]
        decisive = sufficient and scores.count(max(scores)) == 1
        has_in_window = any(item["observation_count"] for item in evidence)
        evidence_state = ("sufficient" if decisive else "tie") if sufficient else (
            "partial" if has_in_window else (
                "late_rejected" if diagnostics["too_late_record_ids"] else
                ("stale" if diagnostics["stale_record_ids"] else "missing")
            )
        )
        fingerprint = sha256(json.dumps(
            {"window_id": window_id, "evidence": evidence},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        now_value = datetime.fromisoformat(timestamp)
        deadline = datetime.fromisoformat(window["late_evidence_until"])
        terminal = decisive or now_value >= deadline
        status = "evaluated" if decisive else ("closed" if terminal else "awaiting_evidence")
        recommendation_id = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """SELECT w.status,w.collection_round,e.status AS experiment_status
                   FROM content_experiment_measurement_windows w
                   JOIN content_experiments e ON e.id=w.experiment_id WHERE w.id=?""",
                (window_id,),
            ).fetchone()
            if current is None:
                raise KeyError("experiment measurement window not found")
            if (
                current["status"] in _WINDOW_TERMINAL_STATES
                or current["experiment_status"] != "active"
            ):
                return self.get_window(window_id)
            if sufficient:
                recommendation_id = self._store_recommendation(
                    connection, experiment, evidence, fingerprint, measurement_window_id=window_id,
                )
            connection.execute(
                """UPDATE content_experiment_measurement_windows
                   SET status=?,evidence_state=?,last_evaluated_at=?,evidence_fingerprint=?,
                       evidence_json=?,recommendation_id=?,has_late_evidence=?,updated_at=?
                   WHERE id=?""",
                (status, evidence_state, timestamp, fingerprint,
                 json.dumps(evidence, sort_keys=True), recommendation_id,
                 int(diagnostics["has_late_evidence"]), timestamp, window_id),
            )
            event_key = f"evaluation:{int(current['collection_round'])}:{fingerprint}"
            _record_window_event(
                connection, window_id, event_key, "window_evaluated",
                {"status": status, "evidence_state": evidence_state,
                 "has_late_evidence": diagnostics["has_late_evidence"],
                 "stale_record_ids": diagnostics["stale_record_ids"],
                 "too_late_record_ids": diagnostics["too_late_record_ids"],
                 "recommendation_id": recommendation_id}, timestamp,
            )
            if not terminal:
                # Round 1 is created with the experiment. Direct/operator
                # evaluation before that job runs must not collide with it.
                next_round = max(1, int(current["collection_round"])) + 1
                retry_at = min(
                    now_value + timedelta(seconds=int(window["retry_interval_seconds"])), deadline,
                ).isoformat()
                _enqueue_job(
                    connection, EXPERIMENT_WINDOW_COLLECT_JOB_TYPE,
                    f"experiment-window:{window_id}:collect:{next_round}",
                    {"experiment_id": experiment["id"], "measurement_window_id": window_id,
                     "collection_round": next_round}, brand_id=experiment["brand_id"],
                    run_after=retry_at,
                )
        if terminal and not sufficient:
            from brandman.operational_feedback import report_stale_metric
            report_stale_metric(
                brand_id=experiment["brand_id"], metric=experiment["metric"],
                evidence_window=window["window_key"],
                related_ids=[experiment["id"], window_id,
                             *[variant["post_id"] for variant in experiment["variants"]]],
            )
        return self.get_window(window_id)

    def list(self, brand_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            ids = [row["id"] for row in connection.execute(
                "SELECT id FROM content_experiments WHERE brand_id=? ORDER BY created_at DESC,id DESC",
                (brand_id,),
            )]
        return [self.get(experiment_id) for experiment_id in ids]

    def recommend(self, experiment_id: str) -> dict[str, Any]:
        experiment = self.get(experiment_id)
        if experiment["status"] != "active":
            raise ExperimentError("only an active experiment can be evaluated")
        windows = experiment.get("measurement_windows") or []
        if windows:
            # A manual review request may observe or trigger a due governed
            # window, but cannot fall back to unbounded all-time evidence.
            for window in reversed(windows):
                if window.get("recommendation_id"):
                    recommendation_id = window["recommendation_id"]
                    return next(
                        item for item in experiment["recommendations"]
                        if item["id"] == recommendation_id
                    )
            now_value = datetime.fromisoformat(_iso(store.now()))
            due = [
                window for window in windows
                if datetime.fromisoformat(window["evaluate_at"]) <= now_value
                and window["status"] not in _WINDOW_TERMINAL_STATES
            ]
            if not due:
                raise ExperimentError(
                    "no configured measurement window is due with a reviewable recommendation"
                )
            evaluated = self.evaluate_window(due[-1]["id"], as_of=now_value.isoformat())
            if not evaluated.get("recommendation_id"):
                raise ExperimentError(
                    f"measurement window evidence is {evaluated['evidence_state']}; no winner is reviewable"
                )
            refreshed = self.get(experiment_id)
            return next(
                item for item in refreshed["recommendations"]
                if item["id"] == evaluated["recommendation_id"]
            )
        evidence = self._evidence(experiment)
        fingerprint = sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        guardrails = experiment["guardrails"]
        sufficient = all(
            item["observation_count"] >= guardrails["min_observations_per_variant"]
            and item["impressions"] >= guardrails["min_impressions_per_variant"]
            for item in evidence
        )
        with self._connect() as connection:
            recommendation_id = self._store_recommendation(
                connection, experiment, evidence, fingerprint,
                sufficient_override=sufficient,
            )
        return next(item for item in self.get(experiment_id)["recommendations"] if item["id"] == recommendation_id)

    def accept(self, experiment_id: str, recommendation_id: str, *, actor: str) -> dict[str, Any]:
        if not is_privileged(actor):
            raise PermissionError("only the authenticated human principal may accept an experiment winner")
        timestamp = store.now()
        learning_engine = BrandLearningEngine(self.database)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            experiment = connection.execute(
                "SELECT * FROM content_experiments WHERE id=?", (experiment_id,),
            ).fetchone()
            if experiment is None:
                raise KeyError("experiment not found")
            if experiment["status"] == "completed":
                accepted = connection.execute(
                    """SELECT * FROM content_experiment_recommendations
                       WHERE id=? AND experiment_id=?""", (recommendation_id, experiment_id),
                ).fetchone()
                if (
                    experiment["accepted_recommendation_id"] == recommendation_id
                    and accepted is not None and accepted["status"] == "accepted"
                    and accepted["accepted_by"] == actor and accepted["learning_id"]
                ):
                    return self.get(experiment_id)
                raise ExperimentError(
                    "completed experiment is bound to a different accepted recommendation"
                )
            latest = connection.execute(
                """SELECT * FROM content_experiment_recommendations
                   WHERE experiment_id=? ORDER BY created_at DESC,id DESC LIMIT 1""",
                (experiment_id,),
            ).fetchone()
            if latest is None or latest["id"] != recommendation_id:
                raise ExperimentError("only the latest experiment recommendation can be accepted")
            if experiment["status"] != "active" or latest["status"] != "recommended" or not latest["winner_variant_id"]:
                raise ExperimentError("a current evidence-backed winner recommendation is required")
            winner = connection.execute(
                "SELECT * FROM content_experiment_variants WHERE id=? AND experiment_id=?",
                (latest["winner_variant_id"], experiment_id),
            ).fetchone()
            assert winner is not None
            evidence = json.loads(latest["evidence_json"] or "[]")
            window = None
            if latest["measurement_window_id"]:
                window = connection.execute(
                    "SELECT * FROM content_experiment_measurement_windows WHERE id=?",
                    (latest["measurement_window_id"],),
                ).fetchone()
            observation_count = sum(int(item.get("observation_count", 0)) for item in evidence)
            review_at = (datetime.fromisoformat(timestamp) + timedelta(days=90)).isoformat()
            learning_id = learning_engine.propose_in_transaction(
                connection, experiment["brand_id"], hypothesis=experiment["hypothesis"],
                proposed_change=(
                    f"Test whether variant pattern {winner['variant_key']} should become a bounded "
                    "future drafting prior; do not modify or publish existing content automatically."
                ),
                evidence_for=[{
                    "summary": (
                        f"Human accepted experiment {experiment_id} winner recommendation "
                        f"{recommendation_id} for governed follow-up testing."
                    ),
                    "experiment_id": experiment_id, "recommendation_id": recommendation_id,
                    "measurement_window_id": latest["measurement_window_id"],
                    "campaign_id": experiment["campaign_id"], "metric": experiment["metric"],
                    "sample_size": observation_count,
                    "observation_period": ({
                        "opens_at": window["opens_at"], "closes_at": window["closes_at"],
                    } if window is not None else None),
                    "artifacts": [item.get("post_id") for item in evidence if item.get("post_id")],
                }],
                evidence_against=[{
                    "summary": "A single experiment recommendation may not generalize; retain exploration.",
                }],
                effect={
                    "winner_variant_key": winner["variant_key"], "metric": experiment["metric"],
                    "variant_scores": {item["variant_key"]: item["metric_value"] for item in evidence},
                },
                uncertainty={
                    "guardrails": json.loads(experiment["guardrails_json"]),
                    "status": "requires_testing_transition_and_separate_human_acceptance",
                },
                scope={"channel": "x"}, review_at=review_at, actor=actor,
            )
            connection.execute(
                """UPDATE content_experiment_recommendations SET status='accepted',learning_id=?,
                   accepted_by=?,accepted_at=?,updated_at=? WHERE id=?""",
                (learning_id, actor, timestamp, timestamp, recommendation_id),
            )
            connection.execute(
                """UPDATE content_experiments SET status='completed',accepted_recommendation_id=?,updated_at=?
                   WHERE id=?""", (recommendation_id, timestamp, experiment_id),
            )
            pending_windows = connection.execute(
                """SELECT id FROM content_experiment_measurement_windows
                   WHERE experiment_id=? AND status NOT IN ('evaluated','closed')""",
                (experiment_id,),
            ).fetchall()
            connection.execute(
                """UPDATE content_experiment_measurement_windows
                   SET status='closed',evidence_state='experiment_completed',updated_at=?
                   WHERE experiment_id=? AND status NOT IN ('evaluated','closed')""",
                (timestamp, experiment_id),
            )
            for pending in pending_windows:
                _record_window_event(
                    connection, pending["id"], f"experiment-completed:{recommendation_id}",
                    "window_closed", {"reason": "experiment_completed",
                                      "accepted_recommendation_id": recommendation_id}, timestamp,
                )
        return self.get(experiment_id)

    def _evidence(self, experiment: Mapping[str, Any]) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        with self._connect() as connection:
            for variant in experiment["variants"]:
                verified: list[dict[str, Any]] = []
                records = connection.execute(
                    "SELECT * FROM performance_records WHERE post_id=? AND brand_id=? ORDER BY observed_at,id",
                    (variant["post_id"], experiment["brand_id"]),
                ).fetchall()
                for row in records:
                    record = dict(row)
                    event = _connector_event_for_performance(
                        connection, record, brand_id=experiment["brand_id"], post_id=variant["post_id"],
                        metric=experiment["metric"],
                    )
                    if event is not None:
                        verified.append(record)
                evidence.append({
                    "variant_id": variant["id"], "variant_key": variant["variant_key"],
                    "post_id": variant["post_id"], "observation_count": len(verified),
                    "impressions": sum(int(item["impressions"]) for item in verified),
                    "metric_value": sum(int(item[experiment["metric"]]) for item in verified),
                    "performance_record_ids": [item["id"] for item in verified],
                })
        return evidence

    def _window_evidence(
        self, experiment: Mapping[str, Any], window: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return connector-authenticated, time-scoped cumulative snapshots.

        X metric snapshots are cumulative, so the latest in-window snapshot is the
        score. Summing snapshots would double count the same impressions/clicks.
        Observation count is retained separately for guardrails and provenance.
        """
        evidence: list[dict[str, Any]] = []
        stale_ids: list[str] = []
        too_late_ids: list[str] = []
        has_late = False
        opens_at = datetime.fromisoformat(window["opens_at"])
        closes_at = datetime.fromisoformat(window["closes_at"])
        evaluate_at = datetime.fromisoformat(window["evaluate_at"])
        fresh_from = closes_at - timedelta(seconds=int(window["freshness_seconds"]))
        late_evidence_until = datetime.fromisoformat(window["late_evidence_until"])
        with self._connect() as connection:
            for variant in experiment["variants"]:
                verified: list[dict[str, Any]] = []
                rows = connection.execute(
                    """SELECT * FROM performance_records
                       WHERE post_id=? AND brand_id=? ORDER BY observed_at,id""",
                    (variant["post_id"], experiment["brand_id"]),
                ).fetchall()
                for row in rows:
                    record = dict(row)
                    event = _connector_event_for_performance(
                        connection, record, brand_id=experiment["brand_id"],
                        post_id=variant["post_id"], metric=experiment["metric"],
                    )
                    if event is None:
                        continue
                    observed = _datetime(record["observed_at"], "performance observed_at")
                    if opens_at <= observed <= closes_at:
                        if observed < fresh_from:
                            stale_ids.append(record["id"])
                            continue
                        # Evidence persisted after the scheduled evaluation remains
                        # admissible when its provider observation belongs to the
                        # declared window, but is explicitly marked late.
                        created = _datetime(record["created_at"], "performance created_at")
                        if created > late_evidence_until:
                            too_late_ids.append(record["id"])
                            continue
                        has_late = has_late or created > evaluate_at
                        verified.append(record)
                    elif (
                        observed < opens_at
                        and (opens_at - observed).total_seconds() > int(window["freshness_seconds"])
                    ):
                        stale_ids.append(record["id"])
                verified.sort(key=lambda item: (
                    _datetime(item["observed_at"], "performance observed_at"), item["id"],
                ))
                latest = verified[-1] if verified else None
                evidence.append({
                    "variant_id": variant["id"], "variant_key": variant["variant_key"],
                    "post_id": variant["post_id"], "observation_count": len(verified),
                    "impressions": int(latest["impressions"]) if latest else 0,
                    "metric_value": int(latest[experiment["metric"]]) if latest else 0,
                    "performance_record_ids": [item["id"] for item in verified],
                    "latest_observed_at": latest["observed_at"] if latest else None,
                })
        return evidence, {
            "stale_record_ids": sorted(set(stale_ids)),
            "too_late_record_ids": sorted(set(too_late_ids)),
            "has_late_evidence": has_late,
        }

    def _store_recommendation(
        self, connection: sqlite3.Connection, experiment: Mapping[str, Any],
        evidence: list[dict[str, Any]], fingerprint: str, *,
        measurement_window_id: str | None = None,
        sufficient_override: bool = True,
    ) -> str:
        scores = {item["variant_id"]: item["metric_value"] for item in evidence}
        winner_id = None
        status = "insufficient_evidence"
        rationale = "Every variant needs authenticated connector performance before a winner can be recommended."
        if sufficient_override:
            best = max(scores.values())
            leaders = [variant_id for variant_id, score in scores.items() if score == best]
            if len(leaders) == 1:
                winner_id = leaders[0]
                status = "recommended"
                qualifier = "declared measurement window" if measurement_window_id else "available evidence"
                rationale = (
                    f"The winner has the highest evidence-backed {experiment['metric']} "
                    f"snapshot in the {qualifier}."
                )
            else:
                status = "tie"
                rationale = "Evidence is sufficient, but the leading variants are tied."
        existing = connection.execute(
            "SELECT id FROM content_experiment_recommendations WHERE experiment_id=? AND evidence_fingerprint=?",
            (experiment["id"], fingerprint),
        ).fetchone()
        if existing is not None:
            return existing["id"]
        timestamp = store.now()
        recommendation_id = str(uuid4())
        connection.execute(
            """INSERT INTO content_experiment_recommendations
               (id,experiment_id,measurement_window_id,status,winner_variant_id,metric,
                evidence_fingerprint,evidence_json,rationale,learning_id,accepted_by,
                accepted_at,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?)""",
            (recommendation_id, experiment["id"], measurement_window_id, status, winner_id,
             experiment["metric"], fingerprint, json.dumps(evidence, sort_keys=True), rationale,
             timestamp, timestamp),
        )
        return recommendation_id


def _connector_event_for_performance(
    connection: sqlite3.Connection, record: Mapping[str, Any], *, brand_id: str,
    post_id: str, metric: str,
) -> sqlite3.Row | None:
    prefix = "connector_event="
    notes = str(record.get("notes") or "")
    if not notes.startswith(prefix) or ":" not in notes[len(prefix):]:
        return None
    account_id, external_id = notes[len(prefix):].split(":", 1)
    row = connection.execute(
        """SELECT e.* FROM connector_events e
           JOIN connector_accounts a ON a.id=e.connector_account_id
           WHERE e.connector_account_id=? AND e.external_id=?
             AND e.event_type='metric_observed' AND a.brand_id=? AND a.connector_type='x'
             AND a.status IN ('healthy','connected','active')""",
        (account_id, external_id, brand_id),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    if payload.get("post_id") != post_id:
        return None
    try:
        if _datetime(row["observed_at"], "connector event observed_at") != _datetime(
            record["observed_at"], "performance observed_at",
        ):
            return None
    except (ExperimentError, KeyError, TypeError):
        return None
    # The persisted observation must reproduce the values claimed by the
    # performance row; merely citing an unrelated connector event is not proof.
    for field in {"impressions", metric}:
        try:
            provider_value = int(payload.get(field, 0))
            recorded_value = int(record[field])
        except (TypeError, ValueError, KeyError):
            return None
        if provider_value < 0 or provider_value != recorded_value:
            return None
    return row


def _guardrails(value: Mapping[str, Any] | None) -> dict[str, int]:
    raw = dict(value or {})
    unknown = set(raw) - _GUARDRAILS
    if unknown:
        raise ExperimentError(f"unknown guardrails: {', '.join(sorted(unknown))}")
    result = {
        "min_observations_per_variant": int(raw.get("min_observations_per_variant", 1)),
        "min_impressions_per_variant": int(raw.get("min_impressions_per_variant", 1)),
    }
    if any(number < 1 for number in result.values()):
        raise ExperimentError("experiment guardrails must be positive integers")
    return result


def _measurement_windows(
    values: list[Mapping[str, Any]] | None, created_at: str,
) -> list[dict[str, Any]]:
    created = _datetime(created_at, "experiment created_at")
    raw_values = values if values is not None else [{
        "window_key": "24h", "opens_at": created.isoformat(),
        "closes_at": (created + timedelta(hours=24)).isoformat(),
        "evaluate_at": (created + timedelta(hours=24)).isoformat(),
        "late_evidence_until": (created + timedelta(hours=48)).isoformat(),
    }]
    if not 1 <= len(raw_values) <= 10:
        raise ExperimentError("experiments require between 1 and 10 measurement windows")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, value in enumerate(raw_values):
        raw = dict(value)
        allowed = {
            "window_key", "opens_at", "closes_at", "evaluate_at", "late_evidence_until",
            "freshness_seconds", "retry_interval_seconds",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ExperimentError(
                f"unknown measurement window fields: {', '.join(sorted(unknown))}"
            )
        key = str(raw.get("window_key") or f"window-{index + 1}").strip()
        if not key or len(key) > 80:
            raise ExperimentError("measurement window_key must be between 1 and 80 characters")
        if key in seen:
            raise ExperimentError("measurement window_key values must be unique")
        seen.add(key)
        try:
            opens = _datetime(raw.get("opens_at") or created.isoformat(), "measurement opens_at")
            closes = _datetime(raw["closes_at"], "measurement closes_at")
            evaluates = _datetime(raw.get("evaluate_at") or closes.isoformat(), "measurement evaluate_at")
            late_until = _datetime(
                raw.get("late_evidence_until") or (evaluates + timedelta(hours=24)).isoformat(),
                "measurement late_evidence_until",
            )
            freshness = int(raw.get("freshness_seconds", 21_600))
            retry = int(raw.get("retry_interval_seconds", 3_600))
        except KeyError as exc:
            raise ExperimentError("measurement closes_at is required") from exc
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ExperimentError):
                raise
            raise ExperimentError("measurement window durations must be integers") from exc
        if not opens < closes <= evaluates <= late_until:
            raise ExperimentError(
                "measurement timestamps must satisfy opens_at < closes_at <= evaluate_at <= late_evidence_until"
            )
        if not 60 <= freshness <= 2_592_000:
            raise ExperimentError("freshness_seconds must be between 60 and 2592000")
        if not 60 <= retry <= 86_400:
            raise ExperimentError("retry_interval_seconds must be between 60 and 86400")
        result.append({
            "window_key": key, "opens_at": opens.isoformat(), "closes_at": closes.isoformat(),
            "evaluate_at": evaluates.isoformat(), "late_evidence_until": late_until.isoformat(),
            "freshness_seconds": freshness, "retry_interval_seconds": retry,
        })
    return result


def _datetime(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ExperimentError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ExperimentError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _iso(value: str) -> str:
    return _datetime(value, "timestamp").isoformat()


def _enqueue_job(
    connection: sqlite3.Connection, job_type: str, idempotency_key: str,
    payload: Mapping[str, Any], *, brand_id: str | None, run_after: str,
    connector_account_id: str | None = None, priority: int = 0,
) -> str:
    """Transactionally persist a durable job on this store's database."""
    timestamp = store.now()
    job_id = str(uuid4())
    connection.execute(
        """INSERT INTO durable_jobs
           (id,brand_id,connector_account_id,job_type,payload,status,run_after,priority,
            max_attempts,attempt_count,idempotency_key,locked_at,locked_by,last_error,
            result,created_at,updated_at,completed_at)
           VALUES (?,?,?,?,?,'queued',?,?,3,0,?,NULL,NULL,NULL,NULL,?,?,NULL)
           ON CONFLICT(job_type,idempotency_key) DO NOTHING""",
        (job_id, brand_id, connector_account_id, job_type,
         json.dumps(dict(payload), sort_keys=True), _iso(run_after), priority,
         idempotency_key, timestamp, timestamp),
    )
    row = connection.execute(
        "SELECT id FROM durable_jobs WHERE job_type=? AND idempotency_key=?",
        (job_type, idempotency_key),
    ).fetchone()
    assert row is not None
    return str(row["id"])


def _record_window_event(
    connection: sqlite3.Connection, window_id: str, event_key: str,
    event_type: str, details: Mapping[str, Any], timestamp: str,
) -> None:
    connection.execute(
        """INSERT INTO content_experiment_measurement_events
           (id,measurement_window_id,event_key,event_type,details_json,created_at)
           VALUES (?,?,?,?,?,?) ON CONFLICT(measurement_window_id,event_key) DO NOTHING""",
        (str(uuid4()), window_id, event_key, event_type,
         json.dumps(dict(details), sort_keys=True), timestamp),
    )


def _source_update_draft(source: Mapping[str, Any]) -> str:
    body = source_grounded_x_draft(
        title=str(source["title"]), summary=str(source["body_summary"]),
        url=source["url"], owned=False,
    )
    return body.replace("From the source: ", "Source update: ", 1)


def _stable_id(namespace: str, *parts: str) -> str:
    digest = sha256("|".join((namespace, *parts)).encode()).hexdigest()[:32]
    return f"{namespace}:{digest}"


def _decode_recommendation(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["evidence"] = json.loads(result.pop("evidence_json"))
    return result


def _decode_window(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["evidence"] = json.loads(result.pop("evidence_json") or "[]")
    result["has_late_evidence"] = bool(result["has_late_evidence"])
    return result


def _decode_event(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["details"] = json.loads(result.pop("details_json") or "{}")
    return result


def make_experiment_window_collection_handler(
    database: str | Path, readable_x_account_ids: list[str] | tuple[str, ...] = (),
):
    experiment_store = ExperimentStore(database)
    account_ids = list(readable_x_account_ids)

    def handle(job: dict[str, Any]) -> dict[str, Any]:
        payload = job.get("payload") or {}
        _validate_window_job_binding(experiment_store, job, payload)
        window = experiment_store.record_collection_request(
            str(payload["measurement_window_id"]),
            collection_round=int(payload["collection_round"]),
            connector_account_ids=account_ids,
            as_of=job.get("locked_at"),
        )
        return {"measurement_window_id": window["id"], "status": window["status"],
                "collection_round": window["collection_round"]}

    return handle


def make_experiment_window_evaluation_handler(database: str | Path):
    experiment_store = ExperimentStore(database)

    def handle(job: dict[str, Any]) -> dict[str, Any]:
        payload = job.get("payload") or {}
        _validate_window_job_binding(experiment_store, job, payload)
        window = experiment_store.evaluate_window(
            str(payload["measurement_window_id"]), as_of=job.get("locked_at"),
        )
        return {"measurement_window_id": window["id"], "status": window["status"],
                "evidence_state": window["evidence_state"],
                "recommendation_id": window["recommendation_id"]}

    return handle


def _validate_window_job_binding(
    experiment_store: ExperimentStore, job: Mapping[str, Any], payload: Mapping[str, Any],
) -> None:
    try:
        window_id = str(payload["measurement_window_id"])
        experiment_id = str(payload["experiment_id"])
        job_brand_id = str(job["brand_id"])
    except (KeyError, TypeError) as exc:
        raise ExperimentError("measurement job is missing its immutable resource binding") from exc
    window = experiment_store.get_window(window_id)
    experiment = experiment_store.get(window["experiment_id"])
    if (
        experiment_id != experiment["id"]
        or job_brand_id != experiment["brand_id"]
    ):
        raise ExperimentError("measurement job resource binding does not match the window")


__all__ = [
    "EXPERIMENT_WINDOW_COLLECT_JOB_TYPE", "EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE",
    "ExperimentError", "ExperimentStore", "make_experiment_window_collection_handler",
    "make_experiment_window_evaluation_handler",
]
