"""Governed approval and publish-once dispatch domain.

The module is deliberately infrastructure-agnostic. ``DispatchStore`` and
``Publisher`` are integration seams for a durable database/job queue and real
connectors; ``InMemoryDispatchStore`` is a deterministic reference adapter for
tests and local development.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
import json
from hashlib import sha256
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import Callable, Mapping, Protocol, Sequence
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(UTC)


class Lifecycle(StrEnum):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    QUEUED = "queued"
    PUBLISHED = "published"
    MEASURED = "measured"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs_attention"


class DispatchError(RuntimeError):
    """Base error for a rejected domain operation."""


class InvalidTransition(DispatchError):
    pass


class RevisionMismatch(DispatchError):
    pass


class ApprovalRequired(DispatchError):
    pass


class DispatchBlocked(DispatchError):
    pass


class DuplicateDispatch(DispatchError):
    pass


@dataclass(frozen=True, slots=True)
class PayloadValidation:
    connector: str
    valid: bool
    effective_length: int | None = None
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "connector": self.connector,
            "valid": self.valid,
            "effective_length": self.effective_length,
            "errors": list(self.errors),
        }


class DispatchValidationError(DispatchError):
    """Payload cannot enter approval because it is invalid for its connector."""

    def __init__(self, summary: PayloadValidation) -> None:
        self.summary = summary
        super().__init__("; ".join(summary.errors))


@dataclass(frozen=True, slots=True)
class Approval:
    approver: str
    revision: int
    approved_at: datetime
    batch_id: str | None = None


@dataclass(frozen=True, slots=True)
class DispatchItem:
    id: str
    connector: str
    payload: Mapping[str, object]
    brand_id: str | None = None
    canonical_post_id: str | None = None
    status: Lifecycle = Lifecycle.DRAFT
    revision: int = 1
    approval: Approval | None = None
    idempotency_key: str | None = None
    external_id: str | None = None
    external_url: str | None = None
    attempt_count: int = 0
    dispatch_claim: str | None = None
    last_error: str | None = None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class ConnectorGate:
    healthy: bool = True
    write_enabled: bool = False
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class PublishResult:
    external_id: str
    external_url: str | None = None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    item_id: str
    action: str
    actor: str
    at: datetime
    revision: int
    detail: str | None = None


class DispatchStore(Protocol):
    """Persistence contract. Implementations must make ``mutate`` atomic."""

    def add(self, item: DispatchItem) -> None: ...

    def get(self, item_id: str) -> DispatchItem: ...

    def mutate(
        self, item_id: str, mutation: Callable[[DispatchItem], DispatchItem]
    ) -> DispatchItem: ...

    def mutate_many(
        self,
        item_ids: Sequence[str],
        mutation: Callable[[Mapping[str, DispatchItem]], Mapping[str, DispatchItem]],
    ) -> list[DispatchItem]: ...

    def append_audit(self, event: AuditEvent) -> None: ...

    def list_items(self, *, brand_id: str | None = None, status: Lifecycle | None = None) -> list[DispatchItem]: ...

    def list_audit(self, item_id: str) -> list[AuditEvent]: ...


class Publisher(Protocol):
    def __call__(self, item: DispatchItem, idempotency_key: str) -> PublishResult: ...


_X_SUPPORTED_PAYLOAD_KEYS = {
    "body", "text", "reply_to_post_id", "expected_external_id",
    "connector_account_id", "scheduled_for", "destination", "destination_required",
}
_X_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_X_POST_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_SECRET_MATERIAL_RE = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~-]{8,}|(?:api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|password)\s*[:=]\s*\S+)"
)


def _safe_dispatch_revision_material(payload: Mapping[str, object]) -> dict[str, object]:
    material = dict(payload)
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    if _SECRET_MATERIAL_RE.search(encoded):
        raise ValueError("dispatch material contains credential-shaped content")
    return material


def x_effective_length(text: str) -> int:
    """Return X's effective length for ordinary text and t.co-wrapped URLs."""
    length = 0
    cursor = 0
    for match in _X_URL_RE.finditer(text):
        length += len(text[cursor:match.start()]) + 23
        cursor = match.end()
    return length + len(text[cursor:])


