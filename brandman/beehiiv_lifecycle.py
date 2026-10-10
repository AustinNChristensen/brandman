"""Read-side reconciliation of Beehiiv state into canonical newsletter lifecycle.

Beehiiv is an adapter, never an authoring source for an issue.  This projector
therefore changes only lifecycle timestamps for an issue already bound by an
exact export receipt.  It never copies provider title/body content into the
newsletter revision.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping
from uuid import uuid4

from brandman.connectors import ConnectorEvent, ConnectorKind, EventKind


class BeehiivLifecycleError(ValueError):
    """A bound provider event is unsafe or ambiguous to reconcile."""


_EXTERNAL_ID = re.compile(r"post_[A-Za-z0-9-]{1,200}")
_PUBLISHED = frozenset({"confirmed", "published", "sent"})
_KNOWN = frozenset({"draft", "scheduled", "archived", *_PUBLISHED})
_LOCAL_TERMINAL = frozenset({"published", "abandoned", "archived"})


class BeehiivNewsletterLifecycleProjector:
    """Advance only an exact, already-exported BrandMan newsletter issue."""

    def __init__(
        self, database: str | Path, *, connection: sqlite3.Connection | None = None,
    ) -> None:
        self.database = str(database)
        self.connection = connection
        with self._connection() as current:
            current.execute(
                """CREATE TABLE IF NOT EXISTS newsletter_provider_reconciliations (
                  id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, revision INTEGER NOT NULL,
                  connector_account_id TEXT NOT NULL, connector_event_id TEXT,
                  provider_external_id TEXT NOT NULL, provider_status TEXT NOT NULL,
                  scheduled_for TEXT, published_at TEXT, observation_fingerprint TEXT NOT NULL,
                  outcome TEXT NOT NULL, observed_at TEXT, created_at TEXT NOT NULL,
                  UNIQUE(issue_id,revision,observation_fingerprint)
                )"""
            )
            current.execute(
                """CREATE INDEX IF NOT EXISTS newsletter_provider_reconciliation_issue
                   ON newsletter_provider_reconciliations(issue_id,revision,created_at,id)"""
            )

    def binding(
        self, *, brand_id: str, connector_account_id: str, event: ConnectorEvent,
    ) -> dict[str, Any] | None:
        """Return the exact issue/revision binding, or ``None`` for ordinary input."""
        if event.connector is not ConnectorKind.BEEHIIV or event.kind is not EventKind.SOURCE_ITEM:
            return None
        external_id = str(event.external_id or "").strip()
        if not _EXTERNAL_ID.fullmatch(external_id):
            return None
        with self._connection() as connection:
            account = connection.execute(
                """SELECT id FROM connector_accounts
                   WHERE id=? AND brand_id=? AND connector_type='beehiiv'""",
                (connector_account_id, brand_id),
            ).fetchone()
            if account is None:
                raise BeehiivLifecycleError(
                    "Beehiiv lifecycle event is not bound to this brand and connector account"
                )
            tables = {
                row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not {"newsletter_issues", "newsletter_export_receipts"} <= tables:
                return None
            matches = connection.execute(
                """SELECT i.id AS issue_id,i.brand_id,i.lifecycle,i.current_revision,
                          i.approved_revision,i.scheduled_for,i.published_at,
                          r.id AS receipt_id,r.revision,r.payload_fingerprint
                   FROM newsletter_issues i JOIN newsletter_export_receipts r
                     ON r.issue_id=i.id AND r.connector='beehiiv'
                   WHERE i.brand_id=? AND i.beehiiv_external_id=?
                     AND r.external_id=?""",
                (brand_id, external_id, external_id),
            ).fetchall()
        if not matches:
            return None
        if len(matches) != 1:
            raise BeehiivLifecycleError(
                "Beehiiv lifecycle event has an ambiguous canonical issue binding"
            )
        bound = dict(matches[0])
        if (
            int(bound["revision"]) != int(bound["current_revision"])
            or int(bound["revision"]) != int(bound["approved_revision"] or -1)
        ):
            raise BeehiivLifecycleError(
                "Beehiiv lifecycle event is not bound to the current approved issue revision"
            )
        if bound["lifecycle"] not in {"exported", "scheduled", *_LOCAL_TERMINAL}:
            raise BeehiivLifecycleError(
                "Beehiiv lifecycle event conflicts with canonical issue state"
            )
        return bound

    def validate(
        self, *, brand_id: str, connector_account_id: str, event: ConnectorEvent,
    ) -> None:
        bound = self.binding(
            brand_id=brand_id, connector_account_id=connector_account_id, event=event,
        )
        if bound is None:
            return
        status = str(event.payload.get("status") or "").strip().lower()
        if status not in _KNOWN:
            raise BeehiivLifecycleError(
                "bound Beehiiv issue has an unknown provider lifecycle state"
            )
        scheduled_for = event.payload.get("scheduled_for")
        published_at = event.payload.get("published_at")
        if status == "scheduled" and not scheduled_for:
            raise BeehiivLifecycleError(
                "scheduled Beehiiv issue is missing its provider schedule"
            )
        if status in _PUBLISHED and not published_at:
            raise BeehiivLifecycleError(
                "published Beehiiv issue is missing its provider publication time"
            )
        for value, name in ((scheduled_for, "schedule"), (published_at, "publication time")):
            if value is not None:
                _aware_timestamp(str(value), name)

    def project(
        self, *, brand_id: str, connector_account_id: str,
        connector_event: Mapping[str, Any] | None, event: ConnectorEvent,
    ) -> int:
        self.validate(
            brand_id=brand_id, connector_account_id=connector_account_id, event=event,
        )
        bound = self.binding(
            brand_id=brand_id, connector_account_id=connector_account_id, event=event,
        )
        if bound is None:
            return 0
        status = str(event.payload.get("status") or "").strip().lower()
        scheduled_for = _optional_timestamp(event.payload.get("scheduled_for"), "schedule")
        published_at = _optional_timestamp(event.payload.get("published_at"), "publication time")
        observed_at = _optional_timestamp(
            event.payload.get("provider_observed_at") or event.occurred_at,
            "observation time",
        )
        fingerprint = "sha256:" + sha256(json.dumps({
            "external_id": event.external_id, "revision": int(bound["revision"]),
            "status": status, "scheduled_for": scheduled_for,
            "published_at": published_at,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        timestamp = datetime.now().astimezone().isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE") if self.connection is None else None
            duplicate = connection.execute(
                """SELECT outcome FROM newsletter_provider_reconciliations
                   WHERE issue_id=? AND revision=? AND observation_fingerprint=?""",
                (bound["issue_id"], bound["revision"], fingerprint),
            ).fetchone()
            if duplicate is not None:
                return 0
            current = connection.execute(
                "SELECT * FROM newsletter_issues WHERE id=?", (bound["issue_id"],),
            ).fetchone()
            if current is None:
                raise BeehiivLifecycleError("canonical newsletter issue disappeared during reconciliation")
            if (
                current["beehiiv_external_id"] != event.external_id
                or int(current["current_revision"]) != int(bound["revision"])
                or int(current["approved_revision"] or -1) != int(bound["revision"])
            ):
                raise BeehiivLifecycleError("canonical newsletter binding changed during reconciliation")
            local = str(current["lifecycle"])
            outcome = "observed_no_change"
            changed = 0
            if local in _LOCAL_TERMINAL:
                outcome = "terminal_state_preserved"
            elif status in {"draft", "archived"}:
                outcome = "provider_state_did_not_regress_canonical_issue"
            elif status == "scheduled":
                if local == "exported":
                    self._transition(
                        connection, current, "scheduled", scheduled_for=scheduled_for,
                        actor="beehiiv-reconciler", timestamp=timestamp,
                    )
                    changed = 1
                    outcome = "advanced_to_scheduled"
                elif local == "scheduled" and current["scheduled_for"] != scheduled_for:
                    connection.execute(
                        "UPDATE newsletter_issues SET scheduled_for=?,updated_at=? WHERE id=?",
                        (scheduled_for, timestamp, current["id"]),
                    )
                    changed = 1
                    outcome = "schedule_updated"
            elif status in _PUBLISHED:
                if local == "exported":
                    self._transition(
                        connection, current, "scheduled",
                        scheduled_for=scheduled_for,
                        actor="beehiiv-reconciler", timestamp=timestamp,
                    )
                    current = connection.execute(
                        "SELECT * FROM newsletter_issues WHERE id=?", (current["id"],),
                    ).fetchone()
                if str(current["lifecycle"]) == "scheduled":
                    self._transition(
                        connection, current, "published", published_at=published_at,
                        actor="beehiiv-reconciler", timestamp=timestamp,
                    )
                    changed = 1
                    outcome = "advanced_to_published"
            connection.execute(
                """INSERT INTO newsletter_provider_reconciliations
                   (id,issue_id,revision,connector_account_id,connector_event_id,
                    provider_external_id,provider_status,scheduled_for,published_at,
                    observation_fingerprint,outcome,observed_at,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid4()), bound["issue_id"], bound["revision"], connector_account_id,
                 (connector_event or {}).get("id"), event.external_id, status,
                 scheduled_for, published_at, fingerprint, outcome, observed_at, timestamp),
            )
        return changed

    def list(self, issue_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT * FROM newsletter_provider_reconciliations
                   WHERE issue_id=? ORDER BY rowid""", (issue_id,),
            ).fetchall()]

    @staticmethod
    def _transition(
        connection: sqlite3.Connection, current: sqlite3.Row, target: str, *,
        scheduled_for: str | None = None, published_at: str | None = None,
        actor: str, timestamp: str,
    ) -> None:
        previous = str(current["lifecycle"])
        connection.execute(
            """UPDATE newsletter_issues SET lifecycle=?,
               scheduled_for=COALESCE(?,scheduled_for),
               published_at=COALESCE(?,published_at),updated_at=? WHERE id=?""",
            (target, scheduled_for, published_at, timestamp, current["id"]),
        )
        connection.execute(
            """INSERT INTO editorial_lifecycle_events
               (id,entity_type,entity_id,action,from_state,to_state,actor,reason,
                revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (str(uuid4()), "newsletter_issue", current["id"], "provider_reconciled",
             previous, target, actor, "Beehiiv read-side lifecycle observation",
             current["current_revision"], timestamp),
        )

    def _connection(self):
        if self.connection is not None:
            return nullcontext(self.connection)
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection


def _aware_timestamp(value: str, name: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BeehiivLifecycleError(f"Beehiiv {name} must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise BeehiivLifecycleError(f"Beehiiv {name} must include a timezone")
    return parsed.isoformat()


def _optional_timestamp(value: Any, name: str) -> str | None:
    return None if value is None else _aware_timestamp(str(value), name)


__all__ = ["BeehiivLifecycleError", "BeehiivNewsletterLifecycleProjector"]
