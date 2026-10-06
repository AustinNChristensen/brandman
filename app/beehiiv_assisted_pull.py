"""Durable, read-only Beehiiv pulls for browser/MCP execution helpers.

These tasks are deliberately separate from content-delivery handoffs: they never
carry newsletter bodies and cannot draft, schedule, send, or publish anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from app.beehiiv_assisted_sync import ingest_beehiiv_pull
from app.campaign_graph import CampaignGraphStore


ASSISTED_BEEHIIV_PULL_JOB_TYPE = "beehiiv.assisted_pull.request"


class BeehiivAssistedPullError(ValueError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS beehiiv_assisted_pull_tasks (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL, connector_account_id TEXT NOT NULL,
  task_type TEXT NOT NULL CHECK(task_type='metadata_and_aggregate_measurements'),
  scheduled_for TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('pending','claimed','completed','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
  claimed_by TEXT, claimed_at TEXT, claim_expires_at TEXT, claim_token_hash TEXT,
  receipt_fingerprint TEXT, observed_at TEXT, posts_received INTEGER,
  metadata_synced INTEGER, measurements_recorded INTEGER,
  post_measurements_received INTEGER, campaign_metrics_recorded INTEGER,
  measured_campaign_ids_json TEXT NOT NULL DEFAULT '[]',
  completed_at TEXT,
  last_failure_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(connector_account_id,scheduled_for)
);
CREATE INDEX IF NOT EXISTS beehiiv_assisted_pull_brand_status
  ON beehiiv_assisted_pull_tasks(brand_id,status,scheduled_for);
CREATE TABLE IF NOT EXISTS beehiiv_assisted_pull_audit (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
  action TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS beehiiv_assisted_pull_audit_task
  ON beehiiv_assisted_pull_audit(task_id,sequence);
"""


