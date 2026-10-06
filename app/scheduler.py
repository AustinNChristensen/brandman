"""Restart-safe, bounded periodic orchestration.

The scheduler only creates durable internal jobs. It never approves, queues, or
publishes external content. A host supplies ticks; importing starts no daemon.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping
from uuid import uuid4

from app import store
from app.operating_repository import build_operating_plan_service
from app.operational_feedback import report_orchestration_failure
from app.sync import SYNC_JOB_TYPE
from app.beehiiv_assisted_pull import ASSISTED_BEEHIIV_PULL_JOB_TYPE

OPERATING_PLAN_JOB_TYPE = "operating_plan.refresh"
CONNECTOR_SYNC = "connector_sync"
OPERATING_PLAN_REFRESH = "operating_plan_refresh"
DOWNSTREAM_JOB = "downstream_job"
ASSISTED_BEEHIIV_PULL = "assisted_beehiiv_pull"

SCHEMA = """
CREATE TABLE IF NOT EXISTS periodic_schedules (
  id TEXT PRIMARY KEY, schedule_key TEXT NOT NULL UNIQUE,
  brand_id TEXT REFERENCES brands(id), connector_account_id TEXT REFERENCES connector_accounts(id),
  name TEXT NOT NULL, action_type TEXT NOT NULL, interval_seconds INTEGER NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1,
  next_run_at TEXT NOT NULL, last_due_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orchestration_decisions (
  id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL REFERENCES periodic_schedules(id),
  due_at TEXT NOT NULL, status TEXT NOT NULL, job_type TEXT NOT NULL,
  idempotency_key TEXT NOT NULL, job_id TEXT, error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(schedule_id, due_at)
);
CREATE TABLE IF NOT EXISTS orchestration_ticks (
  id TEXT PRIMARY KEY, as_of TEXT NOT NULL, max_decisions INTEGER NOT NULL,
  decisions_planned INTEGER NOT NULL, jobs_enqueued INTEGER NOT NULL,
  reconciled INTEGER NOT NULL, created_at TEXT NOT NULL,
  brand_id TEXT REFERENCES brands(id)
);
CREATE INDEX IF NOT EXISTS periodic_schedules_due ON periodic_schedules(enabled,next_run_at);
CREATE INDEX IF NOT EXISTS orchestration_decisions_pending ON orchestration_decisions(status,created_at);
"""


@dataclass(frozen=True, slots=True)
class TickResult:
    tick_id: str
    as_of: str
    decisions_planned: int
    jobs_enqueued: int
    reconciled: int
    reached_limit: bool
    decision_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["decision_ids"] = list(self.decision_ids)
        return value


class PeriodicOrchestrator:
    """Persist schedule slots and translate them to idempotent durable jobs."""

    def __init__(self, database: str | Path, *, clock: Callable[[], datetime] | None = None,
                 safe_downstream_job_types: set[str] | None = None) -> None:
        self.database = Path(database)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.safe_downstream_job_types = frozenset(safe_downstream_job_types or ())
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def init_schema(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            tick_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(orchestration_ticks)")
            }
            if "brand_id" not in tick_columns:
                connection.execute(
                    "ALTER TABLE orchestration_ticks ADD COLUMN brand_id TEXT REFERENCES brands(id)"
                )

    def ensure_schedule(self, schedule_key: str, *, name: str, action_type: str,
                        interval_seconds: int, payload: Mapping[str, Any],
                        brand_id: str | None = None,
                        connector_account_id: str | None = None,
                        next_run_at: str | None = None) -> dict[str, Any]:
        if not schedule_key.strip():
            raise ValueError("schedule_key cannot be empty")
        if action_type not in {
            CONNECTOR_SYNC, OPERATING_PLAN_REFRESH, DOWNSTREAM_JOB, ASSISTED_BEEHIIV_PULL,
        }:
            raise ValueError("unsupported periodic action type")
        if not 60 <= interval_seconds <= 31_536_000:
            raise ValueError("interval_seconds must be between 60 and 31536000")
        self._validate_action(action_type, payload)
        timestamp = self._now_iso()
        first_due = self._iso(next_run_at) if next_run_at else timestamp
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO periodic_schedules
                   (id,schedule_key,brand_id,connector_account_id,name,action_type,
                    interval_seconds,payload,enabled,next_run_at,last_due_at,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?)
                   ON CONFLICT(schedule_key) DO UPDATE SET brand_id=excluded.brand_id,
                     connector_account_id=excluded.connector_account_id,name=excluded.name,
                     action_type=excluded.action_type,interval_seconds=excluded.interval_seconds,
                     payload=excluded.payload,updated_at=excluded.updated_at""",
                (str(uuid4()), schedule_key, brand_id, connector_account_id, name, action_type,
                 interval_seconds, json.dumps(dict(payload), sort_keys=True), first_due,
                 None, timestamp, timestamp),
            )
            row = connection.execute("SELECT * FROM periodic_schedules WHERE schedule_key=?",
                                     (schedule_key,)).fetchone()
        return self._schedule(dict(row))

    def ensure_defaults(self) -> list[dict[str, Any]]:
        """Discover safe read accounts and active missions without enabling writes."""
        result: list[dict[str, Any]] = []
        with self._connect() as connection:
            accounts = connection.execute(
                "SELECT * FROM connector_accounts WHERE status IN ('healthy','connected') ORDER BY id"
            ).fetchall()
            missions = connection.execute(
                "SELECT id,brand_id,name FROM missions WHERE status='active' ORDER BY id"
            ).fetchall()
        for raw in accounts:
            account = dict(raw)
            permissions = set(json.loads(account["scopes"] or "[]")) | set(
                json.loads(account["capabilities"] or "[]"))
            configuration_row = None
            with self._connect() as connection:
                configuration_row = connection.execute(
                    "SELECT configuration FROM connector_account_configurations WHERE connector_account_id=?",
                    (account["id"],),
                ).fetchone()
            configuration = json.loads(
                configuration_row["configuration"] if configuration_row else "{}"
            )
            if (
                account["connector_type"] == "beehiiv"
                and configuration.get("delivery_mode") in {"browser_assisted", "mcp_assisted"}
            ):
                result.append(self.ensure_schedule(
                    f"assisted-beehiiv:{account['id']}:metadata-measurements",
                    name=f"Request aggregate Beehiiv pull: {account['display_name']}",
                    action_type=ASSISTED_BEEHIIV_PULL, interval_seconds=21_600,
                    payload={"brand_id": account["brand_id"],
                             "connector_account_id": account["id"]},
                    brand_id=account["brand_id"], connector_account_id=account["id"],
                ))
                continue
            if account["connector_type"] != "rss" and not any(
                    value.endswith(".read") for value in permissions):
                continue
            stream = "posts" if account["connector_type"] == "beehiiv" else "content"
            result.append(self.ensure_schedule(
                f"connector:{account['id']}:{stream}", name=f"Sync {account['display_name']}",
                action_type=CONNECTOR_SYNC, interval_seconds=900,
                payload={"brand_id": account["brand_id"],
                         "connector_account_id": account["id"], "stream": stream},
                brand_id=account["brand_id"], connector_account_id=account["id"],
            ))
        for mission in missions:
            result.append(self.ensure_schedule(
                f"mission:{mission['id']}:operating-plan",
                name=f"Refresh operating plan: {mission['name']}",
                action_type=OPERATING_PLAN_REFRESH, interval_seconds=3600,
                payload={"brand_id": mission["brand_id"], "mission_id": mission["id"]},
                brand_id=mission["brand_id"],
            ))
        return result

    def tick(self, *, as_of: str | None = None, max_decisions: int = 50,
             brand_id: str | None = None) -> TickResult:
        if not 1 <= max_decisions <= 500:
            raise ValueError("max_decisions must be between 1 and 500")
        timestamp = self._iso(as_of) if as_of else self._now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            due_query = "SELECT * FROM periodic_schedules WHERE enabled=1 AND next_run_at<=?"
            due_parameters: tuple[Any, ...] = (timestamp,)
            if brand_id is not None:
                due_query += " AND brand_id=?"
                due_parameters += (brand_id,)
            due = connection.execute(
                due_query + " ORDER BY next_run_at,id LIMIT ?", (*due_parameters, max_decisions),
            ).fetchall()
            for raw in due:
                schedule = self._schedule(dict(raw))
                due_at = schedule["next_run_at"]
                job_type, key, _ = self._job_for(schedule, due_at)
                suppression = self._connector_sync_suppression(
                    connection, schedule, timestamp,
                )
                connection.execute(
                    """INSERT INTO orchestration_decisions
                       (id,schedule_id,due_at,status,job_type,idempotency_key,job_id,error,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,NULL,?,?,?)
                       ON CONFLICT(schedule_id,due_at) DO NOTHING""",
                    (str(uuid4()), schedule["id"], due_at,
                     "suppressed" if suppression else "planned", job_type, key,
                     suppression, timestamp, timestamp),
                )
                next_due = self._next_due(due_at, schedule["interval_seconds"], timestamp)
                connection.execute(
                    """UPDATE periodic_schedules SET next_run_at=?,last_due_at=?,updated_at=?
                       WHERE id=? AND next_run_at=?""",
                    (next_due, due_at, timestamp, schedule["id"], due_at),
                )
            pending_query = """SELECT d.*,s.brand_id,s.connector_account_id,s.payload AS schedule_payload,
                          s.action_type,s.schedule_key FROM orchestration_decisions d
                   JOIN periodic_schedules s ON s.id=d.schedule_id WHERE d.status='planned'"""
            pending_parameters: tuple[Any, ...] = ()
            if brand_id is not None:
                pending_query += " AND s.brand_id=?"
                pending_parameters = (brand_id,)
            pending = connection.execute(
                pending_query + " ORDER BY d.created_at,d.id LIMIT ?",
                (*pending_parameters, max_decisions),
            ).fetchall()

        enqueued = reconciled = 0
        decision_ids: list[str] = []
        for raw in pending:
            decision = dict(raw)
            payload = json.loads(decision["schedule_payload"] or "{}")
            _, _, job_payload = self._job_for({**decision, "payload": payload}, decision["due_at"])
            try:
                job = store.enqueue_job(decision["job_type"], decision["idempotency_key"],
                                        job_payload, brand_id=decision["brand_id"],
                                        connector_account_id=decision["connector_account_id"],
                                        # Preserve the schedule's logical clock. This keeps
                                        # replay/backfill ticks immediately runnable at their
                                        # declared due time instead of coupling them to the
                                        # worker host's wall clock.
                                        run_after=decision["due_at"])
                with self._connect() as connection:
                    connection.execute(
                        """UPDATE orchestration_decisions SET status='enqueued',job_id=?,
                           error=NULL,updated_at=? WHERE id=? AND status='planned'""",
                        (job["id"], timestamp, decision["id"]),
                    )
                enqueued += 1
                reconciled += int(job["created_at"] < decision["created_at"])
                decision_ids.append(decision["id"])
            except Exception as exc:
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE orchestration_decisions SET error=?,updated_at=? WHERE id=?",
                        (type(exc).__name__, timestamp, decision["id"]),
                    )
                report_orchestration_failure(decision)

        tick_id = str(uuid4())
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO orchestration_ticks
                   (id,as_of,max_decisions,decisions_planned,jobs_enqueued,reconciled,created_at,brand_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (tick_id, timestamp, max_decisions, len(due), enqueued,
                 reconciled, timestamp, brand_id),
            )
            remaining_query = "SELECT 1 FROM periodic_schedules WHERE enabled=1 AND next_run_at<=?"
            remaining_parameters: tuple[Any, ...] = (timestamp,)
            if brand_id is not None:
                remaining_query += " AND brand_id=?"
                remaining_parameters += (brand_id,)
            remaining = connection.execute(
                remaining_query + " LIMIT 1", remaining_parameters,
            ).fetchone()
        return TickResult(tick_id, timestamp, len(due), enqueued, reconciled,
                          bool(remaining), tuple(decision_ids))

    @staticmethod
    def _connector_sync_suppression(
        connection: sqlite3.Connection, schedule: Mapping[str, Any], as_of: str,
    ) -> str | None:
        """Prevent overlapping syncs and cool down repeated DNS incidents.

        The schedule and its audit decision still advance. No cursor is touched;
        the next permitted sync resumes from the last durable cursor.
        """
        if (
            schedule["action_type"] != CONNECTOR_SYNC
            or not schedule.get("connector_account_id")
        ):
            return None
        latest = connection.execute(
            """SELECT status,last_error,updated_at FROM durable_jobs
               WHERE connector_account_id=? AND job_type=?
               ORDER BY updated_at DESC,created_at DESC LIMIT 1""",
            (schedule["connector_account_id"], SYNC_JOB_TYPE),
        ).fetchone()
        if latest is None:
            return None
        if latest["status"] in {"queued", "processing", "retry"}:
            return "equivalent connector sync already active"
        if (
            latest["status"] == "needs_attention"
            and latest["last_error"] == "rss DNS resolution failed"
        ):
            updated = datetime.fromisoformat(str(latest["updated_at"]).replace("Z", "+00:00"))
            current = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if current - updated < timedelta(hours=1):
                return "rss DNS incident cooling down after retry exhaustion"
        return None

    def list_schedules(self, *, brand_id: str | None = None) -> list[dict[str, Any]]:
        query, parameters = "SELECT * FROM periodic_schedules", ()
        if brand_id:
            query, parameters = query + " WHERE brand_id=?", (brand_id,)
        with self._connect() as connection:
            return [self._schedule(dict(row)) for row in connection.execute(
                query + " ORDER BY next_run_at,id", parameters)]

    def set_schedule_enabled(
        self, schedule_key: str, enabled: bool, *, brand_id: str | None = None,
    ) -> dict[str, Any]:
        """Enable or disable future slots without altering prior decisions or jobs."""
        timestamp = self._now_iso()
        scope = "schedule_key=?"
        parameters: tuple[Any, ...] = (schedule_key,)
        if brand_id is not None:
            scope += " AND brand_id=?"
            parameters += (brand_id,)
        with self._connect() as connection:
            updated = connection.execute(
                f"UPDATE periodic_schedules SET enabled=?,updated_at=? WHERE {scope}",
                (int(enabled), timestamp, *parameters),
            )
            if not updated.rowcount:
                raise KeyError(f"unknown periodic schedule: {schedule_key}")
            row = connection.execute(
                f"SELECT * FROM periodic_schedules WHERE {scope}", parameters,
            ).fetchone()
        assert row is not None
        return self._schedule(dict(row))

    def status(self, *, brand_id: str | None = None) -> dict[str, Any]:
        timestamp = self._now_iso()
        schedule_scope = ""
        schedule_parameters: tuple[Any, ...] = (timestamp,)
        decision_scope = ""
        decision_parameters: tuple[Any, ...] = ()
        tick_scope = ""
        tick_parameters: tuple[Any, ...] = ()
        if brand_id is not None:
            schedule_scope = " WHERE brand_id=?"
            schedule_parameters += (brand_id,)
            decision_scope = " AND s.brand_id=?"
            decision_parameters = (brand_id,)
            tick_scope = " WHERE brand_id=?"
            tick_parameters = (brand_id,)
        with self._connect() as connection:
            schedules = connection.execute(
                """SELECT COUNT(*) total,SUM(enabled=1) enabled,
                   SUM(enabled=1 AND next_run_at<=?) due FROM periodic_schedules"""
                + schedule_scope,
                schedule_parameters,
            ).fetchone()
            pending = connection.execute(
                """SELECT COUNT(*) count FROM orchestration_decisions d
                   JOIN periodic_schedules s ON s.id=d.schedule_id
                   WHERE d.status='planned'""" + decision_scope,
                decision_parameters,
            ).fetchone()["count"]
            latest = connection.execute(
                "SELECT * FROM orchestration_ticks" + tick_scope
                + " ORDER BY created_at DESC,id DESC LIMIT 1",
                tick_parameters,
            ).fetchone()
        counts = {key: int(schedules[key] or 0) for key in ("total", "enabled", "due")}
        return {"as_of": timestamp, "schedules": counts,
                "pending_decisions": pending,
                "latest_tick": dict(latest) if latest else None}

    def _validate_action(self, action_type: str, payload: Mapping[str, Any]) -> None:
        if action_type == CONNECTOR_SYNC and not all(
                payload.get(key) for key in ("brand_id", "connector_account_id", "stream")):
            raise ValueError("connector sync requires brand_id, connector_account_id, and stream")
        if action_type == OPERATING_PLAN_REFRESH and not all(
                payload.get(key) for key in ("brand_id", "mission_id")):
            raise ValueError("operating plan refresh requires mission_id and brand_id")
        if action_type == ASSISTED_BEEHIIV_PULL and not all(
                payload.get(key) for key in ("brand_id", "connector_account_id")):
            raise ValueError("assisted Beehiiv pull requires brand_id and connector_account_id")
        if action_type == DOWNSTREAM_JOB and str(payload.get("job_type") or "") not in self.safe_downstream_job_types:
            raise ValueError("downstream job type is not explicitly allowlisted as safe")

    def _job_for(self, schedule: Mapping[str, Any], due_at: str) -> tuple[str, str, dict[str, Any]]:
        payload = dict(schedule["payload"])
        action_type = schedule["action_type"]
        self._validate_action(action_type, payload)
        if action_type == CONNECTOR_SYNC:
            job_type, job_payload = SYNC_JOB_TYPE, payload
        elif action_type == OPERATING_PLAN_REFRESH:
            # Keep the instant intact. The handler resolves the civil day from
            # the mission timezone, avoiding UTC-date rollover errors.
            job_type, job_payload = OPERATING_PLAN_JOB_TYPE, {**payload, "plan_at": due_at}
        elif action_type == ASSISTED_BEEHIIV_PULL:
            job_type = ASSISTED_BEEHIIV_PULL_JOB_TYPE
            job_payload = {**payload, "scheduled_for": due_at}
        else:
            job_type = str(payload.pop("job_type"))
            job_payload = dict(payload.pop("payload", {}))
        key = f"periodic:{schedule.get('schedule_key') or schedule['schedule_id']}:{due_at}"
        return job_type, key, job_payload

    @staticmethod
    def _schedule(row: dict[str, Any]) -> dict[str, Any]:
        row["payload"] = json.loads(row["payload"] or "{}")
        row["enabled"] = bool(row["enabled"])
        return row

    def _now_iso(self) -> str:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _iso(value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return parsed.astimezone(UTC).isoformat()

    @staticmethod
    def _next_due(due_at: str, interval_seconds: int, as_of: str) -> str:
        due, current = datetime.fromisoformat(due_at), datetime.fromisoformat(as_of)
        periods = int(max(0.0, (current - due).total_seconds()) // interval_seconds) + 1
        return (due + timedelta(seconds=periods * interval_seconds)).isoformat()


def make_operating_plan_handler(database: str | Path):
    service = build_operating_plan_service(database)

    def handle(job: dict[str, Any]) -> dict[str, Any]:
        payload = job["payload"]
        plan_value = payload.get("plan_at") or payload.get("plan_date")
        morning = service.create_morning_plan(payload["mission_id"], plan_value)
        scorecard = service.create_eod_scorecard(payload["mission_id"], plan_value)
        plan_date = morning["artifact_date"]
        return {"mission_id": payload["mission_id"], "plan_date": plan_date,
                "morning_plan_artifact_id": morning["id"],
                "scorecard_artifact_id": scorecard["id"]}

    return handle


__all__ = ["ASSISTED_BEEHIIV_PULL", "CONNECTOR_SYNC", "DOWNSTREAM_JOB", "OPERATING_PLAN_JOB_TYPE",
           "OPERATING_PLAN_REFRESH", "PeriodicOrchestrator", "TickResult",
           "make_operating_plan_handler"]
