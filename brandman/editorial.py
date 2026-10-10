"""Brand-OS-owned editorial candidates and newsletter issue lifecycle.

This module deliberately stops at the connector boundary.  It prepares and
governs canonical newsletter content, then records an idempotent receipt after
an adapter (Beehiiv initially) has created the external draft.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from brandman.brand_guidelines import BrandGuidelineStore


class EditorialError(RuntimeError):
    """Base error for editorial domain rules."""


class InvalidTransition(EditorialError):
    """Raised when an issue lifecycle transition is not permitted."""


class ApprovalBlocked(EditorialError):
    """Raised when an issue is not safe or complete enough to approve."""


class ExportConflict(EditorialError):
    """Raised when an idempotency key is reused for a different export."""


class IssueLifecycle(StrEnum):
    IDEA = "idea"
    OUTLINE = "outline"
    DRAFT = "draft"
    FACT_CHECKED = "fact_checked"
    APPROVED = "approved"
    EXPORTED = "exported"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    ABANDONED = "abandoned"
    ARCHIVED = "archived"


LIFECYCLE = tuple(state for state in IssueLifecycle if state not in {IssueLifecycle.ABANDONED, IssueLifecycle.ARCHIVED})
_NEXT = {current: LIFECYCLE[index + 1] for index, current in enumerate(LIFECYCLE[:-1])}
_TRACKING_QUERY_PREFIXES = ("utm_",)
_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_PRIMARY_AUTHORITIES = {"primary", "official", "issuer", "regulator", "filing"}
_SCORE_WEIGHTS = {
    "relevance": 0.22,
    "urgency": 0.15,
    "reader_value": 0.18,
    "novelty": 0.10,
    "confidence": 0.13,
    "search_opportunity": 0.06,
    "social_potential": 0.06,
    "commercial_relevance": 0.04,
    "differentiation": 0.06,
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS editorial_schema_migrations (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS editorial_candidates (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL, duplicate_identity TEXT NOT NULL,
  title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
  recommended_treatment TEXT NOT NULL DEFAULT 'monitor', score REAL NOT NULL,
  scoring_inputs TEXT NOT NULL, rationale TEXT NOT NULL DEFAULT '[]',
  supporting_sources TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(brand_id, duplicate_identity)
);
CREATE INDEX IF NOT EXISTS editorial_candidates_inbox
  ON editorial_candidates(brand_id, status, score DESC, updated_at DESC);
CREATE TABLE IF NOT EXISTS newsletter_issues (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL, candidate_id TEXT,
  lifecycle TEXT NOT NULL, current_revision INTEGER NOT NULL,
  approved_revision INTEGER, approved_by TEXT, approved_at TEXT,
  beehiiv_external_id TEXT, beehiiv_preview_url TEXT,
  scheduled_for TEXT, published_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(candidate_id) REFERENCES editorial_candidates(id),
  UNIQUE(brand_id, beehiiv_external_id)
);
CREATE INDEX IF NOT EXISTS newsletter_issues_queue
  ON newsletter_issues(brand_id, lifecycle, updated_at DESC);
CREATE TABLE IF NOT EXISTS newsletter_revisions (
  id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, revision INTEGER NOT NULL,
  editorial_thesis TEXT NOT NULL DEFAULT '', target_reader TEXT NOT NULL DEFAULT '',
  intended_outcome TEXT NOT NULL DEFAULT '', working_title TEXT NOT NULL DEFAULT '',
  final_title TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',
  preview_text TEXT NOT NULL DEFAULT '', sections TEXT NOT NULL DEFAULT '[]',
  cta TEXT NOT NULL DEFAULT '{}', seo TEXT NOT NULL DEFAULT '{}',
  content_basis TEXT NOT NULL DEFAULT '{}',
  delivery_metadata TEXT NOT NULL DEFAULT '{}',
  claims TEXT NOT NULL DEFAULT '[]', source_provenance TEXT NOT NULL DEFAULT '[]',
  change_note TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(issue_id) REFERENCES newsletter_issues(id),
  UNIQUE(issue_id, revision)
);
CREATE TABLE IF NOT EXISTS newsletter_export_receipts (
  id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, revision INTEGER NOT NULL,
  connector TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
  payload_fingerprint TEXT NOT NULL, external_id TEXT NOT NULL,
  preview_url TEXT, created_at TEXT NOT NULL,
  FOREIGN KEY(issue_id) REFERENCES newsletter_issues(id),
  UNIQUE(connector, external_id)
);
CREATE TABLE IF NOT EXISTS newsletter_fact_checks (
  id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, revision INTEGER NOT NULL,
  reviewer TEXT NOT NULL, verdicts TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '',
  content_fingerprint TEXT NOT NULL, passed INTEGER NOT NULL, created_at TEXT NOT NULL,
  guideline_version_id TEXT, guideline_fingerprint TEXT,
  FOREIGN KEY(issue_id) REFERENCES newsletter_issues(id),
  UNIQUE(issue_id, revision)
);
CREATE TABLE IF NOT EXISTS editorial_lifecycle_events (
  id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
  action TEXT NOT NULL, from_state TEXT NOT NULL, to_state TEXT NOT NULL,
  actor TEXT NOT NULL, reason TEXT NOT NULL, revision INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS editorial_lifecycle_events_entity
  ON editorial_lifecycle_events(entity_type, entity_id, created_at, id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be JSON-persistable") from exc


def _canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_QUERY_KEYS
        and not key.lower().startswith(_TRACKING_QUERY_PREFIXES)
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def candidate_duplicate_identity(
    brand_id: str,
    title: str,
    *,
    source_ids: Sequence[str] = (),
    source_urls: Sequence[str] = (),
) -> str:
    """Return a stable identity for deduplicating the editorial inbox."""

    normalized_title = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    sources = sorted({str(value).strip() for value in source_ids if str(value).strip()})
    urls = sorted({_canonical_url(value) for value in source_urls if str(value).strip()})
    material = _json({"brand": brand_id, "title": normalized_title, "sources": sources, "urls": urls})
    return sha256(material.encode()).hexdigest()


def score_editorial_candidate(dimensions: Mapping[str, float]) -> float:
    """Score a candidate from zero to 100 using explicit deterministic weights."""

    unknown = set(dimensions) - set(_SCORE_WEIGHTS)
    if unknown:
        raise ValueError(f"unknown scoring dimensions: {', '.join(sorted(unknown))}")
    values: dict[str, float] = {}
    for key in _SCORE_WEIGHTS:
        value = float(dimensions.get(key, 0.0))
        if value < 0 or value > 1:
            raise ValueError(f"{key} must be between 0 and 1")
        values[key] = value
    return round(sum(values[key] * weight for key, weight in _SCORE_WEIGHTS.items()) * 100, 2)


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("scoring_inputs", "rationale", "supporting_sources", "intelligence", "sections", "cta", "seo", "content_basis", "delivery_metadata", "claims", "source_provenance"):
        if key in result:
            result[key] = json.loads(result[key])
    return result


class EditorialStore:
    """Additive SQLite repository safe to initialize against an existing database."""

    def __init__(self, database: str | Path, *, clock: Callable[[], str] = _now) -> None:
        self.database = str(database)
        self.clock = clock
        self.init_schema()
        self.guidelines = BrandGuidelineStore(database, clock=clock)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def init_schema(self) -> None:
        if self.database != ":memory:":
            Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO editorial_schema_migrations(version, applied_at) VALUES (1, ?)", (self.clock(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO editorial_schema_migrations(version, applied_at) VALUES (2, ?)", (self.clock(),),
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(editorial_candidates)")
            }
            additions = {
                "publisher_name": "TEXT NOT NULL DEFAULT ''",
                "cluster_key": "TEXT NOT NULL DEFAULT ''",
                "intelligence": "TEXT NOT NULL DEFAULT '{}'",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE editorial_candidates ADD COLUMN {name} {declaration}"
                    )
            revision_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(newsletter_revisions)")
            }
            if "content_basis" not in revision_columns:
                connection.execute(
                    "ALTER TABLE newsletter_revisions ADD COLUMN content_basis TEXT NOT NULL DEFAULT '{}'"
                )
            if "delivery_metadata" not in revision_columns:
                connection.execute(
                    "ALTER TABLE newsletter_revisions ADD COLUMN delivery_metadata TEXT NOT NULL DEFAULT '{}'"
                )
            fact_check_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(newsletter_fact_checks)")
            }
            for name in ("guideline_version_id", "guideline_fingerprint"):
                if name not in fact_check_columns:
                    connection.execute(f"ALTER TABLE newsletter_fact_checks ADD COLUMN {name} TEXT")
            connection.execute(
                "INSERT OR IGNORE INTO editorial_schema_migrations(version, applied_at) VALUES (3, ?)",
                (self.clock(),),
            )

    def upsert_candidate(
        self,
        brand_id: str,
        title: str,
        dimensions: Mapping[str, float],
        *,
        summary: str = "",
        recommended_treatment: str = "monitor",
        rationale: Sequence[str] = (),
        supporting_sources: Sequence[Mapping[str, Any]] = (),
        duplicate_identity: str | None = None,
        publisher_name: str = "",
        cluster_key: str = "",
        intelligence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not brand_id.strip() or not title.strip():
            raise ValueError("brand_id and title are required")
        source_ids = [str(source.get("source_id", "")) for source in supporting_sources]
        source_urls = [str(source.get("url", "")) for source in supporting_sources]
        identity = duplicate_identity or candidate_duplicate_identity(
            brand_id, title, source_ids=source_ids, source_urls=source_urls
        )
        timestamp = self.clock()
        values = {
            "id": str(uuid4()), "brand_id": brand_id, "duplicate_identity": identity,
            "title": title, "summary": summary, "recommended_treatment": recommended_treatment,
            "score": score_editorial_candidate(dimensions), "scoring_inputs": _json(dict(dimensions)),
            "rationale": _json(list(rationale)), "supporting_sources": _json(list(supporting_sources)),
            "publisher_name": publisher_name.strip(), "cluster_key": cluster_key.strip(),
            "intelligence": _json(dict(intelligence or {})),
            "created_at": timestamp, "updated_at": timestamp,
        }
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO editorial_candidates
                   (id,brand_id,duplicate_identity,title,summary,recommended_treatment,score,
                    scoring_inputs,rationale,supporting_sources,publisher_name,cluster_key,intelligence,
                    status,created_at,updated_at)
                   VALUES (:id,:brand_id,:duplicate_identity,:title,:summary,:recommended_treatment,
                    :score,:scoring_inputs,:rationale,:supporting_sources,:publisher_name,:cluster_key,
                    :intelligence,'open',:created_at,:updated_at)
                   ON CONFLICT(brand_id,duplicate_identity) DO UPDATE SET
                    title=excluded.title, summary=excluded.summary,
                    recommended_treatment=excluded.recommended_treatment, score=excluded.score,
                    scoring_inputs=excluded.scoring_inputs, rationale=excluded.rationale,
                    supporting_sources=excluded.supporting_sources,
                    publisher_name=excluded.publisher_name, cluster_key=excluded.cluster_key,
                    intelligence=excluded.intelligence, updated_at=excluded.updated_at
                   WHERE editorial_candidates.status IN ('open','selected')""",
                values,
            )
            row = connection.execute(
                "SELECT * FROM editorial_candidates WHERE brand_id=? AND duplicate_identity=?",
                (brand_id, identity),
            ).fetchone()
        decoded = _decode(row)
        assert decoded is not None
        return decoded

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM editorial_candidates WHERE id=?", (candidate_id,)).fetchone()
        decoded = _decode(row)
        if decoded is None:
            raise KeyError(f"unknown editorial candidate: {candidate_id}")
        return decoded

    def list_candidates(self, brand_id: str, *, status: str | None = "open", limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        query = "SELECT * FROM editorial_candidates WHERE brand_id=?"
        values: list[Any] = [brand_id]
        if status is not None:
            query += " AND status=?"
            values.append(status)
        query += " ORDER BY score DESC, updated_at DESC LIMIT ?"
        values.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [_decode(row) for row in rows]  # type: ignore[misc]

    def abandon_candidate(self, candidate_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        """Remove an unselected opportunity from active consideration without deleting it."""

        return self._transition_candidate(candidate_id, "abandoned", actor=actor, reason=reason)

    def archive_candidate(self, candidate_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        """Archive a candidate only when it has no active downstream issue."""

        return self._transition_candidate(candidate_id, "archived", actor=actor, reason=reason)

    def _transition_candidate(
        self, candidate_id: str, target: str, *, actor: str, reason: str,
    ) -> dict[str, Any]:
        self._require_cleanup_identity(actor, reason)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            candidate = connection.execute(
                "SELECT * FROM editorial_candidates WHERE id=?", (candidate_id,),
            ).fetchone()
            if candidate is None:
                raise KeyError(f"unknown editorial candidate: {candidate_id}")
            current = str(candidate["status"])
            allowed = {"abandoned": {"open"}, "archived": {"open", "abandoned", "selected"}}
            if current not in allowed[target]:
                raise InvalidTransition(f"cannot transition candidate {current} to {target}")
            if target == "archived":
                active = connection.execute(
                    """SELECT id,lifecycle FROM newsletter_issues WHERE candidate_id=?
                       AND lifecycle NOT IN ('abandoned','archived','published') LIMIT 1""",
                    (candidate_id,),
                ).fetchone()
                if active is not None:
                    raise InvalidTransition(
                        f"cannot archive candidate with active newsletter issue {active['id']} ({active['lifecycle']})"
                    )
            timestamp = self.clock()
            connection.execute(
                "UPDATE editorial_candidates SET status=?, updated_at=? WHERE id=?",
                (target, timestamp, candidate_id),
            )
            self._record_lifecycle_event(
                connection, "candidate", candidate_id, target, current, target,
                actor, reason, None, timestamp,
            )
        return self.get_candidate(candidate_id)

    def list_candidate_history(self, candidate_id: str) -> list[dict[str, Any]]:
        self.get_candidate(candidate_id)
        return self._list_lifecycle_events("candidate", candidate_id)

    def create_issue(
        self, brand_id: str, content: Mapping[str, Any], *, created_by: str,
        candidate_id: str | None = None,
    ) -> dict[str, Any]:
        if not brand_id.strip() or not created_by.strip():
            raise ValueError("brand_id and created_by are required")
        issue_id = str(uuid4())
        timestamp = self.clock()
        with self._connect() as connection:
            if candidate_id:
                candidate = connection.execute(
                    "SELECT brand_id FROM editorial_candidates WHERE id=?", (candidate_id,),
                ).fetchone()
                if candidate is None:
                    raise KeyError(f"unknown editorial candidate: {candidate_id}")
                if candidate["brand_id"] != brand_id:
                    raise ValueError("editorial candidate belongs to a different brand")
            connection.execute(
                """INSERT INTO newsletter_issues
                   (id,brand_id,candidate_id,lifecycle,current_revision,created_at,updated_at)
                   VALUES (?,?,?,'idea',1,?,?)""",
                (issue_id, brand_id, candidate_id, timestamp, timestamp),
            )
            self._insert_revision(connection, issue_id, 1, content, created_by, timestamp)
            if candidate_id:
                connection.execute("UPDATE editorial_candidates SET status='selected', updated_at=? WHERE id=?", (timestamp, candidate_id))
        return self.get_issue(issue_id)

    def revise_issue(
        self, issue_id: str, changes: Mapping[str, Any], *, created_by: str,
        change_note: str = "",
    ) -> dict[str, Any]:
        if not created_by.strip():
            raise ValueError("created_by is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            issue = connection.execute("SELECT * FROM newsletter_issues WHERE id=?", (issue_id,)).fetchone()
            if issue is None:
                raise KeyError(f"unknown newsletter issue: {issue_id}")
            if issue["lifecycle"] in {IssueLifecycle.ABANDONED.value, IssueLifecycle.ARCHIVED.value}:
                raise InvalidTransition("an abandoned or archived issue cannot be revised")
            current = connection.execute(
                "SELECT * FROM newsletter_revisions WHERE issue_id=? AND revision=?",
                (issue_id, issue["current_revision"]),
            ).fetchone()
            assert current is not None
            content = _decode(current)
            assert content is not None
            editable = {key: content[key] for key in _REVISION_FIELDS}
            unknown = set(changes) - set(_REVISION_FIELDS)
            if unknown:
                raise ValueError(f"unknown revision fields: {', '.join(sorted(unknown))}")
            editable.update(changes)
            revision = int(issue["current_revision"]) + 1
            timestamp = self.clock()
            editable["change_note"] = change_note
            self._insert_revision(connection, issue_id, revision, editable, created_by, timestamp)
            previous_state = IssueLifecycle(issue["lifecycle"])
            reset_state = IssueLifecycle.DRAFT if LIFECYCLE.index(previous_state) >= LIFECYCLE.index(IssueLifecycle.FACT_CHECKED) else previous_state
            connection.execute(
                """UPDATE newsletter_issues SET current_revision=?, lifecycle=?,
                   approved_revision=NULL, approved_by=NULL, approved_at=NULL,
                   beehiiv_external_id=NULL, beehiiv_preview_url=NULL,
                   scheduled_for=NULL, published_at=NULL, updated_at=? WHERE id=?""",
                (revision, reset_state.value, timestamp, issue_id),
            )
            self._stale_distribution_derivatives(
                connection, issue_id, revision, actor=created_by, timestamp=timestamp,
            )
        return self.get_issue(issue_id)

    def reject_issue(
        self, issue_id: str, *, actor: str, reason: str, expected_revision: int,
    ) -> dict[str, Any]:
        """Return an exact fact-checked revision for changes without reusing it.

        Rejection advances to a byte-equivalent working revision so the rejected
        fact check, package memberships, dispatch approvals, and handoffs cannot
        accidentally authorize later delivery.  The immutable rejected revision
        and the review reason remain available in history.
        """
        self._require_cleanup_identity(actor, reason)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            issue = connection.execute(
                "SELECT * FROM newsletter_issues WHERE id=?", (issue_id,),
            ).fetchone()
            if issue is None:
                raise KeyError(f"unknown newsletter issue: {issue_id}")
            if issue["lifecycle"] != IssueLifecycle.FACT_CHECKED.value:
                raise InvalidTransition("only a fact_checked issue may be rejected")
            if int(issue["current_revision"]) != expected_revision:
                raise ApprovalBlocked("rejection revision does not match the current revision")
            current = connection.execute(
                "SELECT * FROM newsletter_revisions WHERE issue_id=? AND revision=?",
                (issue_id, expected_revision),
            ).fetchone()
            assert current is not None
            content = _decode(current)
            assert content is not None
            next_revision = expected_revision + 1
            timestamp = self.clock()
            editable = {key: content[key] for key in _REVISION_FIELDS}
            editable["change_note"] = f"Review rejected: {reason.strip()}"
            self._insert_revision(
                connection, issue_id, next_revision, editable, actor, timestamp,
            )
            connection.execute(
                """UPDATE newsletter_issues SET current_revision=?, lifecycle='draft',
                   approved_revision=NULL, approved_by=NULL, approved_at=NULL,
                   beehiiv_external_id=NULL, beehiiv_preview_url=NULL,
                   scheduled_for=NULL, published_at=NULL, updated_at=? WHERE id=?""",
                (next_revision, timestamp, issue_id),
            )
            self._stale_distribution_derivatives(
                connection, issue_id, next_revision, actor=actor, timestamp=timestamp,
            )
            self._record_lifecycle_event(
                connection, "newsletter_issue", issue_id, "rejected",
                IssueLifecycle.FACT_CHECKED.value, IssueLifecycle.DRAFT.value,
                actor, reason.strip(), expected_revision, timestamp,
            )
        return self.get_issue(issue_id)

    @staticmethod
    def _stale_distribution_derivatives(
        connection: sqlite3.Connection, issue_id: str, current_revision: int,
        *, actor: str, timestamp: str,
    ) -> None:
        """Atomically quarantine every package derived from an older anchor revision."""
        tables = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not {"newsletter_distribution_packages", "newsletter_distribution_artifacts"} <= tables:
            return
        if "campaign_asset_memberships" in tables:
            packages = connection.execute(
                """SELECT p.id FROM newsletter_distribution_packages p
                   WHERE p.issue_id=? AND p.issue_revision<>? AND
                     (p.status<>'stale' OR EXISTS (
                       SELECT 1 FROM campaign_asset_memberships m
                       WHERE m.package_id=p.id AND m.active=1
                     ))""",
                (issue_id, current_revision),
            ).fetchall()
        else:
            packages = connection.execute(
                """SELECT id FROM newsletter_distribution_packages
                   WHERE issue_id=? AND issue_revision<>? AND status<>'stale'""",
                (issue_id, current_revision),
            ).fetchall()
        if not packages:
            return
        package_ids = [row["id"] for row in packages]
        placeholders = ",".join("?" for _ in package_ids)
        reason = f"anchor newsletter advanced to revision {current_revision}; regenerate package"
        connection.execute(
            f"""UPDATE newsletter_distribution_packages
                SET status='stale',last_error=?,updated_at=? WHERE id IN ({placeholders})""",
            (reason, timestamp, *package_ids),
        )
        if "campaign_asset_memberships" in tables:
            # Withdraw every approval while the old memberships are still
            # addressable, then deactivate them so the exact next revision can
            # establish a new primary attribution context. Historical rows and
            # relationships remain intact for audit/reconstruction.
            from .approval_snapshots import invalidate_membership_approval

            memberships = connection.execute(
                f"""SELECT * FROM campaign_asset_memberships
                    WHERE package_id IN ({placeholders}) AND active=1""", package_ids,
            ).fetchall()
            for membership in memberships:
                invalidate_membership_approval(
                    connection, membership, actor=actor, reason=reason, timestamp=timestamp,
                )
            connection.execute(
                f"""UPDATE campaign_asset_memberships SET active=0,updated_at=?
                    WHERE package_id IN ({placeholders}) AND active=1""",
                (timestamp, *package_ids),
            )
            if "campaign_membership_audit" in tables:
                connection.executemany(
                    """INSERT INTO campaign_membership_audit
                       (membership_id,action,actor,reason,from_package_id,from_campaign_id,
                        to_package_id,to_campaign_id,at)
                       VALUES (?,'deactivated',?,?,?,?,?,?,?)""",
                    [(
                        row["id"], actor, reason, row["package_id"], row["campaign_id"],
                        row["package_id"], row["campaign_id"], timestamp,
                    ) for row in memberships],
                )
        artifacts = connection.execute(
            f"""SELECT dispatch_item_id,post_id FROM newsletter_distribution_artifacts
                WHERE package_id IN ({placeholders})""", package_ids,
        ).fetchall()
        dispatch_ids = [row["dispatch_item_id"] for row in artifacts]
        post_ids = [row["post_id"] for row in artifacts]
        if post_ids:
            post_marks = ",".join("?" for _ in post_ids)
            connection.execute(
                f"UPDATE posts SET status='stale',updated_at=? WHERE id IN ({post_marks}) AND status<>'published'",
                (timestamp, *post_ids),
            )
        if dispatch_ids and "dispatch_items" in tables:
            dispatch_marks = ",".join("?" for _ in dispatch_ids)
            rows = connection.execute(
                f"SELECT id,revision FROM dispatch_items WHERE id IN ({dispatch_marks})", dispatch_ids,
            ).fetchall()
            connection.execute(
                f"""UPDATE dispatch_items SET status='cancelled',approval_approver=NULL,
                    approval_revision=NULL,approval_at=NULL,approval_batch_id=NULL,
                    idempotency_key=NULL,dispatch_claim=NULL,last_error=?,updated_at=?
                    WHERE id IN ({dispatch_marks})
                      AND status NOT IN ('published','measured','cancelled')""",
                (reason, timestamp, *dispatch_ids),
            )
            if "dispatch_audit" in tables:
                connection.executemany(
                    """INSERT INTO dispatch_audit(item_id,action,actor,at,revision,detail)
                       VALUES (?,'anchor revision invalidated derivative',?,?,?,?)""",
                    [(row["id"], actor, timestamp, row["revision"], reason) for row in rows],
                )
            if {"approval_snapshots", "approval_snapshot_invalidations"} <= tables:
                snapshots = connection.execute(
                    f"""SELECT id FROM approval_snapshots WHERE resource_type='dispatch_item'
                        AND resource_id IN ({dispatch_marks})""", dispatch_ids,
                ).fetchall()
                connection.executemany(
                    """INSERT OR IGNORE INTO approval_snapshot_invalidations
                       (snapshot_id,actor,reason,invalidated_at) VALUES (?,?,?,?)""",
                    [(row["id"], actor, reason, timestamp) for row in snapshots],
                )
            if {"execution_tasks", "execution_task_audit"} <= tables:
                tasks = connection.execute(
                    f"""SELECT id,status FROM execution_tasks WHERE resource_id IN ({dispatch_marks})
                        AND status IN ('pending','claimed')""", dispatch_ids,
                ).fetchall()
                connection.executemany(
                    """UPDATE execution_tasks SET status='stale',claimed_by=NULL,claimed_at=NULL,
                       claim_expires_at=NULL,claim_token_hash=NULL,updated_at=? WHERE id=?""",
                    [(timestamp, row["id"]) for row in tasks],
                )
                connection.executemany(
                    """INSERT INTO execution_task_audit(task_id,action,actor,at,detail_json)
                       VALUES (?,'invalidated',?,?,?)""",
                    [(row["id"], actor, timestamp, _json({"reason": reason})) for row in tasks],
                )

    def get_issue(self, issue_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            issue = connection.execute("SELECT * FROM newsletter_issues WHERE id=?", (issue_id,)).fetchone()
            if issue is None:
                raise KeyError(f"unknown newsletter issue: {issue_id}")
            revision = connection.execute(
                "SELECT * FROM newsletter_revisions WHERE issue_id=? AND revision=?",
                (issue_id, issue["current_revision"]),
            ).fetchone()
            fact_check = connection.execute(
                "SELECT * FROM newsletter_fact_checks WHERE issue_id=? AND revision=?",
                (issue_id, issue["current_revision"]),
            ).fetchone()
        result = dict(issue)
        result["content"] = _decode(revision)
        result["approval_valid"] = (
            result["approved_revision"] is not None
            and result["approved_revision"] == result["current_revision"]
        )
        policy = self.guidelines.evaluate_newsletter(
            brand_id=result["brand_id"], issue_id=result["id"],
            revision=int(result["current_revision"]), content=result["content"] or {},
        )
        result["governance"] = newsletter_governance_summary(
            result, result["content"] or {}, dict(fact_check) if fact_check else None,
            policy=policy,
        )
        return result

    def list_issues(
        self, brand_id: str, *, lifecycle: IssueLifecycle | str | None = None,
        include_inactive: bool = False, limit: int = 100,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        query = "SELECT id FROM newsletter_issues WHERE brand_id=?"
        values: list[Any] = [brand_id]
        if lifecycle is not None:
            query += " AND lifecycle=?"
            values.append(IssueLifecycle(lifecycle).value)
        elif not include_inactive:
            query += " AND lifecycle NOT IN ('abandoned','archived')"
        query += " ORDER BY updated_at DESC, id LIMIT ?"
        values.append(limit)
        with self._connect() as connection:
            ids = [row["id"] for row in connection.execute(query, values).fetchall()]
        return [self.get_issue(issue_id) for issue_id in ids]

    def abandon_issue(self, issue_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        """End pre-publication work while preserving every immutable revision."""

        return self._cleanup_issue(issue_id, IssueLifecycle.ABANDONED, actor=actor, reason=reason)

    def archive_issue(self, issue_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        """Hide only terminal editorial work from active operating views."""

        return self._cleanup_issue(issue_id, IssueLifecycle.ARCHIVED, actor=actor, reason=reason)

    def _cleanup_issue(
        self, issue_id: str, target: IssueLifecycle, *, actor: str, reason: str,
    ) -> dict[str, Any]:
        self._require_cleanup_identity(actor, reason)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            issue = connection.execute(
                "SELECT * FROM newsletter_issues WHERE id=?", (issue_id,),
            ).fetchone()
            if issue is None:
                raise KeyError(f"unknown newsletter issue: {issue_id}")
            current = IssueLifecycle(issue["lifecycle"])
            allowed = {
                IssueLifecycle.ABANDONED: {
                    IssueLifecycle.IDEA, IssueLifecycle.OUTLINE, IssueLifecycle.DRAFT,
                    IssueLifecycle.FACT_CHECKED,
                },
                IssueLifecycle.ARCHIVED: {IssueLifecycle.ABANDONED, IssueLifecycle.PUBLISHED},
            }
            if current not in allowed[target]:
                raise InvalidTransition(f"cannot transition {current.value} to {target.value}")
            if self._has_active_export_job(connection, issue_id):
                raise InvalidTransition("cannot clean up an issue with an active newsletter export job")
            timestamp = self.clock()
            connection.execute(
                "UPDATE newsletter_issues SET lifecycle=?, updated_at=? WHERE id=?",
                (target.value, timestamp, issue_id),
            )
            self._record_lifecycle_event(
                connection, "newsletter_issue", issue_id, target.value,
                current.value, target.value, actor, reason,
                int(issue["current_revision"]), timestamp,
            )
        return self.get_issue(issue_id)

    def list_issue_history(self, issue_id: str) -> list[dict[str, Any]]:
        self.get_issue(issue_id)
        return self._list_lifecycle_events("newsletter_issue", issue_id)

    def list_revisions(self, issue_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM newsletter_revisions WHERE issue_id=? ORDER BY revision", (issue_id,)
            ).fetchall()
        return [_decode(row) for row in rows]  # type: ignore[misc]

    def get_fact_check(self, issue_id: str, revision: int | None = None) -> dict[str, Any] | None:
        issue = self.get_issue(issue_id)
        target_revision = revision or int(issue["current_revision"])
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM newsletter_fact_checks WHERE issue_id=? AND revision=?",
                (issue_id, target_revision),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["verdicts"] = json.loads(result["verdicts"])
        result["passed"] = bool(result["passed"])
        return result

    def record_fact_check(
        self, issue_id: str, *, expected_revision: int, reviewer: str,
        verdicts: Sequence[Mapping[str, Any]] = (), notes: str = "",
    ) -> dict[str, Any]:
        """Persist a reviewer-bound, revision-exact verification record.

        A lifecycle label is never accepted as proof of fact checking.  Every
        claim must have a positive verdict and citations must resolve to the
        revision's canonical provenance list.
        """
        if not reviewer.strip():
            raise ValueError("reviewer is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            issue = connection.execute("SELECT * FROM newsletter_issues WHERE id=?", (issue_id,)).fetchone()
            if issue is None:
                raise KeyError(f"unknown newsletter issue: {issue_id}")
            if issue["lifecycle"] != IssueLifecycle.DRAFT.value:
                raise InvalidTransition("only a draft issue may be fact checked")
            if int(issue["current_revision"]) != expected_revision:
                raise ApprovalBlocked("fact-check revision does not match the current revision")
            revision = connection.execute(
                "SELECT * FROM newsletter_revisions WHERE issue_id=? AND revision=?",
                (issue_id, expected_revision),
            ).fetchone()
            assert revision is not None
            content = _decode(revision)
            assert content is not None
            blockers = editorial_completeness_blockers(content)
            blockers.extend(source_provenance_blockers(content["claims"], content["source_provenance"]))
            blockers.extend(canonical_revalidation_blockers(connection, content["source_provenance"]))
            policy = self.guidelines.evaluate_newsletter(
                brand_id=issue["brand_id"], issue_id=issue_id,
                revision=expected_revision, content=content,
            )
            blockers.extend(item["message"] for item in policy["blockers"])
            verdict_by_claim = {
                str(verdict.get("claim_id", "")): verdict for verdict in verdicts
                if str(verdict.get("claim_id", "")).strip()
            }
            for index, claim in enumerate(content["claims"], start=1):
                claim_id = str(claim.get("id") or f"claim-{index}")
                verdict = verdict_by_claim.get(claim_id)
                if verdict is None or verdict.get("verified") is not True:
                    blockers.append(f"{claim_id} lacks a positive reviewer verdict")
            if blockers:
                raise ApprovalBlocked("fact check incomplete: " + "; ".join(blockers))
            fingerprint = "sha256:" + sha256(_json({key: content[key] for key in _REVISION_FIELDS}).encode()).hexdigest()
            guideline = policy.get("guideline") or {}
            record = {
                "id": str(uuid4()), "issue_id": issue_id, "revision": expected_revision,
                "reviewer": reviewer, "verdicts": _json(list(verdicts)), "notes": notes,
                "content_fingerprint": fingerprint, "passed": 1, "created_at": self.clock(),
                "guideline_version_id": guideline.get("version_id"),
                "guideline_fingerprint": guideline.get("content_fingerprint"),
            }
            connection.execute(
                """INSERT INTO newsletter_fact_checks
                   (id,issue_id,revision,reviewer,verdicts,notes,content_fingerprint,passed,created_at,
                    guideline_version_id,guideline_fingerprint)
                   VALUES (:id,:issue_id,:revision,:reviewer,:verdicts,:notes,:content_fingerprint,:passed,:created_at,
                    :guideline_version_id,:guideline_fingerprint)
                   ON CONFLICT(issue_id,revision) DO UPDATE SET
                     reviewer=excluded.reviewer, verdicts=excluded.verdicts, notes=excluded.notes,
                     content_fingerprint=excluded.content_fingerprint, passed=excluded.passed,
                     guideline_version_id=excluded.guideline_version_id,
                     guideline_fingerprint=excluded.guideline_fingerprint,
                     created_at=excluded.created_at""",
                record,
            )
            connection.execute(
                "UPDATE newsletter_issues SET lifecycle='fact_checked', updated_at=? WHERE id=?",
                (record["created_at"], issue_id),
            )
        checked = self.get_fact_check(issue_id, expected_revision)
        assert checked is not None
        return checked

    def transition(self, issue_id: str, target: IssueLifecycle | str) -> dict[str, Any]:
        target = IssueLifecycle(target)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM newsletter_issues WHERE id=?", (issue_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown newsletter issue: {issue_id}")
            current = IssueLifecycle(row["lifecycle"])
            if _NEXT.get(current) != target:
                raise InvalidTransition(f"cannot transition {current.value} to {target.value}")
            if target == IssueLifecycle.FACT_CHECKED:
                raise InvalidTransition("use record_fact_check for revision-bound verification")
            if target == IssueLifecycle.APPROVED:
                raise InvalidTransition("use approve_issue for revision-bound approval")
            if target == IssueLifecycle.EXPORTED:
                raise InvalidTransition("use record_export_receipt after connector export")
            timestamp = self.clock()
            if target == IssueLifecycle.SCHEDULED and not row["beehiiv_external_id"]:
                raise InvalidTransition("an exported Beehiiv issue is required before scheduling")
            connection.execute("UPDATE newsletter_issues SET lifecycle=?, updated_at=? WHERE id=?", (target.value, timestamp, issue_id))
        return self.get_issue(issue_id)

    def approve_issue(self, issue_id: str, *, approver: str, expected_revision: int) -> dict[str, Any]:
        if not approver.strip():
            raise ValueError("approver is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            issue = connection.execute("SELECT * FROM newsletter_issues WHERE id=?", (issue_id,)).fetchone()
            if issue is None:
                raise KeyError(f"unknown newsletter issue: {issue_id}")
            if issue["lifecycle"] != IssueLifecycle.FACT_CHECKED.value:
                raise InvalidTransition("only a fact_checked issue may be approved")
            if int(issue["current_revision"]) != expected_revision:
                raise ApprovalBlocked("approval revision does not match the current revision")
            revision = connection.execute(
                "SELECT * FROM newsletter_revisions WHERE issue_id=? AND revision=?", (issue_id, expected_revision)
            ).fetchone()
            assert revision is not None
            content = _decode(revision)
            assert content is not None
            policy = self.guidelines.evaluate_newsletter(
                brand_id=issue["brand_id"], issue_id=issue_id,
                revision=expected_revision, content=content,
            )
            if policy["blockers"]:
                raise ApprovalBlocked(
                    "active content policy blocks approval: "
                    + "; ".join(item["message"] for item in policy["blockers"])
                )
            blockers = volatile_claim_blockers(json.loads(revision["claims"]))
            blockers.extend(canonical_revalidation_blockers(
                connection, json.loads(revision["source_provenance"]),
            ))
            if blockers:
                raise ApprovalBlocked("volatile claims require verified primary sources: " + "; ".join(blockers))
            fact_check = connection.execute(
                """SELECT passed,guideline_version_id,guideline_fingerprint
                   FROM newsletter_fact_checks WHERE issue_id=? AND revision=?""",
                (issue_id, expected_revision),
            ).fetchone()
            if fact_check is None or not fact_check["passed"]:
                raise ApprovalBlocked("the current revision lacks a governed fact-check record")
            guideline = policy.get("guideline")
            if guideline and (
                fact_check["guideline_version_id"] != guideline["version_id"]
                or fact_check["guideline_fingerprint"] != guideline["content_fingerprint"]
            ):
                raise ApprovalBlocked("the fact check predates the active brand guideline version")
            timestamp = self.clock()
            connection.execute(
                """UPDATE newsletter_issues SET lifecycle='approved', approved_revision=?,
                   approved_by=?, approved_at=?, updated_at=? WHERE id=?""",
                (expected_revision, approver, timestamp, timestamp, issue_id),
            )
        return self.get_issue(issue_id)

    def record_export_receipt(
        self, issue_id: str, *, expected_revision: int, idempotency_key: str,
        external_id: str, preview_url: str | None, payload_fingerprint: str,
        connector: str = "beehiiv",
    ) -> dict[str, Any]:
        required = (idempotency_key, external_id, payload_fingerprint, connector)
        if any(not value.strip() for value in required):
            raise ValueError("idempotency_key, external_id, payload_fingerprint, and connector are required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM newsletter_export_receipts WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                same = (
                    existing["issue_id"] == issue_id and existing["revision"] == expected_revision
                    and existing["external_id"] == external_id
                    and existing["payload_fingerprint"] == payload_fingerprint
                    and existing["connector"] == connector
                )
                if not same:
                    raise ExportConflict("idempotency key is already bound to a different export")
                return dict(existing)
            issue = connection.execute("SELECT * FROM newsletter_issues WHERE id=?", (issue_id,)).fetchone()
            if issue is None:
                raise KeyError(f"unknown newsletter issue: {issue_id}")
            if issue["lifecycle"] != IssueLifecycle.APPROVED.value:
                raise InvalidTransition("only an approved issue may be exported")
            if issue["approved_revision"] != expected_revision or issue["current_revision"] != expected_revision:
                raise ApprovalBlocked("export revision is not the currently approved revision")
            receipt = {
                "id": str(uuid4()), "issue_id": issue_id, "revision": expected_revision,
                "connector": connector, "idempotency_key": idempotency_key,
                "payload_fingerprint": payload_fingerprint, "external_id": external_id,
                "preview_url": preview_url, "created_at": self.clock(),
            }
            try:
                connection.execute(
                    """INSERT INTO newsletter_export_receipts
                       (id,issue_id,revision,connector,idempotency_key,payload_fingerprint,
                        external_id,preview_url,created_at)
                       VALUES (:id,:issue_id,:revision,:connector,:idempotency_key,
                        :payload_fingerprint,:external_id,:preview_url,:created_at)""",
                    receipt,
                )
            except sqlite3.IntegrityError as exc:
                raise ExportConflict("external export is already recorded") from exc
            connection.execute(
                """UPDATE newsletter_issues SET lifecycle='exported',
                   beehiiv_external_id=?, beehiiv_preview_url=?, updated_at=? WHERE id=?""",
                (external_id, preview_url, receipt["created_at"], issue_id),
            )
        return receipt

    def prepare_export(self, issue_id: str, *, connector: str = "beehiiv") -> dict[str, Any]:
        """Build the canonical, revision-bound payload for a connector adapter."""

        issue = self.get_issue(issue_id)
        if issue["lifecycle"] != IssueLifecycle.APPROVED.value or not issue["approval_valid"]:
            raise ApprovalBlocked("only the currently approved revision can be prepared for export")
        content = issue["content"]
        payload = {
            "issue_id": issue_id,
            "revision": issue["current_revision"],
            "connector": connector,
            "subject": content["subject"],
            "preview_text": content["preview_text"],
            "title": content["final_title"] or content["working_title"],
            "sections": content["sections"],
            "cta": content["cta"],
            "seo": content["seo"],
            "delivery_metadata": content["delivery_metadata"],
            "source_provenance": content["source_provenance"],
        }
        encoded = _json(payload)
        return {
            "payload": payload,
            "payload_fingerprint": "sha256:" + sha256(encoded.encode()).hexdigest(),
            "idempotency_key": f"newsletter-export:{connector}:{issue_id}:r{issue['current_revision']}",
        }

    def schedule_issue(self, issue_id: str, scheduled_for: str) -> dict[str, Any]:
        _parse_timestamp(scheduled_for, "scheduled_for")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            issue = connection.execute("SELECT * FROM newsletter_issues WHERE id=?", (issue_id,)).fetchone()
            if issue is None:
                raise KeyError(f"unknown newsletter issue: {issue_id}")
            if issue["lifecycle"] != IssueLifecycle.EXPORTED.value:
                raise InvalidTransition("only an exported issue may be scheduled")
            connection.execute(
                "UPDATE newsletter_issues SET lifecycle='scheduled', scheduled_for=?, updated_at=? WHERE id=?",
                (scheduled_for, self.clock(), issue_id),
            )
        return self.get_issue(issue_id)

    def mark_published(self, issue_id: str, published_at: str) -> dict[str, Any]:
        _parse_timestamp(published_at, "published_at")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            issue = connection.execute("SELECT * FROM newsletter_issues WHERE id=?", (issue_id,)).fetchone()
            if issue is None:
                raise KeyError(f"unknown newsletter issue: {issue_id}")
            if issue["lifecycle"] != IssueLifecycle.SCHEDULED.value:
                raise InvalidTransition("only a scheduled issue may be published")
            connection.execute(
                "UPDATE newsletter_issues SET lifecycle='published', published_at=?, updated_at=? WHERE id=?",
                (published_at, self.clock(), issue_id),
            )
        return self.get_issue(issue_id)

    @staticmethod
    def _require_cleanup_identity(actor: str, reason: str) -> None:
        if not actor.strip():
            raise ValueError("actor is required")
        if not reason.strip():
            raise ValueError("reason is required")

    @staticmethod
    def _record_lifecycle_event(
        connection: sqlite3.Connection, entity_type: str, entity_id: str,
        action: str, from_state: str, to_state: str, actor: str, reason: str,
        revision: int | None, timestamp: str,
    ) -> None:
        connection.execute(
            """INSERT INTO editorial_lifecycle_events
               (id,entity_type,entity_id,action,from_state,to_state,actor,reason,revision,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (str(uuid4()), entity_type, entity_id, action, from_state, to_state,
             actor, reason, revision, timestamp),
        )

    def _list_lifecycle_events(self, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM editorial_lifecycle_events
                   WHERE entity_type=? AND entity_id=? ORDER BY created_at,rowid""",
                (entity_type, entity_id),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _has_active_export_job(connection: sqlite3.Connection, issue_id: str) -> bool:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='durable_jobs'",
        ).fetchone()
        if exists is None:
            return False
        rows = connection.execute(
            """SELECT payload FROM durable_jobs
               WHERE job_type='beehiiv.newsletter.export_draft'
               AND status IN ('queued','retry','running')""",
        ).fetchall()
        for row in rows:
            try:
                if json.loads(row["payload"]).get("issue_id") == issue_id:
                    return True
            except (TypeError, ValueError, AttributeError):
                continue
        return False

    @staticmethod
    def _insert_revision(
        connection: sqlite3.Connection, issue_id: str, revision: int,
        content: Mapping[str, Any], created_by: str, timestamp: str,
    ) -> None:
        unknown = set(content) - set(_REVISION_FIELDS) - {"change_note"}
        if unknown:
            raise ValueError(f"unknown revision fields: {', '.join(sorted(unknown))}")
        values = {key: content.get(key, _REVISION_DEFAULTS[key]) for key in _REVISION_FIELDS}
        values.update({
            "id": str(uuid4()), "issue_id": issue_id, "revision": revision,
            "change_note": content.get("change_note", ""), "created_by": created_by,
            "created_at": timestamp,
        })
        for field in _JSON_REVISION_FIELDS:
            values[field] = _json(values[field])
        connection.execute(
            """INSERT INTO newsletter_revisions
               (id,issue_id,revision,editorial_thesis,target_reader,intended_outcome,
                working_title,final_title,subject,preview_text,sections,cta,seo,content_basis,delivery_metadata,claims,
                source_provenance,change_note,created_by,created_at)
               VALUES (:id,:issue_id,:revision,:editorial_thesis,:target_reader,
                :intended_outcome,:working_title,:final_title,:subject,:preview_text,
                :sections,:cta,:seo,:content_basis,:delivery_metadata,:claims,:source_provenance,:change_note,:created_by,:created_at)""",
            values,
        )


_REVISION_DEFAULTS: dict[str, Any] = {
    "editorial_thesis": "", "target_reader": "", "intended_outcome": "",
    "working_title": "", "final_title": "", "subject": "", "preview_text": "",
    "sections": [], "cta": {}, "seo": {}, "content_basis": {}, "delivery_metadata": {},
    "claims": [], "source_provenance": [],
}
_REVISION_FIELDS = tuple(_REVISION_DEFAULTS)
_JSON_REVISION_FIELDS = {"sections", "cta", "seo", "content_basis", "delivery_metadata", "claims", "source_provenance"}


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def volatile_claim_blockers(claims: Sequence[Mapping[str, Any]]) -> list[str]:
    """Explain which volatile claims lack a timestamped primary citation."""

    blockers: list[str] = []
    for index, claim in enumerate(claims, start=1):
        if not claim.get("volatile", False):
            continue
        valid = False
        for citation in claim.get("citations", []):
            authority = str(citation.get("authority_class", "")).casefold()
            primary = citation.get("is_primary") is True or authority in _PRIMARY_AUTHORITIES
            verified_at = citation.get("verified_at")
            if not primary or not verified_at or not (citation.get("source_id") or citation.get("url")):
                continue
            try:
                _parse_timestamp(str(verified_at), "verified_at")
            except ValueError:
                continue
            valid = True
            break
        if not valid:
            blockers.append(str(claim.get("text") or claim.get("id") or f"claim {index}"))
    return blockers


def editorial_completeness_blockers(content: Mapping[str, Any]) -> list[str]:
    """Return missing structural fields that make an issue unreviewable."""

    blockers: list[str] = []
    for field in ("editorial_thesis", "target_reader", "intended_outcome", "subject"):
        if not str(content.get(field, "")).strip():
            blockers.append(f"{field} is required")
    if not str(content.get("final_title") or content.get("working_title") or "").strip():
        blockers.append("a working_title or final_title is required")
    sections = content.get("sections")
    if not isinstance(sections, list) or not sections:
        blockers.append("at least one section is required")
    elif any(not isinstance(section, Mapping) or not str(section.get("body", "")).strip() for section in sections):
        blockers.append("every section requires body content")
    provenance = content.get("source_provenance")
    basis = content.get("content_basis")
    if not isinstance(basis, Mapping) or not str(basis.get("kind") or "").strip():
        # Existing source-grounded revisions predate the explicit basis field;
        # preserve that safe meaning while blocking source-less placeholders.
        if not isinstance(provenance, list) or not provenance:
            blockers.append(
                "declare content_basis as source_based, original_analysis, or opinion; "
                "source-less content also requires a basis statement"
            )
    else:
        kind = str(basis.get("kind")).strip().casefold()
        if kind not in {"source_based", "original_analysis", "opinion"}:
            blockers.append("content_basis.kind must be source_based, original_analysis, or opinion")
        elif kind == "source_based" and (not isinstance(provenance, list) or not provenance):
            blockers.append("source_based content requires canonical source provenance")
        elif kind in {"original_analysis", "opinion"} and not str(basis.get("statement") or "").strip():
            blockers.append(f"{kind} content requires an explicit basis statement")
    return blockers


def source_provenance_blockers(
    claims: Sequence[Mapping[str, Any]], provenance: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Require claim citations to resolve to one canonical revision source."""

    blockers: list[str] = []
    sources: dict[str, str | None] = {}
    for index, source in enumerate(provenance, start=1):
        source_id = str(source.get("source_id", "")).strip()
        if not source_id:
            blockers.append(f"source provenance {index} lacks source_id")
            continue
        if source_id in sources:
            blockers.append(f"duplicate source provenance identity: {source_id}")
            continue
        url = str(source.get("url", "")).strip()
        if url:
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.netloc:
                blockers.append(f"source {source_id} has an invalid canonical URL")
                sources[source_id] = None
            else:
                sources[source_id] = _canonical_url(url)
        else:
            sources[source_id] = None
    for index, claim in enumerate(claims, start=1):
        claim_id = str(claim.get("id") or f"claim-{index}")
        citations = claim.get("citations")
        if not isinstance(citations, list) or not citations:
            blockers.append(f"{claim_id} lacks a citation")
            continue
        for citation in citations:
            source_id = str(citation.get("source_id", "")).strip()
            if not source_id or source_id not in sources:
                blockers.append(f"{claim_id} cites an unknown canonical source")
                continue
            citation_url = str(citation.get("url", "")).strip()
            if citation_url and sources[source_id] and _canonical_url(citation_url) != sources[source_id]:
                blockers.append(f"{claim_id} citation URL conflicts with source {source_id}")
    return blockers


def canonical_revalidation_blockers(
    connection: sqlite3.Connection, provenance: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Fail closed when a cited source has explicit canonical drift/conflict.

    Absence of a snapshot is visible elsewhere but does not pretend that an
    automated metadata check replaces a human fact check. Only explicit drift
    or conflict blocks the existing reviewer workflow.
    """
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_canonical_revalidations'"
    ).fetchone() is None:
        return []
    blockers = []
    for source in provenance:
        source_id = str(source.get("source_id") or "").strip()
        if not source_id:
            continue
        latest = connection.execute(
            """SELECT status,rationale_json FROM source_canonical_revalidations
               WHERE source_id=? ORDER BY observed_at DESC,created_at DESC,id DESC LIMIT 1""",
            (source_id,),
        ).fetchone()
        if latest is not None and latest["status"] in {"drift", "conflict"}:
            rationale = "; ".join(json.loads(latest["rationale_json"] or "[]"))
            blockers.append(
                f"source {source_id} canonical revalidation is {latest['status']}"
                + (f": {rationale}" if rationale else "")
            )
    return blockers


def newsletter_governance_summary(
    issue: Mapping[str, Any], content: Mapping[str, Any],
    fact_check: Mapping[str, Any] | None, *, policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Explain why an issue is or is not eligible for exact human review.

    This is deliberately returned on every issue read.  Approval queues may
    filter unsafe work, but operator surfaces must never make the reason
    disappear with it.
    """

    blockers: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(code: str, message: str) -> None:
        identity = (code, message)
        if identity not in seen:
            blockers.append({"code": code, "message": message})
            seen.add(identity)

    for message in editorial_completeness_blockers(content):
        add("editorial_incomplete", message)
    for message in source_provenance_blockers(
        content.get("claims", []), content.get("source_provenance", []),
    ):
        add("source_provenance_incomplete", message)
    for claim in volatile_claim_blockers(content.get("claims", [])):
        add(
            "volatile_claim_requires_primary_source",
            f'Volatile claim "{claim}" requires a timestamped primary-source citation '
            "from the issuer, regulator, filing, or other official authority.",
        )
    policy = dict(policy or {})
    for blocker in policy.get("blockers", []):
        add(str(blocker.get("code") or "content_policy_blocked"), str(blocker.get("message") or blocker))

    lifecycle = str(issue.get("lifecycle") or "")
    current_revision = int(issue.get("current_revision") or 0)
    fact_check_valid = bool(
        fact_check
        and int(fact_check.get("revision") or 0) == current_revision
        and fact_check.get("passed") in {True, 1}
        and str(fact_check.get("reviewer") or "").strip()
        and str(fact_check.get("content_fingerprint") or "").startswith("sha256:")
    )
    guideline = policy.get("guideline")
    if guideline:
        fact_check_valid = bool(
            fact_check_valid
            and fact_check.get("guideline_version_id") == guideline.get("version_id")
            and fact_check.get("guideline_fingerprint") == guideline.get("content_fingerprint")
        )
    if lifecycle not in {
        IssueLifecycle.APPROVED.value, IssueLifecycle.EXPORTED.value,
        IssueLifecycle.SCHEDULED.value, IssueLifecycle.PUBLISHED.value,
    } and not fact_check_valid:
        add(
            "current_revision_fact_check_required",
            f"Revision {current_revision} needs a governed fact check with a positive verdict for every claim.",
        )

    reviewable = lifecycle == IssueLifecycle.FACT_CHECKED.value and fact_check_valid and not blockers
    if reviewable:
        next_action = "Review and approve the exact displayed revision."
    elif any(item["code"] == "volatile_claim_requires_primary_source" for item in blockers):
        next_action = "Replace or verify volatile claims with timestamped primary-source evidence, then fact-check this revision."
    elif any(item["code"] in {"editorial_incomplete", "source_provenance_incomplete"} for item in blockers):
        next_action = "Complete the draft and canonical source provenance, then fact-check this revision."
    elif not fact_check_valid:
        next_action = "Record a governed fact check for the current revision."
    else:
        next_action = "No approval action is required for this lifecycle state."
    return {
        "reviewable": reviewable,
        "fact_check_valid": fact_check_valid,
        "next_safe_action": next_action,
        "blockers": blockers,
        "content_policy": policy,
        "guideline": guideline,
    }


class EditorialService:
    """Small application-facing facade over the editorial repository."""

    def __init__(self, store: EditorialStore) -> None:
        self.store = store

    def create_candidate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.upsert_candidate(*args, **kwargs)

    def create_issue(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.create_issue(*args, **kwargs)

    def revise_issue(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.revise_issue(*args, **kwargs)

    def transition(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.transition(*args, **kwargs)

    def record_fact_check(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.record_fact_check(*args, **kwargs)

    def approve_issue(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.approve_issue(*args, **kwargs)

    def reject_issue(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.reject_issue(*args, **kwargs)

    def prepare_export(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.prepare_export(*args, **kwargs)

    def record_export_receipt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.record_export_receipt(*args, **kwargs)

    def schedule_issue(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.schedule_issue(*args, **kwargs)

    def mark_published(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.mark_published(*args, **kwargs)