def validate_payload(
    connector: str, payload: Mapping[str, object]
) -> PayloadValidation:
    """Build the connector-specific validation summary shown before approval."""
    if connector != "x":
        return PayloadValidation(connector=connector, valid=True)
    errors: list[str] = []
    unsupported = sorted(set(payload) - _X_SUPPORTED_PAYLOAD_KEYS)
    if unsupported:
        errors.append(f"unsupported X payload keys: {', '.join(unsupported)}")
    supplied_text_keys = [key for key in ("text", "body") if key in payload]
    if len(supplied_text_keys) > 1:
        errors.append("X payload must use either text or body, not both")
    text_value = payload.get(supplied_text_keys[0]) if supplied_text_keys else None
    if not isinstance(text_value, str) or not text_value.strip():
        errors.append("X text must be nonempty")
        effective_length = 0
    else:
        effective_length = x_effective_length(text_value)
        if effective_length > 280:
            errors.append(
                f"X text effective length is {effective_length}; maximum is 280"
            )
    reply_target = payload.get("reply_to_post_id")
    if reply_target is not None and (
        not isinstance(reply_target, str) or not _X_POST_ID_RE.fullmatch(reply_target)
    ):
        errors.append("reply_to_post_id must be a numeric X post ID")
    expected_id = payload.get("expected_external_id")
    if expected_id is not None and (
        not isinstance(expected_id, str) or not _X_POST_ID_RE.fullmatch(expected_id)
    ):
        errors.append("expected_external_id must be a numeric X post ID")
    for metadata_key in ("connector_account_id", "scheduled_for", "destination"):
        value = payload.get(metadata_key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            errors.append(f"{metadata_key} must be nonempty text when supplied")
    destination_required = payload.get("destination_required", False)
    if not isinstance(destination_required, bool):
        errors.append("destination_required must be boolean when supplied")
    elif destination_required:
        destination = payload.get("destination")
        if not isinstance(destination, str) or not destination.strip():
            errors.append("traffic-driving X content requires a governed destination")
        elif not isinstance(text_value, str) or destination not in text_value:
            errors.append("traffic-driving X text must contain its exact governed destination")
    return PayloadValidation(
        connector="x",
        valid=not errors,
        effective_length=effective_length,
        errors=tuple(errors),
    )


class InMemoryDispatchStore:
    """Thread-safe reference store; production adapters should persist atomically."""

    def __init__(self) -> None:
        self._items: dict[str, DispatchItem] = {}
        self.audit: list[AuditEvent] = []
        self._lock = RLock()

    def add(self, item: DispatchItem) -> None:
        with self._lock:
            if item.id in self._items:
                raise KeyError(item.id)
            self._assert_canonical_revision_available(item)
            self._items[item.id] = item

    def get(self, item_id: str) -> DispatchItem:
        with self._lock:
            try:
                return self._items[item_id]
            except KeyError as error:
                raise KeyError(f"unknown dispatch item: {item_id}") from error

    def mutate(
        self, item_id: str, mutation: Callable[[DispatchItem], DispatchItem]
    ) -> DispatchItem:
        with self._lock:
            current = self.get(item_id)
            updated = mutation(current)
            if updated.id != current.id:
                raise ValueError("mutation cannot change item id")
            self._assert_canonical_revision_available(updated, excluding={current.id})
            self._items[item_id] = updated
            return updated

    def append_audit(self, event: AuditEvent) -> None:
        with self._lock:
            self.audit.append(event)

    def mutate_many(
        self,
        item_ids: Sequence[str],
        mutation: Callable[[Mapping[str, DispatchItem]], Mapping[str, DispatchItem]],
    ) -> list[DispatchItem]:
        with self._lock:
            current = {item_id: self.get(item_id) for item_id in item_ids}
            updated = dict(mutation(current))
            if set(updated) != set(current):
                raise ValueError("batch mutation must return every requested item exactly once")
            for item in updated.values():
                self._assert_canonical_revision_available(item, excluding=set(current))
            self._items.update(updated)
            return [updated[item_id] for item_id in item_ids]

    def _assert_canonical_revision_available(
        self, item: DispatchItem, *, excluding: set[str] | None = None
    ) -> None:
        if item.canonical_post_id is None:
            return
        excluded = excluding or set()
        if any(
            existing.id not in excluded
            and existing.connector == item.connector
            and existing.canonical_post_id == item.canonical_post_id
            and existing.revision == item.revision
            for existing in self._items.values()
        ):
            raise KeyError(
                f"duplicate canonical dispatch revision: {item.connector}/"
                f"{item.canonical_post_id}/{item.revision}"
            )

    def list_items(self, *, brand_id: str | None = None, status: Lifecycle | None = None) -> list[DispatchItem]:
        with self._lock:
            return [
                item for item in self._items.values()
                if (brand_id is None or item.brand_id == brand_id)
                and (status is None or item.status is status)
            ]

    def list_audit(self, item_id: str) -> list[AuditEvent]:
        with self._lock:
            return [event for event in self.audit if event.item_id == item_id]


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatch_items (
  id TEXT PRIMARY KEY, brand_id TEXT, connector TEXT NOT NULL, payload TEXT NOT NULL,
  status TEXT NOT NULL, revision INTEGER NOT NULL,
  approval_approver TEXT, approval_revision INTEGER, approval_at TEXT, approval_batch_id TEXT,
  idempotency_key TEXT, external_id TEXT, external_url TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0, dispatch_claim TEXT, last_error TEXT,
  updated_at TEXT NOT NULL, canonical_post_id TEXT
);
CREATE TABLE IF NOT EXISTS dispatch_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL,
  action TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL,
  revision INTEGER NOT NULL, detail TEXT,
  FOREIGN KEY(item_id) REFERENCES dispatch_items(id)
);
CREATE TABLE IF NOT EXISTS dispatch_revisions (
  item_id TEXT NOT NULL,revision INTEGER NOT NULL,connector TEXT NOT NULL,
  material_json TEXT NOT NULL,material_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,PRIMARY KEY(item_id,revision),
  FOREIGN KEY(item_id) REFERENCES dispatch_items(id)
);
CREATE TRIGGER IF NOT EXISTS dispatch_revisions_no_update
BEFORE UPDATE ON dispatch_revisions BEGIN SELECT RAISE(ABORT,'dispatch revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS dispatch_revisions_no_delete
BEFORE DELETE ON dispatch_revisions BEGIN SELECT RAISE(ABORT,'dispatch revisions are immutable'); END;
CREATE INDEX IF NOT EXISTS dispatch_items_brand_status
  ON dispatch_items(brand_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS dispatch_audit_item
  ON dispatch_audit(item_id, id);
"""


class SQLiteDispatchStore:
    """Durable SQLite adapter with atomic compare/mutate transactions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(SQLITE_SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(dispatch_items)").fetchall()
            }
            if "canonical_post_id" not in columns:
                connection.execute(
                    "ALTER TABLE dispatch_items ADD COLUMN canonical_post_id TEXT"
                )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS dispatch_canonical_revision_uq
                   ON dispatch_items(connector, canonical_post_id, revision)
                   WHERE canonical_post_id IS NOT NULL"""
            )
            self._backfill_revisions(connection)

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _from_row(row: sqlite3.Row) -> DispatchItem:
        approval = None
        if row["approval_approver"] is not None:
            approval = Approval(
                approver=row["approval_approver"],
                revision=row["approval_revision"],
                approved_at=datetime.fromisoformat(row["approval_at"]),
                batch_id=row["approval_batch_id"],
            )
        return DispatchItem(
            id=row["id"], brand_id=row["brand_id"], connector=row["connector"],
            canonical_post_id=row["canonical_post_id"],
            payload=json.loads(row["payload"]), status=Lifecycle(row["status"]),
            revision=row["revision"], approval=approval,
            idempotency_key=row["idempotency_key"], external_id=row["external_id"],
            external_url=row["external_url"], attempt_count=row["attempt_count"],
            dispatch_claim=row["dispatch_claim"], last_error=row["last_error"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _values(item: DispatchItem) -> dict[str, object]:
        return {
            "id": item.id, "brand_id": item.brand_id, "connector": item.connector,
            "payload": json.dumps(item.payload, sort_keys=True), "status": item.status.value,
            "revision": item.revision,
            "approval_approver": item.approval.approver if item.approval else None,
            "approval_revision": item.approval.revision if item.approval else None,
            "approval_at": item.approval.approved_at.isoformat() if item.approval else None,
            "approval_batch_id": item.approval.batch_id if item.approval else None,
            "idempotency_key": item.idempotency_key, "external_id": item.external_id,
            "external_url": item.external_url, "attempt_count": item.attempt_count,
            "dispatch_claim": item.dispatch_claim, "last_error": item.last_error,
            "updated_at": item.updated_at.isoformat(),
            "canonical_post_id": item.canonical_post_id,
        }

    @classmethod
    def _write(cls, connection: sqlite3.Connection, item: DispatchItem) -> None:
        connection.execute(
            """INSERT INTO dispatch_items (
            id,brand_id,connector,payload,status,revision,
            approval_approver,approval_revision,approval_at,approval_batch_id,
            idempotency_key,external_id,external_url,attempt_count,dispatch_claim,
            last_error,updated_at,canonical_post_id) VALUES (
            :id,:brand_id,:connector,:payload,:status,:revision,
            :approval_approver,:approval_revision,:approval_at,:approval_batch_id,
            :idempotency_key,:external_id,:external_url,:attempt_count,:dispatch_claim,
            :last_error,:updated_at,:canonical_post_id)
            ON CONFLICT(id) DO UPDATE SET
            brand_id=excluded.brand_id, connector=excluded.connector, payload=excluded.payload,
            status=excluded.status, revision=excluded.revision,
            approval_approver=excluded.approval_approver,
            approval_revision=excluded.approval_revision, approval_at=excluded.approval_at,
            approval_batch_id=excluded.approval_batch_id,
            idempotency_key=excluded.idempotency_key, external_id=excluded.external_id,
            external_url=excluded.external_url, attempt_count=excluded.attempt_count,
            dispatch_claim=excluded.dispatch_claim, last_error=excluded.last_error,
            updated_at=excluded.updated_at,
            canonical_post_id=excluded.canonical_post_id""",
            cls._values(item),
        )

    @staticmethod
    def _append_revision(connection: sqlite3.Connection, item: DispatchItem) -> None:
        if item.connector not in {"x", "x.like", "x.follow"}:
            return
        material = _safe_dispatch_revision_material(item.payload)
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        fingerprint = "sha256:" + sha256(encoded.encode()).hexdigest()
        connection.execute(
            """INSERT OR IGNORE INTO dispatch_revisions
               (item_id,revision,connector,material_json,material_fingerprint,created_at)
               VALUES (?,?,?,?,?,?)""",
            (item.id, item.revision, item.connector, encoded, fingerprint, item.updated_at.isoformat()),
        )

    def _backfill_revisions(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """SELECT * FROM dispatch_items d WHERE connector IN ('x','x.like','x.follow') AND NOT EXISTS
               (SELECT 1 FROM dispatch_revisions r
                WHERE r.item_id=d.id AND r.revision=d.revision)"""
        ).fetchall()
        for row in rows:
            item = self._from_row(row)
            try:
                self._append_revision(connection, item)
            except ValueError:
                if item.approval is not None:
                    connection.execute(
                        """UPDATE dispatch_items SET status='needs_attention',
                           approval_approver=NULL,approval_revision=NULL,approval_at=NULL,
                           approval_batch_id=NULL,idempotency_key=NULL,
                           last_error='unsafe legacy material cannot be approved' WHERE id=?""",
                        (item.id,),
                    )

    def add(self, item: DispatchItem) -> None:
        try:
            with self._connection() as connection:
                values = self._values(item)
                connection.execute(
                    """INSERT INTO dispatch_items (
                    id,brand_id,connector,payload,status,revision,
                    approval_approver,approval_revision,approval_at,approval_batch_id,
                    idempotency_key,external_id,external_url,attempt_count,
                    dispatch_claim,last_error,updated_at,canonical_post_id) VALUES (
                    :id,:brand_id,:connector,:payload,:status,:revision,
                    :approval_approver,:approval_revision,:approval_at,:approval_batch_id,
                    :idempotency_key,:external_id,:external_url,:attempt_count,
                    :dispatch_claim,:last_error,:updated_at,:canonical_post_id)""",
                    values,
                )
                self._append_revision(connection, item)
        except sqlite3.IntegrityError as error:
            raise KeyError(item.id) from error

    def get(self, item_id: str) -> DispatchItem:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM dispatch_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown dispatch item: {item_id}")
        return self._from_row(row)

    def mutate(self, item_id: str, mutation: Callable[[DispatchItem], DispatchItem]) -> DispatchItem:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM dispatch_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown dispatch item: {item_id}")
            current = self._from_row(row)
            updated = mutation(current)
            if updated.id != current.id:
                raise ValueError("mutation cannot change item id")
            self._write(connection, updated)
            if updated.revision != current.revision:
                self._append_revision(connection, updated)
        return updated

    def mutate_many(
        self,
        item_ids: Sequence[str],
        mutation: Callable[[Mapping[str, DispatchItem]], Mapping[str, DispatchItem]],
    ) -> list[DispatchItem]:
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("batch item ids must be unique")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current: dict[str, DispatchItem] = {}
            for item_id in item_ids:
                row = connection.execute("SELECT * FROM dispatch_items WHERE id=?", (item_id,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown dispatch item: {item_id}")
                current[item_id] = self._from_row(row)
            updated = dict(mutation(current))
            if set(updated) != set(current):
                raise ValueError("batch mutation must return every requested item exactly once")
            for item_id in item_ids:
                self._write(connection, updated[item_id])
                if updated[item_id].revision != current[item_id].revision:
                    self._append_revision(connection, updated[item_id])
        return [updated[item_id] for item_id in item_ids]

    def append_audit(self, event: AuditEvent) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO dispatch_audit(item_id,action,actor,at,revision,detail) VALUES (?,?,?,?,?,?)",
                (event.item_id, event.action, event.actor, event.at.isoformat(), event.revision, event.detail),
            )

    def list_items(self, *, brand_id: str | None = None, status: Lifecycle | None = None) -> list[DispatchItem]:
        clauses, parameters = [], []
        if brand_id is not None:
            clauses.append("brand_id=?")
            parameters.append(brand_id)
        if status is not None:
            clauses.append("status=?")
            parameters.append(status.value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM dispatch_items{where} ORDER BY updated_at DESC", parameters
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_audit(self, item_id: str) -> list[AuditEvent]:
        # A missing item is distinct from an existing item with no audit records.
        self.get(item_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT item_id,action,actor,at,revision,detail FROM dispatch_audit WHERE item_id=? ORDER BY id",
                (item_id,),
            ).fetchall()
        return [AuditEvent(row["item_id"], row["action"], row["actor"], datetime.fromisoformat(row["at"]), row["revision"], row["detail"]) for row in rows]

    def get_revision(self, item_id: str, revision: int) -> dict[str, object]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM dispatch_revisions WHERE item_id=? AND revision=?",
                (item_id, revision),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown dispatch revision: {item_id} r{revision}")
        result = dict(row)
        result["material"] = json.loads(result.pop("material_json"))
        return result


class GovernedDispatcher:
    """Application service enforcing approval and external-write invariants."""

    def __init__(
        self,
        store: DispatchStore,
        publishers: Mapping[str, Publisher] | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.publishers = dict(publishers or {})
        self.clock = clock
        self.connector_gates: dict[str, ConnectorGate] = {}
        self.kill_switch_enabled = False

    def create(
        self,
        connector: str,
        payload: Mapping[str, object],
        *,
        item_id: str | None = None,
        brand_id: str | None = None,
        canonical_post_id: str | None = None,
        revision: int = 1,
        actor: str = "system",
    ) -> DispatchItem:
        if revision < 1:
            raise ValueError("revision must be at least 1")
        item = DispatchItem(
            id=item_id or str(uuid4()), connector=connector,
            payload=dict(payload), brand_id=brand_id,
            canonical_post_id=canonical_post_id, revision=revision,
            updated_at=self.clock(),
        )
        self.store.add(item)
        self._audit(item, "created", actor)
        return item

    def submit_for_approval(self, item_id: str, *, actor: str) -> DispatchItem:
        summary = self.validate(item_id)
        if not summary.valid:
            raise DispatchValidationError(summary)
        self._assert_package_governance(self.store.get(item_id))
        return self._transition(item_id, {Lifecycle.DRAFT}, Lifecycle.AWAITING_APPROVAL, actor)

    def validate(self, item_id: str) -> PayloadValidation:
        item = self.store.get(item_id)
        return validate_payload(item.connector, item.payload)

    def edit(self, item_id: str, payload: Mapping[str, object], *, actor: str) -> DispatchItem:
        def mutation(item: DispatchItem) -> DispatchItem:
            if item.status in {Lifecycle.PUBLISHED, Lifecycle.MEASURED, Lifecycle.CANCELLED}:
                raise InvalidTransition(f"cannot edit {item.status}")
            return replace(
                item,
                payload=dict(payload),
                revision=item.revision + 1,
                status=Lifecycle.DRAFT,
                approval=None,
                idempotency_key=None,
                last_error=None,
                updated_at=self.clock(),
            )

        updated = self.store.mutate(item_id, mutation)
        self._audit(updated, "edited; approval invalidated", actor)
        return updated

    def link_canonical_post(self, item_id: str, canonical_post_id: str, *, actor: str) -> DispatchItem:
        """Reconcile one legacy draft with its canonical post without approving it."""
        if not canonical_post_id.strip():
            raise ValueError("canonical_post_id is required")

        def mutation(item: DispatchItem) -> DispatchItem:
            if item.canonical_post_id not in {None, canonical_post_id}:
                raise InvalidTransition("dispatch item is already linked to another canonical post")
            if item.status in {Lifecycle.PUBLISHED, Lifecycle.MEASURED, Lifecycle.CANCELLED}:
                raise InvalidTransition(f"cannot reconcile {item.status}")
            return replace(item, canonical_post_id=canonical_post_id, updated_at=self.clock())

        updated = self.store.mutate(item_id, mutation)
        self._audit(updated, "linked to canonical post", actor, canonical_post_id)
        return updated

    def approve(self, item_id: str, *, revision: int, approver: str, batch_id: str | None = None) -> DispatchItem:
        self._assert_package_governance(self.store.get(item_id), revision=revision)
        def mutation(item: DispatchItem) -> DispatchItem:
            if item.status is not Lifecycle.AWAITING_APPROVAL:
                raise InvalidTransition(f"cannot approve {item.status}")
            if item.revision != revision:
                raise RevisionMismatch(f"expected revision {item.revision}, got {revision}")
            approval = Approval(approver=approver, revision=revision, approved_at=self.clock(), batch_id=batch_id)
            return replace(item, status=Lifecycle.APPROVED, approval=approval, updated_at=self.clock())

        updated = self.store.mutate(item_id, mutation)
        self._audit(updated, "approved", approver, batch_id)
        return updated

    def approve_batch(self, revisions: Mapping[str, int], *, approver: str, batch_id: str | None = None) -> list[DispatchItem]:
        """Approve an explicitly enumerated batch after validating every member.

        Validation is completed before writes. Production stores should wrap the
        subsequent mutations in a transaction if cross-item atomicity is needed.
        """
        if not revisions:
            raise ValueError("approval batch cannot be empty")
        for item_id, revision in revisions.items():
            self._assert_package_governance(self.store.get(item_id), revision=revision)
        batch_id = batch_id or str(uuid4())
        approved_at = self.clock()

        def mutation(items: Mapping[str, DispatchItem]) -> Mapping[str, DispatchItem]:
            updated = {}
            for item_id, revision in revisions.items():
                item = items[item_id]
                if item.status is not Lifecycle.AWAITING_APPROVAL:
                    raise InvalidTransition(f"{item_id} cannot be approved from {item.status}")
                if item.revision != revision:
                    raise RevisionMismatch(f"{item_id}: expected revision {item.revision}, got {revision}")
                approval = Approval(approver, revision, approved_at, batch_id)
                updated[item_id] = replace(item, status=Lifecycle.APPROVED, approval=approval, updated_at=approved_at)
            return updated

        approved = self.store.mutate_many(list(revisions), mutation)
        for item in approved:
            self._audit(item, "approved", approver, batch_id)
        return approved

    def reject(self, item_id: str, *, revision: int, actor: str) -> DispatchItem:
        self._require_revision(item_id, revision)
        return self._transition(item_id, {Lifecycle.AWAITING_APPROVAL}, Lifecycle.REJECTED, actor)

    def cancel(self, item_id: str, *, actor: str) -> DispatchItem:
        return self._transition(
            item_id,
            {Lifecycle.DRAFT, Lifecycle.AWAITING_APPROVAL, Lifecycle.APPROVED, Lifecycle.QUEUED, Lifecycle.FAILED, Lifecycle.NEEDS_ATTENTION, Lifecycle.REJECTED},
            Lifecycle.CANCELLED,
            actor,
        )

    def queue(self, item_id: str, *, actor: str = "system") -> DispatchItem:
        self._assert_package_governance(self.store.get(item_id))
        def mutation(item: DispatchItem) -> DispatchItem:
            self._valid_approval(item)
            key = item.idempotency_key or f"dispatch:{item.id}:revision:{item.revision}"
            return replace(item, status=Lifecycle.QUEUED, idempotency_key=key, updated_at=self.clock())

        updated = self.store.mutate(item_id, mutation)
        self._audit(updated, "queued", actor)
        return updated

    def dispatch(self, item_id: str, *, actor: str = "worker") -> DispatchItem:
        """Publish a queued item once, preserving its stable idempotency key on retry."""
        item = self.store.get(item_id)
        if item.external_id or item.status in {Lifecycle.PUBLISHED, Lifecycle.MEASURED}:
            raise DuplicateDispatch(f"{item_id} was already published")
        if item.status not in {Lifecycle.QUEUED, Lifecycle.FAILED, Lifecycle.NEEDS_ATTENTION}:
            raise InvalidTransition(f"cannot dispatch {item.status}")
        self._valid_approval(item)
        self._assert_package_governance(item)
        self._assert_write_allowed(item.connector)
        publisher = self.publishers.get(item.connector)
        if publisher is None:
            return self._record_failure(item_id, "connector publisher is not configured", actor, needs_attention=True)
        key = item.idempotency_key
        if not key:
            raise DispatchError("queued item is missing an idempotency key")

        # Atomically claim and record the attempt before the external boundary.
        # The connector's stable key closes the remaining ambiguous-timeout gap.
        claim = str(uuid4())

        def claim_attempt(current: DispatchItem) -> DispatchItem:
            if current.external_id or current.status in {Lifecycle.PUBLISHED, Lifecycle.MEASURED}:
                raise DuplicateDispatch(f"{item_id} was already published")
            if current.dispatch_claim:
                raise DuplicateDispatch(f"{item_id} is already being dispatched")
            self._valid_approval(current)
            self._assert_package_governance(current)
            return replace(
                current,
                attempt_count=current.attempt_count + 1,
                dispatch_claim=claim,
                last_error=None,
                updated_at=self.clock(),
            )

        attempted = self.store.mutate(item_id, claim_attempt)
        try:
            result = publisher(attempted, key)
            if not result.external_id:
                raise ValueError("publisher returned no external id")
        except Exception as error:
            return self._record_failure(item_id, str(error), actor)

        def published(current: DispatchItem) -> DispatchItem:
            if current.external_id:
                raise DuplicateDispatch(f"{item_id} was already published")
            if current.dispatch_claim != claim:
                raise DuplicateDispatch(f"{item_id} dispatch claim changed")
            return replace(current, status=Lifecycle.PUBLISHED, external_id=result.external_id, external_url=result.external_url, dispatch_claim=None, last_error=None, updated_at=self.clock())

        updated = self.store.mutate(item_id, published)
        self._audit(updated, "published", actor, result.external_id)
        return updated

    def mark_measured(self, item_id: str, *, actor: str = "metrics-worker") -> DispatchItem:
        return self._transition(item_id, {Lifecycle.PUBLISHED}, Lifecycle.MEASURED, actor)

    def record_external_receipt(
        self, item_id: str, *, revision: int, external_id: str,
        external_url: str | None, actor: str,
    ) -> DispatchItem:
        """Reconcile a browser/MCP-posted X action without invoking a publisher."""
        if not external_id.strip() or not actor.strip():
            raise ValueError("external_id and actor are required")
        self._assert_package_governance(self.store.get(item_id), revision=revision)

        def mutation(item: DispatchItem) -> DispatchItem:
            if item.external_id:
                if item.external_id == external_id and item.revision == revision:
                    return item
                raise DuplicateDispatch(f"{item_id} already has a different external receipt")
            if item.status not in {Lifecycle.APPROVED, Lifecycle.QUEUED}:
                raise InvalidTransition("external receipt requires an approved X action")
            if item.connector != "x":
                raise InvalidTransition("external X receipt requires an X dispatch item")
            if item.revision != revision:
                raise RevisionMismatch(f"expected revision {item.revision}, got {revision}")
            self._valid_approval(item)
            return replace(
                item, status=Lifecycle.PUBLISHED, external_id=external_id,
                external_url=external_url, idempotency_key=item.idempotency_key or
                f"dispatch:{item.id}:revision:{item.revision}", dispatch_claim=None,
                last_error=None, updated_at=self.clock(),
            )

        updated = self.store.mutate(item_id, mutation)
        self._audit(updated, "external receipt recorded", actor, external_id)
        return updated

    def set_connector_gate(self, connector: str, *, healthy: bool, write_enabled: bool, detail: str | None = None) -> None:
        self.connector_gates[connector] = ConnectorGate(healthy, write_enabled, detail)

    def set_kill_switch(self, enabled: bool) -> None:
        self.kill_switch_enabled = enabled

    def _assert_write_allowed(self, connector: str) -> None:
        if self.kill_switch_enabled:
            raise DispatchBlocked("global dispatch kill switch is enabled")
        gate = self.connector_gates.get(connector)
        if gate is None:
            raise DispatchBlocked(f"connector {connector!r} has no configured write gate")
        if not gate.healthy:
            raise DispatchBlocked(gate.detail or f"connector {connector!r} is unhealthy")
        if not gate.write_enabled:
            raise DispatchBlocked(f"connector {connector!r} is not write-enabled")

    def _assert_package_governance(
        self, item: DispatchItem, *, revision: int | None = None,
    ) -> None:
        """Fail closed for durable package derivatives at each action boundary."""
        if not isinstance(self.store, SQLiteDispatchStore):
            return
        from app.distribution_governance import assert_package_dispatch_governance
        with self.store._connection() as connection:
            assert_package_dispatch_governance(
                connection, item.id, revision=item.revision if revision is None else revision,
            )

    def _valid_approval(self, item: DispatchItem) -> None:
        if item.status not in {Lifecycle.APPROVED, Lifecycle.QUEUED, Lifecycle.FAILED, Lifecycle.NEEDS_ATTENTION}:
            raise ApprovalRequired(f"{item.id} is not approved")
        if item.approval is None or item.approval.revision != item.revision:
            raise ApprovalRequired(f"{item.id} lacks approval for revision {item.revision}")

    def _require_revision(self, item_id: str, revision: int) -> None:
        item = self.store.get(item_id)
        if item.revision != revision:
            raise RevisionMismatch(f"expected revision {item.revision}, got {revision}")

    def _transition(self, item_id: str, allowed: set[Lifecycle], target: Lifecycle, actor: str) -> DispatchItem:
        def mutation(item: DispatchItem) -> DispatchItem:
            if item.status not in allowed:
                raise InvalidTransition(f"cannot transition {item.status} to {target}")
            return replace(item, status=target, updated_at=self.clock())

        updated = self.store.mutate(item_id, mutation)
        self._audit(updated, target.value, actor)
        return updated

    def _record_failure(self, item_id: str, message: str, actor: str, *, needs_attention: bool = False) -> DispatchItem:
        status = Lifecycle.NEEDS_ATTENTION if needs_attention else Lifecycle.FAILED
        updated = self.store.mutate(item_id, lambda item: replace(item, status=status, dispatch_claim=None, last_error=message, updated_at=self.clock()))
        self._audit(updated, status.value, actor, message)
        return updated

    def _audit(self, item: DispatchItem, action: str, actor: str, detail: str | None = None) -> None:
        self.store.append_audit(AuditEvent(item.id, action, actor, self.clock(), item.revision, detail))


def dispatch_item_to_dict(item: DispatchItem) -> dict[str, object]:
    return {
        "id": item.id,
        "brand_id": item.brand_id,
        "canonical_post_id": item.canonical_post_id,
        "connector": item.connector,
        "payload": dict(item.payload),
        "status": item.status.value,
        "revision": item.revision,
        "approval": None if item.approval is None else {
            "approver": item.approval.approver,
            "revision": item.approval.revision,
            "approved_at": item.approval.approved_at.isoformat(),
            "batch_id": item.approval.batch_id,
        },
        "idempotency_key": item.idempotency_key,
        "external_id": item.external_id,
        "external_url": item.external_url,
        "attempt_count": item.attempt_count,
        "last_error": item.last_error,
        "updated_at": item.updated_at.isoformat(),
    }


def audit_event_to_dict(event: AuditEvent) -> dict[str, object]:
    return {
        "item_id": event.item_id,
        "action": event.action,
        "actor": event.actor,
        "at": event.at.isoformat(),
        "revision": event.revision,
        "detail": event.detail,
    }


__all__ = [
    "Approval",
    "ApprovalRequired",
    "AuditEvent",
    "ConnectorGate",
    "DispatchBlocked",
    "DispatchError",
    "DispatchItem",
    "DispatchStore",
    "DispatchValidationError",
    "DuplicateDispatch",
    "GovernedDispatcher",
    "InMemoryDispatchStore",
    "InvalidTransition",
    "Lifecycle",
    "PayloadValidation",
    "PublishResult",
    "Publisher",
    "RevisionMismatch",
    "SQLiteDispatchStore",
    "audit_event_to_dict",
    "dispatch_item_to_dict",
    "validate_payload",
    "x_effective_length",
]
