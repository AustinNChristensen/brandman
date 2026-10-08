"""Governed product-feedback lifecycle and reconciliation.

Reporting a recurrence never changes lifecycle state. In particular, a newly
observed failure cannot silently reopen a resolved or verified gap; reopening is
an explicit, audited action. Reconciliation records likely implementation matches
without claiming that the underlying gap has been fixed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4


_SEVERITIES = {"low", "medium", "high", "critical"}
_STATUSES = {"open", "in_progress", "resolved", "verified"}
_TRANSITIONS = {
    "open": {"in_progress"},
    "in_progress": {"resolved", "open"},
    "resolved": {"verified", "open"},
    "verified": {"open"},
}


class FeedbackError(ValueError):
    pass


class FeedbackNotFound(KeyError):
    pass


class FeedbackStore:
    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        self._initialize()

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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS product_feedback (
                  id TEXT PRIMARY KEY, brand_id TEXT, reporter TEXT NOT NULL,
                  summary TEXT NOT NULL, details TEXT NOT NULL, status TEXT NOT NULL,
                  created_at TEXT NOT NULL, component TEXT NOT NULL DEFAULT 'unknown',
                  severity TEXT NOT NULL DEFAULT 'medium', fingerprint TEXT,
                  reproduction TEXT NOT NULL DEFAULT '', expected_behavior TEXT NOT NULL DEFAULT '',
                  actual_behavior TEXT NOT NULL DEFAULT '', workaround TEXT NOT NULL DEFAULT '',
                  related_ids TEXT NOT NULL DEFAULT '[]', first_seen_at TEXT,
                  last_seen_at TEXT, occurrence_count INTEGER NOT NULL DEFAULT 1,
                  updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS feedback_history (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  feedback_id TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
                  at TEXT NOT NULL, from_status TEXT, to_status TEXT, details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS feedback_history_item_idx
                  ON feedback_history(feedback_id, sequence);
                CREATE TABLE IF NOT EXISTS feedback_comments (
                  id TEXT PRIMARY KEY, feedback_id TEXT NOT NULL, actor TEXT NOT NULL,
                  body TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS feedback_comments_item_idx
                  ON feedback_comments(feedback_id, created_at);
                CREATE TABLE IF NOT EXISTS feedback_reconciliation_matches (
                  id TEXT PRIMARY KEY, feedback_id TEXT NOT NULL, component TEXT NOT NULL,
                  reason TEXT NOT NULL, implementation_links_json TEXT NOT NULL DEFAULT '[]',
                  matched_at TEXT NOT NULL, UNIQUE(feedback_id, component)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS product_feedback_brand_fingerprint
                  ON product_feedback(COALESCE(brand_id, ''), fingerprint) WHERE fingerprint IS NOT NULL;
                """
            )
            existing = {row["name"] for row in connection.execute("PRAGMA table_info(product_feedback)")}
            migrations = {
                "assignee": "TEXT",
                "implementation_links": "TEXT NOT NULL DEFAULT '[]'",
                "implementation_notes": "TEXT NOT NULL DEFAULT ''",
                "resolution_evidence": "TEXT NOT NULL DEFAULT ''",
                "resolved_by": "TEXT",
                "resolved_at": "TEXT",
                "verified_by": "TEXT",
                "verified_at": "TEXT",
            }
            for column, definition in migrations.items():
                if column not in existing:
                    connection.execute(f"ALTER TABLE product_feedback ADD COLUMN {column} {definition}")

    def report(
        self,
        *,
        reporter: str,
        summary: str,
        details: str,
        component: str,
        severity: str = "medium",
        brand_id: str | None = None,
        fingerprint: str | None = None,
        reproduction: str = "",
        expected_behavior: str = "",
        actual_behavior: str = "",
        workaround: str = "",
        related_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if severity not in _SEVERITIES:
            raise FeedbackError("invalid feedback severity")
        if not reporter.strip() or not summary.strip() or not component.strip():
            raise FeedbackError("reporter, summary, and component are required")
        fingerprint = fingerprint or hashlib.sha256(
            f"{brand_id or ''}|{component}|{summary}".casefold().encode()
        ).hexdigest()
        timestamp = _now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM product_feedback WHERE COALESCE(brand_id,'')=COALESCE(?, '') AND fingerprint=?",
                (brand_id, fingerprint),
            ).fetchone()
            if existing:
                connection.execute(
                    """UPDATE product_feedback SET reporter=?, details=?, severity=?, reproduction=?,
                       expected_behavior=?, actual_behavior=?, workaround=?, related_ids=?,
                       last_seen_at=?, occurrence_count=occurrence_count+1, updated_at=? WHERE id=?""",
                    (reporter, details, severity, reproduction, expected_behavior, actual_behavior,
                     workaround, json.dumps(related_ids or []), timestamp, timestamp, existing["id"]),
                )
                feedback_id = existing["id"]
                self._history(connection, feedback_id, "recurrence_reported", reporter, existing["status"], existing["status"], {
                    "occurrence_count": existing["occurrence_count"] + 1,
                })
            else:
                feedback_id = str(uuid4())
                connection.execute(
                    """INSERT INTO product_feedback
                       (id,brand_id,reporter,summary,details,status,created_at,component,severity,
                        fingerprint,reproduction,expected_behavior,actual_behavior,workaround,
                        related_ids,first_seen_at,last_seen_at,occurrence_count,updated_at,
                        assignee,implementation_links,implementation_notes,resolution_evidence,
                        resolved_by,resolved_at,verified_by,verified_at)
                       VALUES (?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,'[]','','',NULL,NULL,NULL,NULL)""",
                    (feedback_id, brand_id, reporter, summary, details, timestamp, component,
                     severity, fingerprint, reproduction, expected_behavior, actual_behavior,
                     workaround, json.dumps(related_ids or []), timestamp, timestamp, 1, timestamp),
                )
                self._history(connection, feedback_id, "reported", reporter, None, "open", {})
            row = connection.execute("SELECT * FROM product_feedback WHERE id=?", (feedback_id,)).fetchone()
            connection.commit()
            return self._decode(row)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def comment(self, feedback_id: str, body: str, *, actor: str) -> dict[str, Any]:
        if not actor.strip() or not body.strip():
            raise FeedbackError("comment actor and body are required")
        with self._connect() as connection:
            item = self._require(connection, feedback_id)
            comment = {"id": str(uuid4()), "feedback_id": feedback_id, "actor": actor,
                       "body": body, "created_at": _now()}
            connection.execute(
                "INSERT INTO feedback_comments(id,feedback_id,actor,body,created_at) VALUES (:id,:feedback_id,:actor,:body,:created_at)",
                comment,
            )
            self._history(connection, feedback_id, "commented", actor, item["status"], item["status"], {})
        return comment

    def start(
        self, feedback_id: str, *, assignee: str, actor: str,
        implementation_links: Iterable[str] = (), implementation_notes: str = "",
    ) -> dict[str, Any]:
        if not assignee.strip():
            raise FeedbackError("assignee is required")
        return self._transition(
            feedback_id, "in_progress", actor=actor, assignee=assignee,
            implementation_links=list(implementation_links), implementation_notes=implementation_notes,
        )

    def resolve(
        self, feedback_id: str, *, actor: str, resolution_evidence: str,
        implementation_links: Iterable[str] = (), implementation_notes: str = "",
    ) -> dict[str, Any]:
        self._require_chris(actor)
        if not resolution_evidence.strip():
            raise FeedbackError("resolution evidence is required")
        return self._transition(
            feedback_id, "resolved", actor=actor, resolution_evidence=resolution_evidence,
            implementation_links=list(implementation_links), implementation_notes=implementation_notes,
        )

    def verify(self, feedback_id: str, *, actor: str, evidence: str) -> dict[str, Any]:
        self._require_chris(actor)
        if not evidence.strip():
            raise FeedbackError("verification evidence is required")
        return self._transition(feedback_id, "verified", actor=actor, verification_evidence=evidence)

    def reopen(self, feedback_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise FeedbackError("reopen reason is required")
        return self._transition(feedback_id, "open", actor=actor, reopen_reason=reason)

    def _transition(self, feedback_id: str, target: str, *, actor: str, **changes: Any) -> dict[str, Any]:
        if not actor.strip() or target not in _STATUSES:
            raise FeedbackError("invalid lifecycle transition")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require(connection, feedback_id)
            if target not in _TRANSITIONS[current["status"]]:
                raise FeedbackError(f"cannot transition feedback from {current['status']} to {target}")
            timestamp = _now()
            values: dict[str, Any] = {"status": target, "updated_at": timestamp}
            details: dict[str, Any] = {}
            if "assignee" in changes:
                values["assignee"] = changes["assignee"]
            if changes.get("implementation_links"):
                values["implementation_links"] = json.dumps(list(changes["implementation_links"]))
                details["implementation_links"] = list(changes["implementation_links"])
            if changes.get("implementation_notes"):
                values["implementation_notes"] = changes["implementation_notes"]
            if target == "resolved":
                values.update(resolution_evidence=changes["resolution_evidence"], resolved_by=actor, resolved_at=timestamp,
                              verified_by=None, verified_at=None)
            elif target == "verified":
                values.update(verified_by=actor, verified_at=timestamp)
                details["verification_evidence"] = changes["verification_evidence"]
            elif target == "open":
                values.update(resolved_by=None, resolved_at=None, verified_by=None, verified_at=None)
                details["reopen_reason"] = changes["reopen_reason"]
            assignments = ",".join(f"{key}=?" for key in values)
            connection.execute(
                f"UPDATE product_feedback SET {assignments} WHERE id=?",
                (*values.values(), feedback_id),
            )
            self._history(connection, feedback_id, target, actor, current["status"], target, details)
            row = connection.execute("SELECT * FROM product_feedback WHERE id=?", (feedback_id,)).fetchone()
            connection.commit()
            return self._decode(row)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reconcile_shipped_component(
        self, component: str, *, keywords: Iterable[str], implementation_links: Iterable[str] = (),
        actor: str = "system:reconciliation",
    ) -> list[dict[str, Any]]:
        """Attach shipped-code evidence to generic or already-classified gaps.

        Reconciliation deliberately does not change lifecycle status. An exact
        component match makes the operation useful after gaps have been triaged,
        while keyword matching prevents a broad component-level update from
        attaching evidence to unrelated feedback.
        """
        terms = sorted({term.strip().casefold() for term in keywords if term.strip()})
        if not component.strip() or not terms:
            raise FeedbackError("component and at least one keyword are required")
        matches: list[dict[str, Any]] = []
        with self._connect() as connection:
            candidates = connection.execute(
                """SELECT * FROM product_feedback
                   WHERE component IN ('unknown','agent-workflow',?)""",
                (component,),
            ).fetchall()
            for candidate in candidates:
                haystack = " ".join((candidate["summary"], candidate["details"], candidate["reproduction"])).casefold()
                matched = [term for term in terms if term in haystack]
                if not matched:
                    continue
                reason = f"matched keywords: {', '.join(matched)}"
                prior = connection.execute(
                    "SELECT 1 FROM feedback_reconciliation_matches WHERE feedback_id=? AND component=?",
                    (candidate["id"], component),
                ).fetchone()
                connection.execute(
                    """INSERT INTO feedback_reconciliation_matches
                       (id,feedback_id,component,reason,implementation_links_json,matched_at)
                       VALUES (?,?,?,?,?,?) ON CONFLICT(feedback_id,component) DO UPDATE SET
                       reason=excluded.reason,implementation_links_json=excluded.implementation_links_json,
                       matched_at=excluded.matched_at""",
                    (str(uuid4()), candidate["id"], component, reason,
                     json.dumps(list(implementation_links)), _now()),
                )
                if prior is None:
                    self._history(connection, candidate["id"], "implementation_match_found", actor,
                                  candidate["status"], candidate["status"], {"component": component})
                matches.append({"feedback_id": candidate["id"], "component": component,
                                "status": candidate["status"], "reason": reason})
        return matches

    def get(self, feedback_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._require(connection, feedback_id)
            item = self._decode(row)
            item["comments"] = [dict(comment) for comment in connection.execute(
                "SELECT * FROM feedback_comments WHERE feedback_id=? ORDER BY created_at,id", (feedback_id,)
            ).fetchall()]
            item["implementation_matches"] = [self._decode_match(match) for match in connection.execute(
                "SELECT * FROM feedback_reconciliation_matches WHERE feedback_id=? ORDER BY matched_at", (feedback_id,)
            ).fetchall()]
            item["history"] = self._history_rows(connection, feedback_id)
            return item

    def list(
        self, *, brand_id: str | None = None, status: str | None = None,
        component: str | None = None, assignee: str | None = None, severity: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[str] = []
        for column, value in (("brand_id", brand_id), ("status", status), ("component", component),
                              ("assignee", assignee), ("severity", severity)):
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        with self._connect() as connection:
            registry = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fixture_quarantine_registry'"
            ).fetchone()
            if registry:
                clauses.append("NOT EXISTS (SELECT 1 FROM fixture_quarantine_registry q "
                    "WHERE q.table_name='product_feedback' "
                    "AND q.record_key_json=json_array(product_feedback.id))")
            query = "SELECT * FROM product_feedback"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY last_seen_at DESC, created_at DESC"
            return [self._decode(row) for row in connection.execute(query, parameters).fetchall()]

    def history(self, feedback_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            self._require(connection, feedback_id)
            return self._history_rows(connection, feedback_id)

    @staticmethod
    def _require_chris(actor: str) -> None:
        if actor != "chris":
            raise PermissionError("only the authenticated human principal may resolve or verify feedback")

    @staticmethod
    def _require(connection: sqlite3.Connection, feedback_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM product_feedback WHERE id=?", (feedback_id,)).fetchone()
        if row is None:
            raise FeedbackNotFound("feedback item not found")
        return row

    @staticmethod
    def _history(connection: sqlite3.Connection, feedback_id: str, action: str, actor: str,
                 from_status: str | None, to_status: str | None, details: dict[str, Any]) -> None:
        connection.execute(
            """INSERT INTO feedback_history
               (feedback_id,action,actor,at,from_status,to_status,details_json)
               VALUES (?,?,?,?,?,?,?)""",
            (feedback_id, action, actor, _now(), from_status, to_status,
             json.dumps(details, sort_keys=True)),
        )

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["related_ids"] = json.loads(item.get("related_ids") or "[]")
        item["implementation_links"] = json.loads(item.get("implementation_links") or "[]")
        return item

    @staticmethod
    def _decode_match(row: sqlite3.Row) -> dict[str, Any]:
        match = dict(row)
        match["implementation_links"] = json.loads(match.pop("implementation_links_json") or "[]")
        return match

    @staticmethod
    def _history_rows(connection: sqlite3.Connection, feedback_id: str) -> list[dict[str, Any]]:
        result = []
        for row in connection.execute(
            "SELECT * FROM feedback_history WHERE feedback_id=? ORDER BY sequence", (feedback_id,)
        ).fetchall():
            event = dict(row)
            event["details"] = json.loads(event.pop("details_json") or "{}")
            result.append(event)
        return result


def _now() -> str:
    return datetime.now(UTC).isoformat()
