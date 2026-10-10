"""Governed onboarding for public third-party RSS/Atom sources.

Onboarding is deliberately configuration-only: it performs no network request,
stores no article body, and exposes no generic scraping surface. The existing RSS
connector later reads only syndicated title/summary/link metadata on a bounded
periodic schedule.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import ipaddress
import json
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from brandman.scheduler import CONNECTOR_SYNC, PeriodicOrchestrator


class ThirdPartySourceError(RuntimeError):
    """A safe source configuration or lifecycle error."""


CONTENT_POLICY = "syndicated_title_summary_link_only"
DEFAULT_POLLING_SECONDS = 1800
_ACTIVE_JOB_STATES = ("queued", "retry", "running")

SCHEMA = """
CREATE TABLE IF NOT EXISTS third_party_source_configs (
  connector_account_id TEXT PRIMARY KEY REFERENCES connector_accounts(id),
  brand_id TEXT NOT NULL REFERENCES brands(id), publisher_name TEXT NOT NULL,
  homepage_url TEXT, feed_url TEXT NOT NULL, feed_format TEXT NOT NULL,
  content_policy TEXT NOT NULL, polling_interval_seconds INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(brand_id, feed_url)
);
CREATE INDEX IF NOT EXISTS third_party_source_configs_brand
  ON third_party_source_configs(brand_id, enabled, publisher_name);
