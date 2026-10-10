"""Durable, read-only connector health-check orchestration.

Health probes call only existing read connectors. A daemon probe thread bounds the
worker's wait; Python cannot forcibly cancel an in-progress blocking request, so a
timed-out request may finish in the background. Production transports must also
enforce their own socket/TLS timeout (the bundled transport does). Late results are
ignored and can never overwrite the durable timed-out status.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import queue
import sqlite3
import threading
from typing import Any, Mapping
from uuid import uuid4

from brandman import store
from brandman.connectors import ConnectorError, ReadConnector


HEALTH_CHECK_JOB_TYPE = "connector.health_check"
_ACTIVE = {"healthy", "connected", "degraded", "needs_attention"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS connector_health_checks (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL REFERENCES brands(id),
  connector_account_id TEXT NOT NULL REFERENCES connector_accounts(id),
  connector_type TEXT NOT NULL, status TEXT NOT NULL,
  provider_responded INTEGER NOT NULL DEFAULT 0,
  response_status INTEGER, error_code TEXT, timeout_seconds REAL NOT NULL,
  requested_by TEXT NOT NULL, requested_at TEXT NOT NULL,
  started_at TEXT, completed_at TEXT, job_id TEXT
);
CREATE INDEX IF NOT EXISTS connector_health_latest
  ON connector_health_checks(connector_account_id,requested_at DESC);
CREATE TABLE IF NOT EXISTS connector_health_audit (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  health_check_id TEXT NOT NULL REFERENCES connector_health_checks(id),
  connector_account_id TEXT NOT NULL, action TEXT NOT NULL,
  actor TEXT NOT NULL, at TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}'
);
"""