class BeehiivAssistedPullStore:
    """Lease/retry/receipt lifecycle for aggregate, provider-read-only work."""

    def __init__(
        self, database: str | Path, *, clock: Callable[[], datetime] | None = None,
        agent_freshness_seconds: int = 900,
    ) -> None:
        self.database = str(database)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.agent_freshness_seconds = agent_freshness_seconds
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(beehiiv_assisted_pull_tasks)"
                )
            }
            for name in ("post_measurements_received", "campaign_metrics_recorded"):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE beehiiv_assisted_pull_tasks ADD COLUMN {name} INTEGER"
                    )
            if "measured_campaign_ids_json" not in columns:
                connection.execute(
                    """ALTER TABLE beehiiv_assisted_pull_tasks
                       ADD COLUMN measured_campaign_ids_json TEXT NOT NULL DEFAULT '[]'"""
                )
        # Initialize campaign measurement storage outside any receipt writer
        # transaction so exact post aggregates can project atomically later.
        CampaignGraphStore(database)

    def ensure(
        self, *, brand_id: str, connector_account_id: str, scheduled_for: str,
        max_attempts: int = 3, actor: str = "brand-os-scheduler",
    ) -> dict[str, Any]:
        due = self._iso(scheduled_for)
        if not 1 <= max_attempts <= 10:
            raise BeehiivAssistedPullError("max_attempts must be between 1 and 10")
        timestamp = self._now()
        key = f"assisted-beehiiv-pull:{connector_account_id}:{due}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_account(connection, brand_id, connector_account_id)
            inserted = connection.execute(
                """INSERT INTO beehiiv_assisted_pull_tasks
                   (id,brand_id,connector_account_id,task_type,scheduled_for,
                    idempotency_key,status,max_attempts,created_at,updated_at)
                   VALUES (?,?,?,'metadata_and_aggregate_measurements',?,?,'pending',?,?,?)
                   ON CONFLICT(connector_account_id,scheduled_for) DO NOTHING""",
                (str(uuid4()), brand_id, connector_account_id, due, key,
                 max_attempts, timestamp, timestamp),
            )
            row = connection.execute(
                """SELECT * FROM beehiiv_assisted_pull_tasks
                   WHERE connector_account_id=? AND scheduled_for=?""",
                (connector_account_id, due),
            ).fetchone()
            assert row is not None
            if row["brand_id"] != brand_id or row["idempotency_key"] != key:
                raise BeehiivAssistedPullError("scheduled pull identity conflict")
            if inserted.rowcount == 1:
                self._audit(connection, row["id"], "requested", actor, {
                    "scheduled_for": due,
                    "scope": "post_metadata_and_aggregate_measurements_only",
                })
        return self._decode(row)

    def list(self, brand_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
        self.recover_expired()
        query = "SELECT * FROM beehiiv_assisted_pull_tasks WHERE brand_id=?"
        values: list[Any] = [brand_id]
        if status is not None:
            if status not in {"pending", "claimed", "completed", "failed"}:
                raise BeehiivAssistedPullError("invalid assisted pull status")
            query += " AND status=?"; values.append(status)
        with self._connect() as connection:
            rows = connection.execute(query + " ORDER BY scheduled_for,id", values).fetchall()
        return [self._decode(row) for row in rows]

    def get(self, task_id: str) -> dict[str, Any]:
        self.recover_expired()
        return self._get(task_id, internal=False)

    def claim(
        self, task_id: str, *, actor: str, lease_seconds: int = 900,
    ) -> dict[str, Any]:
        if not _SAFE_ACTOR.fullmatch(actor):
            raise BeehiivAssistedPullError("actor must be a safe execution-agent identifier")
        if not 60 <= lease_seconds <= 3600:
            raise BeehiivAssistedPullError("lease_seconds must be between 60 and 3600")
        self.recover_expired()
        token = secrets.token_urlsafe(32)
        now = self._clock()
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM beehiiv_assisted_pull_tasks WHERE id=?", (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError("assisted Beehiiv pull not found")
            if row["status"] != "pending":
                raise BeehiivAssistedPullError("assisted Beehiiv pull is not available to claim")
            self._assert_account(connection, row["brand_id"], row["connector_account_id"])
            if not self._agent_is_fresh(connection, row["brand_id"], actor):
                raise BeehiivAssistedPullError(
                    "actor must be an enabled execution agent with a fresh heartbeat"
                )
            if not self._provider_enabled(connection, row["brand_id"]):
                raise BeehiivAssistedPullError("Beehiiv assisted work is disabled")
            updated = connection.execute(
                """UPDATE beehiiv_assisted_pull_tasks SET status='claimed',claimed_by=?,
                   claimed_at=?,claim_expires_at=?,claim_token_hash=?,attempt_count=attempt_count+1,
                   last_failure_code=NULL,updated_at=? WHERE id=? AND status='pending'""",
                (actor, now.isoformat(), expires, _token_hash(token), now.isoformat(), task_id),
            )
            if updated.rowcount != 1:
                raise BeehiivAssistedPullError("assisted Beehiiv pull claim changed")
            self._audit(connection, task_id, "claimed", actor, {"claim_expires_at": expires})
            result = connection.execute(
                "SELECT * FROM beehiiv_assisted_pull_tasks WHERE id=?", (task_id,),
            ).fetchone()
        return {**self._decode(result), "claim_token": token}

    def heartbeat(
        self, task_id: str, *, actor: str, claim_token: str,
        lease_seconds: int = 900,
    ) -> dict[str, Any]:
        if not 60 <= lease_seconds <= 3600:
            raise BeehiivAssistedPullError("lease_seconds must be between 60 and 3600")
        self.recover_expired()
        now = self._clock(); expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM beehiiv_assisted_pull_tasks WHERE id=?", (task_id,),
            ).fetchone()
            self._assert_claim(row, actor, claim_token, now)
            if not self._agent_is_fresh(connection, row["brand_id"], actor):
                raise BeehiivAssistedPullError("execution-agent heartbeat is stale")
            if not self._provider_enabled(connection, row["brand_id"]):
                raise BeehiivAssistedPullError("Beehiiv assisted work is disabled")
            connection.execute(
                "UPDATE beehiiv_assisted_pull_tasks SET claim_expires_at=?,updated_at=? WHERE id=?",
                (expires, now.isoformat(), task_id),
            )
            self._audit(connection, task_id, "lease_renewed", actor, {
                "claim_expires_at": expires,
            })
            result = connection.execute(
                "SELECT * FROM beehiiv_assisted_pull_tasks WHERE id=?", (task_id,),
            ).fetchone()
        return self._decode(result)

    def submit_receipt(
        self, task_id: str, *, actor: str, claim_token: str,
        observed_at: str, posts: Sequence[Mapping[str, Any]],
        publication_stats: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.recover_expired()
        fingerprint = _receipt_fingerprint(observed_at, posts, publication_stats)
        current = self._get(task_id, internal=True)
        if current["status"] == "completed":
            if secrets.compare_digest(str(current.get("receipt_fingerprint") or ""), fingerprint):
                return self.get(task_id)
            raise BeehiivAssistedPullError("completed pull has a different receipt")
        self._assert_claim_mapping(current, actor, claim_token, self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM beehiiv_assisted_pull_tasks WHERE id=?", (task_id,),
            ).fetchone()
            self._assert_claim(row, actor, claim_token, self._clock())
            self._assert_account(connection, row["brand_id"], row["connector_account_id"])
            if not self._provider_enabled(connection, row["brand_id"]):
                raise BeehiivAssistedPullError("Beehiiv assisted work is disabled")
            # Provider control, claim validation, projections, receipt, and audit
            # share one writer transaction. A concurrent kill switch therefore
            # linearizes entirely before (deny, zero writes) or after completion.
            outcome = ingest_beehiiv_pull(
                self.database, brand_id=row["brand_id"],
                connector_account_id=row["connector_account_id"], posts=posts,
                publication_stats=publication_stats, observed_at=observed_at,
                connection=connection, require_post_measurement=True,
            )
            timestamp = self._now()
            updated = connection.execute(
                """UPDATE beehiiv_assisted_pull_tasks SET status='completed',
                   receipt_fingerprint=?,observed_at=?,posts_received=?,metadata_synced=?,
                   measurements_recorded=?,post_measurements_received=?,
                   campaign_metrics_recorded=?,measured_campaign_ids_json=?,
                   completed_at=?,claim_token_hash=NULL,
                   claim_expires_at=NULL,updated_at=? WHERE id=? AND status='claimed'""",
                (fingerprint, outcome["observed_at"], outcome["posts_received"],
                 outcome["metadata_synced"], outcome["recorded"],
                 outcome["post_measurements_received"],
                 outcome["campaign_metrics_projected"],
                 json.dumps(outcome["measured_campaign_ids"], sort_keys=True),
                 timestamp, timestamp, task_id),
            )
            if updated.rowcount != 1:
                raise BeehiivAssistedPullError("assisted pull changed before receipt commit")
            self._audit(connection, task_id, "aggregate_receipt_recorded", actor, {
                "posts_received": outcome["posts_received"],
                "metadata_synced": outcome["metadata_synced"],
                "measurements_recorded": outcome["recorded"],
                "post_measurements_received": outcome["post_measurements_received"],
                "campaign_metrics_recorded": outcome["campaign_metrics_projected"],
                "measured_campaign_ids": outcome["measured_campaign_ids"],
                "privacy": "aggregate_only_no_subscriber_records",
            })
            result = connection.execute(
                "SELECT * FROM beehiiv_assisted_pull_tasks WHERE id=?", (task_id,),
            ).fetchone()
        return self._decode(result)

    def record_failure(
        self, task_id: str, *, actor: str, claim_token: str, failure_code: str,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", failure_code):
            raise BeehiivAssistedPullError("failure_code must be a safe machine code")
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM beehiiv_assisted_pull_tasks WHERE id=?", (task_id,),
            ).fetchone()
            self._assert_claim(row, actor, claim_token, now)
            exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
            status = "failed" if exhausted else "pending"
            connection.execute(
                """UPDATE beehiiv_assisted_pull_tasks SET status=?,claimed_by=NULL,
                   claimed_at=NULL,claim_expires_at=NULL,claim_token_hash=NULL,
                   last_failure_code=?,updated_at=? WHERE id=?""",
                (status, failure_code, now.isoformat(), task_id),
            )
            self._audit(connection, task_id, "failed" if exhausted else "retry_requested", actor, {
                "failure_code": failure_code, "attempt": int(row["attempt_count"]),
            })
            result = connection.execute(
                "SELECT * FROM beehiiv_assisted_pull_tasks WHERE id=?", (task_id,),
            ).fetchone()
        return self._decode(result)

    def recover_expired(self) -> int:
        timestamp = self._now(); recovered = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT * FROM beehiiv_assisted_pull_tasks
                   WHERE status='claimed' AND claim_expires_at<=?""", (timestamp,),
            ).fetchall()
            for row in rows:
                exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
                status = "failed" if exhausted else "pending"
                connection.execute(
                    """UPDATE beehiiv_assisted_pull_tasks SET status=?,claimed_by=NULL,
                       claimed_at=NULL,claim_expires_at=NULL,claim_token_hash=NULL,
                       last_failure_code='lease_expired',updated_at=? WHERE id=?""",
                    (status, timestamp, row["id"]),
                )
                self._audit(connection, row["id"],
                            "lease_exhausted" if exhausted else "lease_expired",
                            "brand-os", {"previous_claimant": row["claimed_by"]})
                recovered += 1
        return recovered

    def audit(self, task_id: str) -> list[dict[str, Any]]:
        self.get(task_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM beehiiv_assisted_pull_audit WHERE task_id=? ORDER BY sequence",
                (task_id,),
            ).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail_json"])} for row in rows]

    def _get(self, task_id: str, *, internal: bool) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM beehiiv_assisted_pull_tasks WHERE id=?", (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError("assisted Beehiiv pull not found")
        return self._decode(row, internal=internal)

    def _decode(self, row: sqlite3.Row, *, internal: bool = False) -> dict[str, Any]:
        result = dict(row); token_hash = result.pop("claim_token_hash")
        if internal:
            result["_claim_token_hash"] = token_hash
        result["read_only"] = True
        result["requires_post_measurement"] = True
        result["measured_campaign_ids"] = json.loads(
            result.pop("measured_campaign_ids_json", "[]") or "[]"
        )
        result["allowed_data"] = "post_metadata_and_aggregate_measurements"
        result["forbidden_actions"] = ["subscriber_data", "draft", "schedule", "send", "publish"]
        return result

    def _assert_account(
        self, connection: sqlite3.Connection, brand_id: str, account_id: str,
    ) -> None:
        row = connection.execute(
            """SELECT a.*,COALESCE(c.configuration,'{}') configuration
               FROM connector_accounts a LEFT JOIN connector_account_configurations c
                 ON c.connector_account_id=a.id WHERE a.id=? AND a.brand_id=?""",
            (account_id, brand_id),
        ).fetchone()
        if row is None or row["connector_type"] != "beehiiv" or row["status"] not in {
            "connected", "healthy", "active",
        }:
            raise BeehiivAssistedPullError("connected same-brand Beehiiv account is required")
        configuration = json.loads(row["configuration"] or "{}")
        if configuration.get("delivery_mode") not in {"browser_assisted", "mcp_assisted"}:
            raise BeehiivAssistedPullError("Beehiiv account is not assisted-mode")

    def _agent_is_fresh(
        self, connection: sqlite3.Connection, brand_id: str, actor: str,
    ) -> bool:
        row = connection.execute(
            """SELECT last_heartbeat_at FROM execution_agents
               WHERE brand_id=? AND agent_id=? AND enabled=1""", (brand_id, actor),
        ).fetchone()
        if row is None or not row["last_heartbeat_at"]:
            return False
        try:
            observed = self._parse(row["last_heartbeat_at"])
        except (TypeError, ValueError):
            return False
        age = (self._clock() - observed).total_seconds()
        return 0 <= age <= self.agent_freshness_seconds

    @staticmethod
    def _provider_enabled(connection: sqlite3.Connection, brand_id: str) -> bool:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_provider_controls'"
        ).fetchone()
        if exists is None:
            return True
        rows = connection.execute(
            """SELECT provider,enabled FROM execution_provider_controls
               WHERE brand_id=? AND provider IN ('all','beehiiv')""", (brand_id,),
        ).fetchall()
        controls = {row["provider"]: bool(row["enabled"]) for row in rows}
        return controls.get("all", True) and controls.get("beehiiv", True)

    def _assert_claim(
        self, row: sqlite3.Row | None, actor: str, claim_token: str, now: datetime,
    ) -> None:
        if row is None:
            raise KeyError("assisted Beehiiv pull not found")
        self._assert_claim_mapping(dict(row), actor, claim_token, now)

    def _assert_claim_mapping(
        self, row: Mapping[str, Any], actor: str, claim_token: str, now: datetime,
    ) -> None:
        token_hash = row.get("_claim_token_hash", row.get("claim_token_hash"))
        if (
            row.get("status") != "claimed" or row.get("claimed_by") != actor
            or not token_hash or not secrets.compare_digest(str(token_hash), _token_hash(claim_token))
        ):
            raise BeehiivAssistedPullError("valid active claim is required")
        if self._parse(str(row["claim_expires_at"])) <= now:
            raise BeehiivAssistedPullError("assisted Beehiiv pull claim expired")

    def _audit(
        self, connection: sqlite3.Connection, task_id: str, action: str,
        actor: str, detail: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """INSERT INTO beehiiv_assisted_pull_audit
               (task_id,action,actor,at,detail_json) VALUES (?,?,?,?,?)""",
            (task_id, action, actor, self._now(), json.dumps(dict(detail), sort_keys=True)),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _clock(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("clock must be timezone-aware")
        return value.astimezone(UTC)

    def _now(self) -> str:
        return self._clock().isoformat()

    @staticmethod
    def _parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)

    @classmethod
    def _iso(cls, value: str) -> str:
        parsed = cls._parse(value)
        if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise BeehiivAssistedPullError("scheduled_for must include a timezone")
        return parsed.isoformat()


def make_assisted_beehiiv_pull_handler(database: str | Path):
    tasks = BeehiivAssistedPullStore(database)

    def handle(job: Mapping[str, Any]) -> dict[str, Any]:
        payload = job["payload"]
        return tasks.ensure(
            brand_id=payload["brand_id"],
            connector_account_id=payload["connector_account_id"],
            scheduled_for=payload["scheduled_for"],
        )

    return handle


def _receipt_fingerprint(
    observed_at: str, posts: Sequence[Mapping[str, Any]],
    publication_stats: Mapping[str, Any] | None,
) -> str:
    return "sha256:" + sha256(json.dumps({
        "observed_at": observed_at, "posts": list(posts),
        "publication_stats": publication_stats,
    }, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _token_hash(token: str) -> str:
    return sha256(token.encode()).hexdigest()


_SAFE_ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,99}")


__all__ = [
    "ASSISTED_BEEHIIV_PULL_JOB_TYPE", "BeehiivAssistedPullError",
    "BeehiivAssistedPullStore", "make_assisted_beehiiv_pull_handler",
]
