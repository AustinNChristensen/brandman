"""SQLite integration adapter for :mod:`brandman.operating_plan`."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any

from brandman import store
from brandman.dispatch import Lifecycle, SQLiteDispatchStore, dispatch_item_to_dict
from brandman.editorial import EditorialStore
from brandman.mission_artifacts import MissionArtifactStore
from brandman.operating_plan import OperatingPlanService


def sqlite_connection_factory(database: str | Path) -> Callable[[], Iterator[sqlite3.Connection]]:
    """Return the context-managed connection shape used by MissionArtifactStore."""

    path = str(database)

    @contextmanager
    def connection() -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    return connection


def _decode(record: Mapping[str, Any], *fields: str) -> dict[str, Any]:
    result = dict(record)
    for field in fields:
        value = result.get(field)
        if isinstance(value, str):
            try:
                result[field] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return result


class SQLiteOperatingPlanRepository:
    """Read canonical operating state from the current Brand OS SQLite database."""

    def __init__(
        self,
        database: str | Path | None = None,
        *,
        dispatch: SQLiteDispatchStore | None = None,
        editorial: EditorialStore | None = None,
    ) -> None:
        self.database = str(store.DATA_PATH if database is None else database)
        self.connection_factory = sqlite_connection_factory(self.database)
        self.dispatch = dispatch or SQLiteDispatchStore(self.database)
        self.editorial = editorial or EditorialStore(self.database)

    def mission_progress(self, mission_id: str, as_of: str | None = None) -> dict[str, Any] | None:
        """Load progress, preferring evidence-promoted canonical KPI values."""

        with self.connection_factory() as connection:
            mission_row = connection.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
            if mission_row is None:
                return None
            mission = dict(mission_row)
            goals = [dict(row) for row in connection.execute(
                "SELECT * FROM mission_goals WHERE mission_id=? ORDER BY metric", (mission_id,)
            ).fetchall()]
            tables = {row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            for goal in goals:
                latest = None
                canonical_kpis_enabled = "canonical_kpi_values" in tables
                if canonical_kpis_enabled:
                    latest = connection.execute(
                        """SELECT value,observed_at,source,evidence_record_id
                           FROM canonical_kpi_values WHERE mission_id=? AND metric=?""",
                        (mission_id, goal["metric"]),
                    ).fetchone()
                # Legacy snapshots are a compatibility source only before the
                # evidence-backed attribution schema is installed. Once it is
                # present, an unpromoted observation must never become current.
                if not canonical_kpis_enabled and "kpi_snapshots" in tables:
                    latest = connection.execute(
                        """SELECT value,observed_at,source,id AS evidence_record_id
                           FROM kpi_snapshots WHERE mission_id=? AND metric=?
                           ORDER BY observed_at DESC,created_at DESC LIMIT 1""",
                        (mission_id, goal["metric"]),
                    ).fetchone()
                goal["current"] = latest["value"] if latest is not None else goal["baseline"]
                goal["latest_observed_at"] = latest["observed_at"] if latest is not None else None
                goal["kpi_source"] = latest["source"] if latest is not None else "mission_baseline"
                goal["kpi_evidence_record_id"] = latest["evidence_record_id"] if latest is not None else None

        starts = datetime.fromisoformat(str(mission["starts_at"]).replace("Z", "+00:00"))
        ends = datetime.fromisoformat(str(mission["ends_at"]).replace("Z", "+00:00"))
        observed = datetime.fromisoformat((as_of or store.now()).replace("Z", "+00:00"))
        total_seconds = max((ends - starts).total_seconds(), 1)
        elapsed = max(0.0, min(1.0, (observed - starts).total_seconds() / total_seconds))
        remaining_days = max((ends - observed).total_seconds() / 86400, 0.0)
        for goal in goals:
            span = float(goal["target"]) - float(goal["baseline"])
            current = float(goal["current"])
            goal["expected_current"] = float(goal["baseline"]) + span * elapsed
            goal["progress_percent"] = 100.0 if span == 0 else max(0.0, min(100.0, ((current - float(goal["baseline"])) / span) * 100))
            goal["required_daily_change"] = 0.0 if remaining_days == 0 else abs(float(goal["target"]) - current) / remaining_days
        mission["goals"] = goals
        mission["as_of"] = observed.isoformat()
        mission["remaining_days"] = remaining_days
        return mission

    def scheduled_work(self, brand_id: str, plan_date: str) -> list[dict[str, Any]]:
        """Return only work with an actual delivery date for this plan day.

        Unscheduled drafts are editorial backlog. Treating them as scheduled
        creates fake urgency and duplicate preparation actions.
        """

        with self.connection_factory() as connection:
            posts = [dict(row) for row in connection.execute(
                """SELECT p.id,'post' AS kind,p.campaign_id,p.channel,p.status,p.scheduled_for,
                   p.external_post_id,p.updated_at,c.name AS campaign_name
                   FROM posts p JOIN campaigns c ON c.id=p.campaign_id
                   WHERE c.brand_id=? AND date(p.scheduled_for)=?
                     AND lower(p.status) NOT IN ('cancelled','stale','rejected','archived')
                   ORDER BY p.scheduled_for,p.updated_at DESC""",
                (brand_id, plan_date),
            ).fetchall()]
            sources = [dict(row) for row in connection.execute(
                """SELECT s.id,'source' AS kind,NULL AS campaign_id,s.source_type AS channel,
                   s.lifecycle_state AS status,s.scheduled_for,s.external_source_id,
                   s.created_at AS updated_at,s.title FROM sources s
                   WHERE s.brand_id=? AND date(s.scheduled_for)=?
                   AND lower(s.lifecycle_state) NOT IN ('cancelled','stale','rejected','archived','abandoned')
                   AND NOT EXISTS (SELECT 1 FROM fixture_quarantine_registry q
                     WHERE q.table_name='sources' AND q.record_key_json=json_array(s.id))
                   ORDER BY s.scheduled_for,s.created_at DESC""",
                (brand_id, plan_date),
            ).fetchall()]
            issue_tables = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='newsletter_issues'"
            ).fetchone()
            issues = [] if issue_tables is None else [dict(row) for row in connection.execute(
                """SELECT id,'newsletter_issue' AS kind,candidate_id,NULL AS campaign_id,
                   'beehiiv' AS channel,lifecycle AS status,scheduled_for,
                   beehiiv_external_id AS external_id,updated_at,current_revision
                   FROM newsletter_issues WHERE brand_id=?
                   AND lower(lifecycle) NOT IN ('cancelled','stale','rejected','archived','abandoned')
                   AND (
                     date(scheduled_for)=? OR date(published_at)=?
                   ) ORDER BY scheduled_for,updated_at DESC""",
                (brand_id, plan_date, plan_date),
            ).fetchall()]
        return posts + sources + issues

    def approval_queue(self, brand_id: str) -> list[dict[str, Any]]:
        items = [
            dispatch_item_to_dict(item)
            for item in self.dispatch.list_items(brand_id=brand_id, status=Lifecycle.AWAITING_APPROVAL)
        ]
        for item in items:
            item["channel"] = item.get("connector")
            item["artifact_id"] = item.get("canonical_post_id")
        # fact_checked is the editorial equivalent of awaiting approval.
        for issue in self.editorial.list_issues(brand_id, lifecycle="fact_checked"):
            fact_check = self.editorial.get_fact_check(
                issue["id"], int(issue["current_revision"])
            )
            if not (
                fact_check
                and fact_check.get("passed") is True
                and int(fact_check.get("revision", -1)) == int(issue["current_revision"])
                and fact_check.get("issue_id") == issue["id"]
            ):
                continue
            items.append({
                "id": f"newsletter-approval:{issue['id']}:r{issue['current_revision']}",
                "issue_id": issue["id"], "artifact_id": issue["id"],
                "channel": "newsletter", "connector": "beehiiv",
                "status": "awaiting_approval", "revision": issue["current_revision"],
                "updated_at": issue["updated_at"],
            })
        with self.connection_factory() as connection:
            has_experiments = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_experiment_recommendations'"
            ).fetchone()
            recommendations = [] if has_experiments is None else connection.execute(
                """SELECT r.id,r.experiment_id,r.winner_variant_id,r.metric,r.updated_at
                   FROM content_experiment_recommendations r
                   JOIN content_experiments e ON e.id=r.experiment_id
                   WHERE e.brand_id=? AND e.status='active' AND r.status='recommended'
                   ORDER BY r.updated_at DESC,r.id DESC""", (brand_id,),
            ).fetchall()
        for recommendation in recommendations:
            items.append({
                "id": recommendation["id"], "recommendation_id": recommendation["id"],
                "experiment_id": recommendation["experiment_id"],
                "artifact_id": recommendation["winner_variant_id"],
                "channel": "x-experiment", "connector": "x",
                "kind": "experiment_recommendation", "status": "awaiting_human_acceptance",
                "metric": recommendation["metric"], "updated_at": recommendation["updated_at"],
            })
        return sorted(items, key=lambda item: (str(item.get("updated_at") or ""), str(item["id"])), reverse=True)

    def connector_health(self, brand_id: str) -> list[dict[str, Any]]:
        with self.connection_factory() as connection:
            tables = {row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            joins = [
                "LEFT JOIN connector_account_configurations c ON c.connector_account_id=a.id"
            ]
            conditions = [
                "a.brand_id=?",
                # These are deliberate terminal/retired states, not outages.
                # Keep actionable states such as disconnected, degraded, and
                # reconnect_required so real connector problems still surface.
                "lower(a.status) NOT IN "
                "('disabled','abandoned','quarantined','cancelled','stale',"
                "'rejected','archived','deleted')",
            ]
            if "third_party_source_configs" in tables:
                joins.append(
                    "LEFT JOIN third_party_source_configs t ON t.connector_account_id=a.id"
                )
                # Disabling a governed public feed is an intentional operator
                # decision, not a connection outage that should be promoted as
                # a mission-blocking restore action.
                conditions.append("(t.connector_account_id IS NULL OR t.enabled=1)")
            if "fixture_quarantine_registry" in tables:
                conditions.extend((
                    "NOT EXISTS (SELECT 1 FROM fixture_quarantine_registry q "
                    "WHERE q.table_name='connector_accounts' "
                    "AND q.record_key_json=json_array(a.id))",
                    "NOT EXISTS (SELECT 1 FROM fixture_quarantine_registry q "
                    "WHERE q.table_name='connector_account_configurations' "
                    "AND q.record_key_json=json_array(a.id))",
                ))
                if "third_party_source_configs" in tables:
                    conditions.append(
                        "NOT EXISTS (SELECT 1 FROM fixture_quarantine_registry q "
                        "WHERE q.table_name='third_party_source_configs' "
                        "AND q.record_key_json=json_array(a.id))"
                    )
            if "periodic_schedules" in tables:
                # A connector may have multiple periodic uses.  A retired
                # schedule suppresses the connector only when no enabled,
                # non-quarantined schedule still governs that account.
                viable_schedule = (
                    "SELECT 1 FROM periodic_schedules active "
                    "WHERE active.connector_account_id=a.id AND active.enabled=1"
                )
                if "fixture_quarantine_registry" in tables:
                    viable_schedule += (
                        " AND NOT EXISTS (SELECT 1 FROM fixture_quarantine_registry q "
                        "WHERE q.table_name='periodic_schedules' "
                        "AND q.record_key_json=json_array(active.id))"
                    )
                conditions.append(
                    "(NOT EXISTS (SELECT 1 FROM periodic_schedules any_schedule "
                    "WHERE any_schedule.connector_account_id=a.id) OR EXISTS ("
                    + viable_schedule + "))"
                )
            rows = connection.execute(
                "SELECT a.*,COALESCE(c.configuration,'{}') AS configuration "
                "FROM connector_accounts a " + " ".join(joins)
                + " WHERE " + " AND ".join(conditions)
                + " ORDER BY a.connector_type,a.display_name,a.id",
                (brand_id,),
            ).fetchall()
            agent_rows = [] if "execution_agents" not in tables else connection.execute(
                "SELECT * FROM execution_agents WHERE brand_id=? AND enabled=1", (brand_id,),
            ).fetchall()
            receipt_rows = [] if "execution_tasks" not in tables else connection.execute(
                """SELECT provider FROM execution_tasks WHERE brand_id=? AND status='completed'
                   AND receipt_external_id IS NOT NULL""", (brand_id,),
            ).fetchall()
        items = [_decode(row, "scopes", "capabilities", "configuration") for row in rows]
        assisted_providers = {
            item["connector_type"] for item in items
            if (item.get("configuration") or {}).get("delivery_mode")
            in {"browser_assisted", "mcp_assisted"}
            and item["connector_type"] in {"beehiiv", "x"}
        }
        for item in items:
            mode = (item.get("configuration") or {}).get("delivery_mode", "api")
            item["execution_mode"] = "assisted" if mode in {
                "browser_assisted", "mcp_assisted"
            } else "api"
            item["required_for_operating_plan"] = not (
                item["connector_type"] == "website"
                or (item["execution_mode"] == "api"
                    and item["connector_type"] in assisted_providers)
            )
        if assisted_providers:
            now = datetime.fromisoformat(store.now().replace("Z", "+00:00")).astimezone(UTC)
            recent_agents = []
            for row in agent_rows:
                raw = row["last_heartbeat_at"]
                if not raw:
                    continue
                observed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if observed.tzinfo and 0 <= (now - observed.astimezone(UTC)).total_seconds() <= 900:
                    recent_agents.append(row)
            proven = {row["provider"] for row in receipt_rows}
            missing_receipts = sorted(assisted_providers - proven)
            healthy = bool(recent_agents) and not missing_receipts
            needs = []
            if not recent_agents:
                needs.append("start or sign in to the browser/MCP execution helper")
            if missing_receipts:
                needs.append("record an approval-bound receipt for " + ", ".join(missing_receipts))
            items.append({
                "id": "assisted-execution", "connector_type": "assisted_execution",
                "display_name": "Browser/MCP assisted execution",
                "status": "healthy" if healthy else "needs_attention",
                "detail": "Assisted delivery needs " + " and ".join(needs) if needs else
                          "Execution helper heartbeat and provider receipts are current.",
                "execution_mode": "assisted", "required_for_operating_plan": True,
                "assisted_providers": sorted(assisted_providers),
            })
        return items

    def recent_performance(self, brand_id: str) -> list[dict[str, Any]]:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """SELECT r.*,p.campaign_id FROM performance_records r
                   LEFT JOIN posts p ON p.id=r.post_id WHERE r.brand_id=?
                   AND NOT EXISTS (SELECT 1 FROM fixture_quarantine_registry q
                     WHERE q.table_name='performance_records'
                       AND q.record_key_json=json_array(r.id))
                   ORDER BY r.observed_at DESC,r.created_at DESC LIMIT 100""",
                (brand_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def editorial_candidates(self, brand_id: str) -> list[dict[str, Any]]:
        return self.editorial.list_candidates(brand_id, status="open", limit=100)

    def open_gaps(self, brand_id: str) -> list[dict[str, Any]]:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """SELECT * FROM product_feedback WHERE brand_id=? AND status='open'
                   AND severity IN ('critical','high')
                   ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END,
                   last_seen_at DESC,created_at DESC""",
                (brand_id,),
            ).fetchall()
        return [_decode(row, "related_ids") for row in rows]

    def job_failures(self, brand_id: str) -> list[dict[str, Any]]:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """SELECT * FROM durable_jobs WHERE brand_id=?
                   AND status IN ('failed','needs_attention')
                   ORDER BY updated_at DESC,created_at DESC""",
                (brand_id,),
            ).fetchall()
        return [_decode(row, "payload", "result") for row in rows]


def build_progress_loader(database: str | Path | None = None) -> Callable[[str, str | None], Mapping[str, Any] | None]:
    repository = SQLiteOperatingPlanRepository(store.DATA_PATH if database is None else database)

    def load(mission_id: str, as_of: str | None = None) -> Mapping[str, Any] | None:
        return repository.mission_progress(mission_id, as_of)

    return load


def build_mission_artifact_store(database: str | Path | None = None) -> MissionArtifactStore:
    artifacts = MissionArtifactStore(sqlite_connection_factory(store.DATA_PATH if database is None else database))
    artifacts.init_schema()
    return artifacts


def build_operating_plan_service(database: str | Path | None = None) -> OperatingPlanService:
    """Build the production operating service over one shared SQLite file."""

    selected = store.DATA_PATH if database is None else database
    repository = SQLiteOperatingPlanRepository(selected)
    artifacts = build_mission_artifact_store(selected)
    return OperatingPlanService(repository, artifacts)


__all__ = [
    "SQLiteOperatingPlanRepository", "sqlite_connection_factory",
    "build_progress_loader", "build_mission_artifact_store", "build_operating_plan_service",
]