class ConnectorHealthStore:
    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def trigger(
        self, brand_id: str, *, actor: str, connector_account_id: str | None = None,
        timeout_seconds: float = 20,
    ) -> list[dict[str, Any]]:
        if not actor.strip():
            raise ValueError("actor is required")
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 1 and 60")
        query = "SELECT * FROM connector_accounts WHERE brand_id=?"
        values: tuple[Any, ...] = (brand_id,)
        if connector_account_id:
            query += " AND id=?"
            values += (connector_account_id,)
        query += " ORDER BY connector_type,id"
        with self._connect() as connection:
            accounts = connection.execute(query, values).fetchall()
        if connector_account_id and not accounts:
            raise KeyError("unknown connector account")
        results = []
        for account in accounts:
            check_id = str(uuid4())
            timestamp = _now()
            reason = _not_probeable_reason(account)
            status = "not_probeable" if reason else "queued"
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO connector_health_checks
                       (id,brand_id,connector_account_id,connector_type,status,
                        provider_responded,response_status,error_code,timeout_seconds,
                        requested_by,requested_at,started_at,completed_at,job_id)
                       VALUES (:id,:brand_id,:account_id,:connector_type,:status,0,NULL,
                               :error_code,:timeout_seconds,:actor,:requested_at,NULL,
                               :completed_at,NULL)""",
                    {
                        "id": check_id, "brand_id": brand_id,
                        "account_id": account["id"],
                        "connector_type": account["connector_type"],
                        "status": status, "error_code": reason,
                        "timeout_seconds": timeout_seconds, "actor": actor,
                        "requested_at": timestamp,
                        "completed_at": timestamp if reason else None,
                    },
                )
                self._audit(
                    connection, check_id, account["id"],
                    "skipped" if reason else "queued", actor, timestamp,
                    {"reason_code": reason} if reason else {},
                )
            if reason is None:
                job = store.enqueue_job(
                    HEALTH_CHECK_JOB_TYPE, f"health-check:{check_id}",
                    {"health_check_id": check_id}, brand_id=brand_id,
                    connector_account_id=account["id"], max_attempts=1,
                )
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE connector_health_checks SET job_id=? WHERE id=?",
                        (job["id"], check_id),
                    )
            results.append(self.get(check_id))
        return results

    def start(self, check_id: str, *, actor: str) -> dict[str, Any]:
        timestamp = _now()
        with self._connect() as connection:
            updated = connection.execute(
                """UPDATE connector_health_checks SET status='running',started_at=?
                   WHERE id=? AND status='queued'""", (timestamp, check_id),
            )
            if not updated.rowcount:
                raise ValueError("health check is not queued")
            row = connection.execute(
                "SELECT connector_account_id FROM connector_health_checks WHERE id=?",
                (check_id,),
            ).fetchone()
            self._audit(connection, check_id, row["connector_account_id"], "started", actor, timestamp, {})
        return self.get(check_id)

    def complete(
        self, check_id: str, *, status: str, actor: str,
        provider_responded: bool, response_status: int | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"healthy", "unhealthy", "failed", "timed_out", "not_configured"}:
            raise ValueError("invalid health status")
        if status in {"healthy", "unhealthy"} and not provider_responded:
            raise ValueError("provider health cannot be claimed without a response")
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM connector_health_checks WHERE id=?", (check_id,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown health check")
            if row["status"] != "running":
                raise ValueError("health check is not running")
            connection.execute(
                """UPDATE connector_health_checks SET status=?,provider_responded=?,
                   response_status=?,error_code=?,completed_at=? WHERE id=?""",
                (status, int(provider_responded), response_status, error_code, timestamp, check_id),
            )
            if provider_responded:
                connection.execute(
                    """UPDATE connector_accounts SET status=?,health_checked_at=?,last_error=?,updated_at=?
                       WHERE id=?""",
                    (
                        "healthy" if status == "healthy" else "degraded",
                        timestamp, error_code, timestamp, row["connector_account_id"],
                    ),
                )
            self._audit(
                connection, check_id, row["connector_account_id"], "completed", actor,
                timestamp, {"status": status, "provider_responded": provider_responded,
                            "response_status": response_status, "error_code": error_code},
            )
        return self.get(check_id)

    def get(self, check_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM connector_health_checks WHERE id=?", (check_id,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown health check")
            audits = connection.execute(
                """SELECT sequence,action,actor,at,details FROM connector_health_audit
                   WHERE health_check_id=? ORDER BY sequence""", (check_id,),
            ).fetchall()
        result = dict(row)
        result["provider_responded"] = bool(result["provider_responded"])
        result["audit"] = [
            {**dict(item), "details": json.loads(item["details"])} for item in audits
        ]
        return result

    def list(self, brand_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as connection:
            ids = [row["id"] for row in connection.execute(
                """SELECT id FROM connector_health_checks WHERE brand_id=?
                   ORDER BY requested_at DESC,id DESC LIMIT ?""", (brand_id, limit),
            ).fetchall()]
        return [self.get(check_id) for check_id in ids]

    @staticmethod
    def _audit(connection, check_id, account_id, action, actor, at, details):
        connection.execute(
            """INSERT INTO connector_health_audit
               (health_check_id,connector_account_id,action,actor,at,details)
               VALUES (?,?,?,?,?,?)""",
            (check_id, account_id, action, actor, at, json.dumps(details, sort_keys=True)),
        )


def make_connector_health_handler(
    connectors: Mapping[str, ReadConnector], health: ConnectorHealthStore,
):
    def handle(job: dict[str, Any]) -> dict[str, Any]:
        check_id = job["payload"]["health_check_id"]
        check = health.start(check_id, actor="bounded-worker")
        connector = connectors.get(check["connector_account_id"])
        if connector is None:
            return health.complete(
                check_id, status="not_configured", actor="bounded-worker",
                provider_responded=False, error_code="connector.not_registered",
            )
        output: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def probe() -> None:
            try:
                connector.sync(None)
                output.put(("success", None))
            except BaseException as exc:  # contained and redacted below
                output.put(("error", exc))

        thread = threading.Thread(target=probe, name=f"health-{check_id}", daemon=True)
        thread.start()
        thread.join(float(check["timeout_seconds"]))
        if thread.is_alive():
            return health.complete(
                check_id, status="timed_out", actor="bounded-worker",
                provider_responded=False, error_code="probe.timeout",
            )
        outcome, value = output.get_nowait()
        if outcome == "success":
            return health.complete(
                check_id, status="healthy", actor="bounded-worker",
                provider_responded=True, response_status=None,
            )
        if isinstance(value, ConnectorError):
            responded = value.status_code is not None
            return health.complete(
                check_id, status="unhealthy" if responded else "failed",
                actor="bounded-worker", provider_responded=responded,
                response_status=value.status_code,
                error_code=_safe_error(value),
            )
        return health.complete(
            check_id, status="failed", actor="bounded-worker",
            provider_responded=False, error_code=f"probe.{type(value).__name__.lower()}",
        )

    return handle


def register_connector_health(worker, connectors, health: ConnectorHealthStore) -> None:
    worker.register(HEALTH_CHECK_JOB_TYPE, make_connector_health_handler(connectors, health))


def _not_probeable_reason(account: sqlite3.Row) -> str | None:
    if account["status"] not in _ACTIVE:
        return "account.not_active"
    scopes = set(json.loads(account["scopes"] or "[]"))
    capabilities = set(json.loads(account["capabilities"] or "[]"))
    kind = account["connector_type"]
    if kind == "rss":
        return None if "content.read" in capabilities else "scope.content.read_missing"
    required = {
        "beehiiv": {"posts.read"},
        "x": {"tweet.read", "users.read", "offline.access"},
        "website": {"analytics.read"},
    }.get(kind)
    if required is None:
        return "connector.unsupported"
    if kind == "x" and any(scope.endswith(".write") for scope in scopes):
        return "scope.read_write_not_separated"
    missing = required - scopes
    return "scope." + "+".join(sorted(missing)) + "_missing" if missing else None


def _safe_error(error: ConnectorError) -> str:
    operation = "_".join(
        part for part in error.operation.lower().replace(":", " ").split()
        if part.replace("_", "").isalnum()
    )[:80] or "request"
    suffix = f".http_{error.status_code}" if error.status_code is not None else ""
    return f"{error.connector.value}.{operation}{suffix}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "ConnectorHealthStore", "HEALTH_CHECK_JOB_TYPE",
    "make_connector_health_handler", "register_connector_health",
]
