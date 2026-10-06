"""SQLite persistence for governed dispatch.

The adapter keeps the small :mod:`app.dispatch` domain free of infrastructure
concerns while providing transaction-safe mutations for multiple workers and
process restarts.  It intentionally opens a connection per operation: SQLite's
``BEGIN IMMEDIATE`` then acts as the cross-process mutation lock.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.dispatch import (
    Approval,
    AuditEvent,
    ConnectorGate,
    DispatchBlocked,
    DispatchItem,
    GovernedDispatcher,
    Lifecycle,
)


class SQLiteDispatchStore:
    """Durable implementation of ``DispatchStore`` backed by a SQLite file."""

    def __init__(self, database: str | Path, *, timeout: float = 30.0) -> None:
        self.database = str(database)
        if self.database == ":memory:":
            raise ValueError("use a file-backed database for durable dispatch")
        self.timeout = timeout
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=self.timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        path = Path(self.database)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS dispatch_schema (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dispatch_items (
                    id TEXT PRIMARY KEY,
                    connector TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision > 0),
                    approval_approver TEXT,
                    approval_revision INTEGER,
                    approval_at TEXT,
                    approval_batch_id TEXT,
                    idempotency_key TEXT,
                    external_id TEXT,
                    external_url TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    dispatch_claim TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    CHECK (
                        (approval_approver IS NULL AND approval_revision IS NULL AND approval_at IS NULL)
                        OR
                        (approval_approver IS NOT NULL AND approval_revision IS NOT NULL AND approval_at IS NOT NULL)
                    )
                );
                CREATE UNIQUE INDEX IF NOT EXISTS dispatch_idempotency_key_uq
                    ON dispatch_items(idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS dispatch_external_id_uq
                    ON dispatch_items(connector, external_id)
                    WHERE external_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS dispatch_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    at TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    detail TEXT,
                    FOREIGN KEY(item_id) REFERENCES dispatch_items(id)
                );
                CREATE INDEX IF NOT EXISTS dispatch_audit_item_idx
                    ON dispatch_audit(item_id, sequence);
                CREATE TABLE IF NOT EXISTS dispatch_connector_gates (
                    connector TEXT PRIMARY KEY,
                    healthy INTEGER NOT NULL CHECK (healthy IN (0, 1)),
                    write_enabled INTEGER NOT NULL CHECK (write_enabled IN (0, 1)),
                    detail TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dispatch_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO dispatch_schema(version, applied_at) VALUES (1, ?)",
                (datetime.now().astimezone().isoformat(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO dispatch_settings(key, value, updated_at) "
                "VALUES ('kill_switch', '0', ?)",
                (datetime.now().astimezone().isoformat(),),
            )

    def add(self, item: DispatchItem) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO dispatch_items (
                        id, connector, payload_json, status, revision,
                        approval_approver, approval_revision, approval_at,
                        approval_batch_id, idempotency_key, external_id,
                        external_url, attempt_count, dispatch_claim, last_error,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    self._values(item),
                )
        except sqlite3.IntegrityError as error:
            if "dispatch_items.id" in str(error):
                raise KeyError(item.id) from error
            raise

    def get(self, item_id: str) -> DispatchItem:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dispatch_items WHERE id = ?", (item_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown dispatch item: {item_id}")
        return self._item(row)

    def mutate(
        self,
        item_id: str,
        mutation: Callable[[DispatchItem], DispatchItem],
    ) -> DispatchItem:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM dispatch_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown dispatch item: {item_id}")
            current = self._item(row)
            updated = mutation(current)
            if updated.id != current.id:
                raise ValueError("mutation cannot change item id")
            connection.execute(
                """UPDATE dispatch_items SET
                    connector = ?, payload_json = ?, status = ?, revision = ?,
                    approval_approver = ?, approval_revision = ?, approval_at = ?,
                    approval_batch_id = ?, idempotency_key = ?, external_id = ?,
                    external_url = ?, attempt_count = ?, dispatch_claim = ?,
                    last_error = ?, updated_at = ? WHERE id = ?""",
                self._values(updated)[1:] + (item_id,),
            )
            connection.commit()
            return updated
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def append_audit(self, event: AuditEvent) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO dispatch_audit
                    (item_id, action, actor, at, revision, detail)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    event.item_id,
                    event.action,
                    event.actor,
                    event.at.isoformat(),
                    event.revision,
                    event.detail,
                ),
            )

    def list_audit(self, item_id: str | None = None) -> list[AuditEvent]:
        query = "SELECT * FROM dispatch_audit"
        parameters: tuple[str, ...] = ()
        if item_id is not None:
            query += " WHERE item_id = ?"
            parameters = (item_id,)
        query += " ORDER BY sequence"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            AuditEvent(
                item_id=row["item_id"],
                action=row["action"],
                actor=row["actor"],
                at=datetime.fromisoformat(row["at"]),
                revision=row["revision"],
                detail=row["detail"],
            )
            for row in rows
        ]

    @property
    def audit(self) -> list[AuditEvent]:
        """Compatibility view matching ``InMemoryDispatchStore.audit``."""
        return self.list_audit()

    def set_connector_gate(self, connector: str, gate: ConnectorGate) -> None:
        now = datetime.now().astimezone().isoformat()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO dispatch_connector_gates
                    (connector, healthy, write_enabled, detail, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(connector) DO UPDATE SET
                    healthy = excluded.healthy,
                    write_enabled = excluded.write_enabled,
                    detail = excluded.detail,
                    updated_at = excluded.updated_at""",
                (connector, gate.healthy, gate.write_enabled, gate.detail, now),
            )

    def get_connector_gate(self, connector: str) -> ConnectorGate | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dispatch_connector_gates WHERE connector = ?",
                (connector,),
            ).fetchone()
        if row is None:
            return None
        return ConnectorGate(bool(row["healthy"]), bool(row["write_enabled"]), row["detail"])

    def list_connector_gates(self) -> dict[str, ConnectorGate]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM dispatch_connector_gates ORDER BY connector"
            ).fetchall()
        return {
            row["connector"]: ConnectorGate(
                bool(row["healthy"]), bool(row["write_enabled"]), row["detail"]
            )
            for row in rows
        }

    def set_kill_switch(self, enabled: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO dispatch_settings(key, value, updated_at)
                    VALUES ('kill_switch', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at""",
                ("1" if enabled else "0", datetime.now().astimezone().isoformat()),
            )

    def get_kill_switch(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM dispatch_settings WHERE key = 'kill_switch'"
            ).fetchone()
        return row is not None and row["value"] == "1"

    def release_dispatch_claim(
        self, item_id: str, expected_claim: str, *, error: str
    ) -> DispatchItem:
        """Release a known orphaned claim while retaining its idempotency key.

        Operators should only call this after reconciling the external connector.
        A retry will reuse the same stable key, so a connector can safely return
        the original result after an ambiguous timeout or worker crash.
        """

        def release(item: DispatchItem) -> DispatchItem:
            if item.dispatch_claim != expected_claim:
                raise ValueError("dispatch claim does not match")
            if item.external_id is not None:
                raise ValueError("published dispatch claims cannot be released")
            return replace(
                item,
                status=Lifecycle.NEEDS_ATTENTION,
                dispatch_claim=None,
                last_error=error,
                updated_at=datetime.now().astimezone(),
            )

        return self.mutate(item_id, release)

    @staticmethod
    def _values(item: DispatchItem) -> tuple[object, ...]:
        approval = item.approval
        return (
            item.id,
            item.connector,
            json.dumps(dict(item.payload), sort_keys=True, separators=(",", ":")),
            item.status.value,
            item.revision,
            approval.approver if approval else None,
            approval.revision if approval else None,
            approval.approved_at.isoformat() if approval else None,
            approval.batch_id if approval else None,
            item.idempotency_key,
            item.external_id,
            item.external_url,
            item.attempt_count,
            item.dispatch_claim,
            item.last_error,
            item.updated_at.isoformat(),
        )

    @staticmethod
    def _item(row: sqlite3.Row) -> DispatchItem:
        approval = None
        if row["approval_approver"] is not None:
            approval = Approval(
                approver=row["approval_approver"],
                revision=row["approval_revision"],
                approved_at=datetime.fromisoformat(row["approval_at"]),
                batch_id=row["approval_batch_id"],
            )
        return DispatchItem(
            id=row["id"],
            connector=row["connector"],
            payload=json.loads(row["payload_json"]),
            status=Lifecycle(row["status"]),
            revision=row["revision"],
            approval=approval,
            idempotency_key=row["idempotency_key"],
            external_id=row["external_id"],
            external_url=row["external_url"],
            attempt_count=row["attempt_count"],
            dispatch_claim=row["dispatch_claim"],
            last_error=row["last_error"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class SQLiteGovernedDispatcher(GovernedDispatcher):
    """Governed dispatcher whose gates and kill switch survive restarts."""

    store: SQLiteDispatchStore

    def set_connector_gate(
        self,
        connector: str,
        *,
        healthy: bool,
        write_enabled: bool,
        detail: str | None = None,
    ) -> None:
        self.store.set_connector_gate(
            connector, ConnectorGate(healthy, write_enabled, detail)
        )

    def set_kill_switch(self, enabled: bool) -> None:
        self.store.set_kill_switch(enabled)

    def _assert_write_allowed(self, connector: str) -> None:
        if self.store.get_kill_switch():
            raise DispatchBlocked("global dispatch kill switch is enabled")
        gate = self.store.get_connector_gate(connector)
        if gate is None:
            raise DispatchBlocked(f"connector {connector!r} has no configured write gate")
        if not gate.healthy:
            raise DispatchBlocked(gate.detail or f"connector {connector!r} is unhealthy")
        if not gate.write_enabled:
            raise DispatchBlocked(f"connector {connector!r} is not write-enabled")


__all__ = ["SQLiteDispatchStore", "SQLiteGovernedDispatcher"]