CREATE TABLE IF NOT EXISTS third_party_source_audit (
  id TEXT PRIMARY KEY, connector_account_id TEXT NOT NULL,
  action TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
  FOREIGN KEY(connector_account_id) REFERENCES connector_accounts(id)
);
CREATE INDEX IF NOT EXISTS third_party_source_audit_account
  ON third_party_source_audit(connector_account_id, created_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def validate_public_feed_url(value: str) -> str:
    """Accept a public HTTPS feed URL without credentials or local-network targets."""
    raw = value.strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise ValueError("feed_url must be a valid public HTTPS URL") from exc
    if parts.scheme.casefold() != "https" or not parts.hostname:
        raise ValueError("feed_url must use HTTPS and include a public hostname")
    if parts.username or parts.password or parts.fragment or parts.query:
        raise ValueError("feed_url cannot contain credentials, query parameters, or a fragment")
    if port not in {None, 443}:
        raise ValueError("feed_url must use the standard HTTPS port")
    hostname = parts.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise ValueError("feed_url hostname must be public")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("feed_url must use a public DNS hostname, not an IP address")
    if "." not in hostname:
        raise ValueError("feed_url hostname must be public")
    path = parts.path or "/"
    return urlunsplit(("https", hostname, path, "", ""))


def validate_homepage_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    parts = urlsplit(value.strip())
    if parts.scheme.casefold() != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("homepage_url must be a public HTTPS URL without credentials")
    hostname = parts.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise ValueError("homepage_url hostname must be public")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("homepage_url must use a DNS hostname")
    return urlunsplit(("https", hostname, parts.path or "/", "", ""))


class ThirdPartySourceService:
    def __init__(self, database: str | Path, *, clock: Callable[[], str] = _now) -> None:
        self.database = Path(database)
        self.clock = clock
        self.scheduler = PeriodicOrchestrator(self.database)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def onboard(
        self, brand_id: str, *, publisher_name: str, feed_url: str,
        actor: str, reason: str, homepage_url: str | None = None,
        feed_format: str = "auto", polling_interval_seconds: int = DEFAULT_POLLING_SECONDS,
    ) -> dict[str, Any]:
        self._require_governance(actor, reason)
        if not publisher_name.strip():
            raise ValueError("publisher_name is required")
        if feed_format not in {"auto", "rss", "atom"}:
            raise ValueError("feed_format must be auto, rss, or atom")
        if not 900 <= polling_interval_seconds <= 86_400:
            raise ValueError("polling_interval_seconds must be between 900 and 86400")
        canonical_feed = validate_public_feed_url(feed_url)
        canonical_homepage = validate_homepage_url(homepage_url)
        timestamp = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT connector_account_id FROM third_party_source_configs WHERE brand_id=? AND feed_url=?",
                (brand_id, canonical_feed),
            ).fetchone()
            if existing is not None:
                return self.get(existing["connector_account_id"])
            existing_account = connection.execute(
                """SELECT id FROM connector_accounts
                   WHERE brand_id=? AND connector_type='rss' AND account_key=?""",
                (brand_id, canonical_feed),
            ).fetchone()
            account_id = existing_account["id"] if existing_account else str(uuid4())
            if existing_account:
                # Adopt legacy metadata into governance without changing its
                # stable account identity or sync history.
                connection.execute(
                    """UPDATE connector_accounts SET display_name=?,status='connected',
                       scopes='[]',capabilities='["content.read"]',last_error=NULL,updated_at=?
                       WHERE id=?""",
                    (publisher_name.strip(), timestamp, account_id),
                )
            else:
                connection.execute(
                    """INSERT INTO connector_accounts
                       (id,brand_id,connector_type,account_key,display_name,status,scopes,
                        capabilities,health_checked_at,last_error,created_at,updated_at)
                       VALUES (?,?, 'rss', ?,?, 'connected','[]','["content.read"]',NULL,NULL,?,?)""",
                    (account_id, brand_id, canonical_feed, publisher_name.strip(), timestamp, timestamp),
                )
            connection.execute(
                """INSERT INTO third_party_source_configs
                   (connector_account_id,brand_id,publisher_name,homepage_url,feed_url,
                    feed_format,content_policy,polling_interval_seconds,enabled,created_by,
                    created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,1,?,?,?)""",
                (account_id, brand_id, publisher_name.strip(), canonical_homepage,
                 canonical_feed, feed_format, CONTENT_POLICY, polling_interval_seconds,
                 actor.strip(), timestamp, timestamp),
            )
            self._audit(connection, account_id, "onboarded", actor, reason, {
                "feed_format": feed_format,
                "polling_interval_seconds": polling_interval_seconds,
                "content_policy": CONTENT_POLICY,
            }, timestamp)
        self.scheduler.ensure_schedule(
            self._schedule_key(account_id), name=f"Sync {publisher_name.strip()}",
            action_type=CONNECTOR_SYNC, interval_seconds=polling_interval_seconds,
            payload={"brand_id": brand_id, "connector_account_id": account_id, "stream": "content"},
            brand_id=brand_id, connector_account_id=account_id,
        )
        return self.get(account_id)

    def set_enabled(
        self, connector_account_id: str, enabled: bool, *, actor: str, reason: str,
    ) -> dict[str, Any]:
        self._require_governance(actor, reason)
        timestamp = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            config = connection.execute(
                "SELECT * FROM third_party_source_configs WHERE connector_account_id=?",
                (connector_account_id,),
            ).fetchone()
            if config is None:
                raise KeyError(f"unknown third-party source: {connector_account_id}")
            if bool(config["enabled"]) == enabled:
                raise ThirdPartySourceError(
                    f"third-party source is already {'enabled' if enabled else 'disabled'}"
                )
            connection.execute(
                "UPDATE third_party_source_configs SET enabled=?,updated_at=? WHERE connector_account_id=?",
                (int(enabled), timestamp, connector_account_id),
            )
            connection.execute(
                "UPDATE connector_accounts SET status=?,updated_at=? WHERE id=?",
                ("connected" if enabled else "disconnected", timestamp, connector_account_id),
            )
            cancelled = 0
            if not enabled:
                jobs = connection.execute(
                    """SELECT id,payload FROM durable_jobs WHERE connector_account_id=?
                       AND job_type='connector.sync' AND status IN ('queued','retry')""",
                    (connector_account_id,),
                ).fetchall()
                for job in jobs:
                    try:
                        matches = json.loads(job["payload"]).get("connector_account_id") == connector_account_id
                    except (TypeError, ValueError, AttributeError):
                        matches = False
                    if matches:
                        cancelled += connection.execute(
                            """UPDATE durable_jobs SET status='cancelled',updated_at=?,last_error=?
                               WHERE id=? AND status IN ('queued','retry')""",
                            (timestamp, "source disabled by operator", job["id"]),
                        ).rowcount
            self._audit(connection, connector_account_id,
                        "enabled" if enabled else "disabled", actor, reason,
                        {"queued_jobs_cancelled": cancelled}, timestamp)
        self.scheduler.set_schedule_enabled(self._schedule_key(connector_account_id), enabled)
        return self.get(connector_account_id)

    def get(self, connector_account_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT c.*,a.display_name,a.status AS connector_status,a.last_error,
                          a.health_checked_at
                   FROM third_party_source_configs c JOIN connector_accounts a
                     ON a.id=c.connector_account_id
                   WHERE c.connector_account_id=?""",
                (connector_account_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown third-party source: {connector_account_id}")
            running = connection.execute(
                """SELECT COUNT(*) count FROM durable_jobs WHERE connector_account_id=?
                   AND job_type='connector.sync' AND status='running'""",
                (connector_account_id,),
            ).fetchone()["count"]
        schedules = [item for item in self.scheduler.list_schedules()
                     if item["schedule_key"] == self._schedule_key(connector_account_id)]
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["schedule"] = schedules[0] if schedules else None
        result["running_sync_jobs"] = int(running)
        result["audit"] = self.history(connector_account_id)
        return result

    def list(self, brand_id: str, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        query = "SELECT connector_account_id FROM third_party_source_configs WHERE brand_id=?"
        values: list[Any] = [brand_id]
        if not include_disabled:
            query += " AND enabled=1"
        query += " ORDER BY publisher_name,connector_account_id"
        with self._connect() as connection:
            ids = [row["connector_account_id"] for row in connection.execute(query, values)]
        return [self.get(account_id) for account_id in ids]

    def history(self, connector_account_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM third_party_source_audit WHERE connector_account_id=?
                   ORDER BY created_at,rowid""", (connector_account_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    @staticmethod
    def _require_governance(actor: str, reason: str) -> None:
        if not actor.strip():
            raise ValueError("actor is required")
        if not reason.strip():
            raise ValueError("reason is required")

    @staticmethod
    def _schedule_key(connector_account_id: str) -> str:
        return f"connector:{connector_account_id}:content"

    @staticmethod
    def _audit(
        connection: sqlite3.Connection, account_id: str, action: str,
        actor: str, reason: str, details: dict[str, Any], timestamp: str,
    ) -> None:
        connection.execute(
            "INSERT INTO third_party_source_audit VALUES (?,?,?,?,?,?,?)",
            (str(uuid4()), account_id, action, actor.strip(), reason.strip(),
             json.dumps(details, sort_keys=True), timestamp),
        )


__all__ = [
    "CONTENT_POLICY", "DEFAULT_POLLING_SECONDS", "ThirdPartySourceError",
    "ThirdPartySourceService", "validate_public_feed_url",
]
