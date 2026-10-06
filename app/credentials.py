"""Encrypted connector credential persistence.

The public surface of this module deliberately separates safe connection metadata
from secret material.  Callers must explicitly ``reveal()`` a ``SecretPayload``
at the last possible moment (normally while constructing an authorization header).
Neither encrypted nor decrypted credentials are included in model representations.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken


MASTER_KEY_ENV = "BRAND_OS_CREDENTIAL_MASTER_KEY"
_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")
_STATUSES = {"connected", "unhealthy", "reconnect_required", "disconnected"}


class CredentialConfigurationError(RuntimeError):
    """Raised when encryption cannot be configured, without echoing key material."""


class CredentialDecryptionError(RuntimeError):
    """Raised when stored material cannot be authenticated with the active key."""


class CredentialDisconnectedError(RuntimeError):
    """Raised when a disconnected connection is asked for secret material."""


@dataclass(frozen=True, slots=True)
class SecretPayload:
    """Decrypted credentials with a deliberately redacted representation."""

    _values: Mapping[str, Any]

    def __repr__(self) -> str:
        return "SecretPayload(<redacted>)"

    __str__ = __repr__

    def reveal(self) -> dict[str, Any]:
        """Return a defensive copy for immediate use by a connector."""

        return dict(self._values)


@dataclass(frozen=True, slots=True)
class ConnectionMetadata:
    id: str
    provider: str
    account_id: str
    display_name: str
    status: str
    required_scopes: tuple[str, ...]
    granted_scopes: tuple[str, ...]
    missing_scopes: tuple[str, ...]
    excessive_scopes: tuple[str, ...]
    has_credentials: bool
    credential_revision: int
    health_checked_at: str | None
    last_error_code: str | None
    created_at: str
    updated_at: str

    @property
    def scope_status(self) -> str:
        if self.missing_scopes:
            return "insufficient"
        if self.excessive_scopes:
            return "excessive"
        return "least_privilege"

    @property
    def reconnect_required(self) -> bool:
        return self.status in {"reconnect_required", "disconnected"}

    def as_dict(self) -> dict[str, Any]:
        """Return API-safe metadata; secret and ciphertext fields cannot appear."""

        return {
            "id": self.id,
            "provider": self.provider,
            "account_id": self.account_id,
            "display_name": self.display_name,
            "status": self.status,
            "required_scopes": list(self.required_scopes),
            "granted_scopes": list(self.granted_scopes),
            "missing_scopes": list(self.missing_scopes),
            "excessive_scopes": list(self.excessive_scopes),
            "scope_status": self.scope_status,
            "has_credentials": self.has_credentials,
            "credential_revision": self.credential_revision,
            "health_checked_at": self.health_checked_at,
            "last_error_code": self.last_error_code,
            "reconnect_required": self.reconnect_required,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class CredentialAuditEvent:
    sequence: int
    connection_id: str
    provider: str
    account_id: str
    action: str
    actor: str
    at: str
    credential_revision: int


class CredentialStore:
    """SQLite-backed, encrypted-at-rest connector credentials.

    ``master_key`` must be a deployment-provided Fernet key.  There is no fallback
    or generated default: missing and malformed keys fail during construction.
    """

    def __init__(self, database: str | Path, master_key: str | bytes | None) -> None:
        self.database = str(database)
        self._fernet = self._cipher(master_key)
        self._initialize()

    @classmethod
    def from_environment(
        cls, database: str | Path, *, variable: str = MASTER_KEY_ENV
    ) -> "CredentialStore":
        return cls(database, os.environ.get(variable))

    @staticmethod
    def _cipher(key: str | bytes | None) -> Fernet:
        if not key:
            raise CredentialConfigurationError("connector credential master key is not configured")
        try:
            encoded = key.encode("ascii") if isinstance(key, str) else key
            return Fernet(encoded)
        except (TypeError, ValueError) as exc:
            raise CredentialConfigurationError("connector credential master key is invalid") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        if self.database != ":memory:":
            Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            if self.database != ":memory:":
                connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS credential_schema (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS connector_credentials (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    encrypted_payload BLOB,
                    status TEXT NOT NULL,
                    required_scopes_json TEXT NOT NULL DEFAULT '[]',
                    granted_scopes_json TEXT NOT NULL DEFAULT '[]',
                    credential_revision INTEGER NOT NULL DEFAULT 0,
                    health_checked_at TEXT,
                    last_error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(provider, account_id),
                    CHECK(status IN ('connected','unhealthy','reconnect_required','disconnected')),
                    CHECK((encrypted_payload IS NULL) = (credential_revision = 0))
                );
                CREATE TABLE IF NOT EXISTS credential_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    connection_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    at TEXT NOT NULL,
                    credential_revision INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS credential_audit_connection_idx
                    ON credential_audit(connection_id, sequence);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO credential_schema(version, applied_at) VALUES (1, ?)",
                (_now(),),
            )

    def put(
        self,
        provider: str,
        account_id: str,
        display_name: str,
        credentials: Mapping[str, Any],
        *,
        required_scopes: list[str] | tuple[str, ...] = (),
        granted_scopes: list[str] | tuple[str, ...] = (),
        actor: str = "system",
    ) -> ConnectionMetadata:
        provider, account_id = _identity(provider, account_id)
        required = _scopes(required_scopes)
        granted = _scopes(granted_scopes)
        encrypted = self._encrypt(credentials)
        timestamp = _now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM connector_credentials WHERE provider=? AND account_id=?",
                (provider, account_id),
            ).fetchone()
            if current is None:
                connection_id = str(uuid4())
                revision = 1
                created_at = timestamp
                action = "connected"
                connection.execute(
                    """INSERT INTO connector_credentials
                       (id,provider,account_id,display_name,encrypted_payload,status,
                        required_scopes_json,granted_scopes_json,credential_revision,
                        health_checked_at,last_error_code,created_at,updated_at)
                       VALUES (?,?,?,?,?,'connected',?,?,?,?,?,?,?)""",
                    (
                        connection_id, provider, account_id, display_name, encrypted,
                        _json_scopes(required), _json_scopes(granted), revision,
                        timestamp, None, created_at, timestamp,
                    ),
                )
            else:
                connection_id = current["id"]
                revision = int(current["credential_revision"]) + 1
                action = "credentials_updated"
                connection.execute(
                    """UPDATE connector_credentials SET display_name=?, encrypted_payload=?,
                       status='connected', required_scopes_json=?, granted_scopes_json=?,
                       credential_revision=?, health_checked_at=?, last_error_code=NULL,
                       updated_at=? WHERE id=?""",
                    (
                        display_name, encrypted, _json_scopes(required), _json_scopes(granted),
                        revision, timestamp, timestamp, connection_id,
                    ),
                )
            self._audit(connection, connection_id, provider, account_id, action, actor, revision)
            row = connection.execute(
                "SELECT * FROM connector_credentials WHERE id=?", (connection_id,)
            ).fetchone()
            connection.commit()
            return _metadata(row)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, provider: str, account_id: str) -> ConnectionMetadata:
        provider, account_id = _identity(provider, account_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM connector_credentials WHERE provider=? AND account_id=?",
                (provider, account_id),
            ).fetchone()
        if row is None:
            raise KeyError("unknown connector account")
        return _metadata(row)

    def list(self, provider: str | None = None) -> list[ConnectionMetadata]:
        query = "SELECT * FROM connector_credentials"
        parameters: tuple[str, ...] = ()
        if provider is not None:
            normalized, _ = _identity(provider, "placeholder")
            query += " WHERE provider=?"
            parameters = (normalized,)
        query += " ORDER BY provider, display_name, account_id"
        with self._connect() as connection:
            return [_metadata(row) for row in connection.execute(query, parameters).fetchall()]

    def secret(self, provider: str, account_id: str) -> SecretPayload:
        provider, account_id = _identity(provider, account_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT encrypted_payload,status FROM connector_credentials WHERE provider=? AND account_id=?",
                (provider, account_id),
            ).fetchone()
        if row is None:
            raise KeyError("unknown connector account")
        if row["status"] == "disconnected" or row["encrypted_payload"] is None:
            raise CredentialDisconnectedError("connector account has no active credentials")
        try:
            values = json.loads(self._fernet.decrypt(row["encrypted_payload"]).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialDecryptionError("connector credentials cannot be decrypted") from exc
        if not isinstance(values, dict):
            raise CredentialDecryptionError("connector credentials have an invalid payload")
        return SecretPayload(MappingProxyType(values))

    def set_health(
        self,
        provider: str,
        account_id: str,
        status: str,
        *,
        error_code: str | None = None,
        actor: str = "system",
    ) -> ConnectionMetadata:
        if status not in _STATUSES - {"disconnected"}:
            raise ValueError("health status must be connected, unhealthy, or reconnect_required")
        if error_code is not None and not _SAFE_ERROR_CODE.fullmatch(error_code):
            raise ValueError("error_code must be a safe machine-readable code")
        provider, account_id = _identity(provider, account_id)
        timestamp = _now()
        with self._connect() as connection:
            updated = connection.execute(
                """UPDATE connector_credentials SET status=?, health_checked_at=?,
                   last_error_code=?, updated_at=? WHERE provider=? AND account_id=?""",
                (status, timestamp, error_code, timestamp, provider, account_id),
            )
            if not updated.rowcount:
                raise KeyError("unknown connector account")
            row = connection.execute(
                "SELECT * FROM connector_credentials WHERE provider=? AND account_id=?",
                (provider, account_id),
            ).fetchone()
            self._audit(
                connection, row["id"], provider, account_id, f"health_{status}", actor,
                row["credential_revision"],
            )
        return _metadata(row)

    def disconnect(self, provider: str, account_id: str, *, actor: str = "system") -> ConnectionMetadata:
        provider, account_id = _identity(provider, account_id)
        timestamp = _now()
        with self._connect() as connection:
            updated = connection.execute(
                """UPDATE connector_credentials SET encrypted_payload=NULL, status='disconnected',
                   credential_revision=0, last_error_code=NULL, health_checked_at=?, updated_at=?
                   WHERE provider=? AND account_id=?""",
                (timestamp, timestamp, provider, account_id),
            )
            if not updated.rowcount:
                raise KeyError("unknown connector account")
            row = connection.execute(
                "SELECT * FROM connector_credentials WHERE provider=? AND account_id=?",
                (provider, account_id),
            ).fetchone()
            self._audit(connection, row["id"], provider, account_id, "disconnected", actor, 0)
        return _metadata(row)

    def delete(self, provider: str, account_id: str, *, actor: str = "system") -> None:
        provider, account_id = _identity(provider, account_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id,credential_revision FROM connector_credentials WHERE provider=? AND account_id=?",
                (provider, account_id),
            ).fetchone()
            if row is None:
                raise KeyError("unknown connector account")
            self._audit(
                connection, row["id"], provider, account_id, "deleted", actor,
                row["credential_revision"],
            )
            connection.execute("DELETE FROM connector_credentials WHERE id=?", (row["id"],))

    def rotate_master_key(self, new_master_key: str | bytes, *, actor: str = "system") -> int:
        new_fernet = self._cipher(new_master_key)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM connector_credentials WHERE encrypted_payload IS NOT NULL"
            ).fetchall()
            replacements: list[tuple[bytes, str]] = []
            for row in rows:
                try:
                    plaintext = self._fernet.decrypt(row["encrypted_payload"])
                except InvalidToken as exc:
                    raise CredentialDecryptionError("connector credentials cannot be decrypted") from exc
                replacements.append((new_fernet.encrypt(plaintext), row["id"]))
            timestamp = _now()
            for encrypted, connection_id in replacements:
                connection.execute(
                    "UPDATE connector_credentials SET encrypted_payload=?,updated_at=? WHERE id=?",
                    (encrypted, timestamp, connection_id),
                )
            for row in rows:
                self._audit(
                    connection, row["id"], row["provider"], row["account_id"],
                    "master_key_rotated", actor, row["credential_revision"],
                )
            connection.commit()
            self._fernet = new_fernet
            return len(rows)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def audit(self, provider: str, account_id: str) -> list[CredentialAuditEvent]:
        provider, account_id = _identity(provider, account_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM credential_audit WHERE provider=? AND account_id=?
                   ORDER BY sequence""",
                (provider, account_id),
            ).fetchall()
        return [CredentialAuditEvent(**dict(row)) for row in rows]

    def _encrypt(self, credentials: Mapping[str, Any]) -> bytes:
        if not credentials:
            raise ValueError("credentials must not be empty")
        try:
            serialized = json.dumps(dict(credentials), separators=(",", ":"), sort_keys=True).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("credentials must be JSON serializable") from exc
        return self._fernet.encrypt(serialized)

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        connection_id: str,
        provider: str,
        account_id: str,
        action: str,
        actor: str,
        revision: int,
    ) -> None:
        connection.execute(
            """INSERT INTO credential_audit
               (connection_id,provider,account_id,action,actor,at,credential_revision)
               VALUES (?,?,?,?,?,?,?)""",
            (connection_id, provider, account_id, action, actor, _now(), revision),
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _identity(provider: str, account_id: str) -> tuple[str, str]:
    provider = provider.strip().lower()
    account_id = account_id.strip()
    if not provider or not account_id:
        raise ValueError("provider and account_id are required")
    if len(provider) > 50 or len(account_id) > 255:
        raise ValueError("provider or account_id is too long")
    return provider, account_id


def _scopes(scopes: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({scope.strip() for scope in scopes if scope.strip()}))


def _json_scopes(scopes: tuple[str, ...]) -> str:
    return json.dumps(scopes, separators=(",", ":"))


def _metadata(row: sqlite3.Row) -> ConnectionMetadata:
    required = tuple(json.loads(row["required_scopes_json"]))
    granted = tuple(json.loads(row["granted_scopes_json"]))
    return ConnectionMetadata(
        id=row["id"],
        provider=row["provider"],
        account_id=row["account_id"],
        display_name=row["display_name"],
        status=row["status"],
        required_scopes=required,
        granted_scopes=granted,
        missing_scopes=tuple(sorted(set(required) - set(granted))),
        excessive_scopes=tuple(sorted(set(granted) - set(required))),
        has_credentials=row["encrypted_payload"] is not None,
        credential_revision=row["credential_revision"],
        health_checked_at=row["health_checked_at"],
        last_error_code=row["last_error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
