"""Append-only, material-bound evidence for every human approval surface."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from uuid import uuid4


SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_snapshots (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL, account_ref TEXT NOT NULL,
  campaign_id TEXT, asset_membership_id TEXT,
  resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, action_type TEXT NOT NULL,
  destination TEXT NOT NULL, intended_schedule TEXT, revision INTEGER NOT NULL,
  approver TEXT NOT NULL, approved_at TEXT NOT NULL, material_fingerprint TEXT NOT NULL,
  material_json TEXT NOT NULL, created_at TEXT NOT NULL,
  guideline_version_id TEXT, guideline_fingerprint TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS approval_snapshot_identity ON approval_snapshots(
  resource_type,resource_id,revision,material_fingerprint,approved_at,
  COALESCE(campaign_id,''),COALESCE(asset_membership_id,'')
);
CREATE TABLE IF NOT EXISTS approval_snapshot_invalidations (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id TEXT NOT NULL,
  actor TEXT NOT NULL, reason TEXT NOT NULL, invalidated_at TEXT NOT NULL,
  FOREIGN KEY(snapshot_id) REFERENCES approval_snapshots(id), UNIQUE(snapshot_id)
);
CREATE TRIGGER IF NOT EXISTS approval_snapshots_no_update
BEFORE UPDATE ON approval_snapshots BEGIN SELECT RAISE(ABORT,'approval snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS approval_snapshots_no_delete
BEFORE DELETE ON approval_snapshots BEGIN SELECT RAISE(ABORT,'approval snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS approval_invalidations_no_update
BEFORE UPDATE ON approval_snapshot_invalidations BEGIN SELECT RAISE(ABORT,'approval invalidations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS approval_invalidations_no_delete
BEFORE DELETE ON approval_snapshot_invalidations BEGIN SELECT RAISE(ABORT,'approval invalidations are immutable'); END;
"""


class ApprovalSnapshotStore:
    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='approval_snapshots'"
            ).fetchone()
            if exists:
                columns = {row["name"] for row in connection.execute(
                    "PRAGMA table_info(approval_snapshots)"
                )}
                for column in (
                    "campaign_id", "asset_membership_id", "guideline_version_id", "guideline_fingerprint",
                ):
                    if column not in columns:
                        connection.execute(f"ALTER TABLE approval_snapshots ADD COLUMN {column} TEXT")
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute(
                "PRAGMA table_info(approval_snapshots)"
            )}
            for column in (
                "campaign_id", "asset_membership_id", "guideline_version_id", "guideline_fingerprint",
            ):
                if column not in columns:
                    connection.execute(f"ALTER TABLE approval_snapshots ADD COLUMN {column} TEXT")
            self._migrate_legacy_identity(connection)
        self.backfill_current()
        self.reconcile_invalidations()

    @staticmethod
    def _migrate_legacy_identity(connection: sqlite3.Connection) -> None:
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='approval_snapshots'"
        ).fetchone()["sql"]
        legacy = "UNIQUE(resource_type,resource_id,revision,material_fingerprint)"
        if legacy not in definition.replace(" ", "").replace("\n", ""):
            return
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """CREATE TABLE approval_snapshots_v2 (
                  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL, account_ref TEXT NOT NULL,
                  campaign_id TEXT, asset_membership_id TEXT,
                  resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, action_type TEXT NOT NULL,
                  destination TEXT NOT NULL, intended_schedule TEXT, revision INTEGER NOT NULL,
                  approver TEXT NOT NULL, approved_at TEXT NOT NULL, material_fingerprint TEXT NOT NULL,
                  material_json TEXT NOT NULL, created_at TEXT NOT NULL,
                  guideline_version_id TEXT, guideline_fingerprint TEXT)"""
            )
            connection.execute(
                """INSERT INTO approval_snapshots_v2 SELECT id,brand_id,account_ref,
                   campaign_id,asset_membership_id,resource_type,resource_id,action_type,
                   destination,intended_schedule,revision,approver,approved_at,
                   material_fingerprint,material_json,created_at,NULL,NULL FROM approval_snapshots"""
            )
            invalidations = [dict(row) for row in connection.execute(
                "SELECT * FROM approval_snapshot_invalidations"
            )]
            for trigger in (
                "approval_snapshots_no_update", "approval_snapshots_no_delete",
                "approval_invalidations_no_update", "approval_invalidations_no_delete",
            ):
                connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            connection.execute("DROP TABLE approval_snapshot_invalidations")
            connection.execute("DROP TABLE approval_snapshots")
            connection.execute("ALTER TABLE approval_snapshots_v2 RENAME TO approval_snapshots")
            connection.execute(
                """CREATE TABLE approval_snapshot_invalidations (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id TEXT NOT NULL,
                  actor TEXT NOT NULL, reason TEXT NOT NULL, invalidated_at TEXT NOT NULL,
                  FOREIGN KEY(snapshot_id) REFERENCES approval_snapshots(id), UNIQUE(snapshot_id))"""
            )
            connection.executemany(
                """INSERT INTO approval_snapshot_invalidations
                   (sequence,snapshot_id,actor,reason,invalidated_at) VALUES (?,?,?,?,?)""",
                [(row["sequence"], row["snapshot_id"], row["actor"], row["reason"], row["invalidated_at"])
                 for row in invalidations],
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def capture_dispatch(self, item: Any) -> dict[str, Any]:
        if item.approval is None or item.approval.revision != item.revision:
            raise ValueError("dispatch approval must cover the exact current revision")
        payload = dict(item.payload)
        action, destination = _dispatch_action(payload)
        destination = str(payload.get("destination") or destination)
        return self._capture(
            brand_id=str(item.brand_id or "unscoped"),
            account_ref=str(payload.get("connector_account_id") or f"assisted:{item.connector}"),
            resource_type="engagement_action" if action in {"reply", "like", "follow"} else "dispatch_item",
            resource_id=item.id, action_type=action, destination=destination,
            intended_schedule=_optional_text(payload.get("scheduled_for") or payload.get("intended_schedule")),
            revision=item.revision, approver=item.approval.approver,
            approved_at=item.approval.approved_at.isoformat(), material=payload,
        )

    def proposed_dispatch(self, item: Any) -> dict[str, Any]:
        with self._connect() as connection:
            scope = _dispatch_scope(connection, item)
        return {**scope, "review_token": _review_token(scope)}

    def approve_dispatch(
        self, item_id: str, *, revision: int, review_token: str,
        approver: str, batch_id: str | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """Compare the displayed scope, approve, and snapshot under one writer lock."""
        from brandman.dispatch import Lifecycle, SQLiteDispatchStore, validate_payload
        from brandman.distribution_governance import assert_package_dispatch_governance
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM dispatch_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(item_id)
            item = SQLiteDispatchStore._from_row(row)
            if item.status is not Lifecycle.AWAITING_APPROVAL:
                raise ValueError(f"cannot approve {item.status}")
            if item.revision != revision:
                raise ValueError(f"expected revision {item.revision}, got {revision}")
            assert_package_dispatch_governance(connection, item_id, revision=revision)
            validation = validate_payload(item.connector, item.payload)
            if not validation.valid:
                raise ValueError("dispatch is not approval-ready: " + "; ".join(validation.errors))
            scope = _dispatch_scope(connection, item)
            if not review_token or review_token != _review_token(scope):
                raise ValueError("displayed approval scope no longer matches the current action")
            timestamp = _now()
            connection.execute(
                """UPDATE dispatch_items SET status='approved',approval_approver=?,
                   approval_revision=?,approval_at=?,approval_batch_id=?,updated_at=? WHERE id=?""",
                (approver, revision, timestamp, batch_id, timestamp, item_id),
            )
            connection.execute(
                """INSERT INTO dispatch_audit(item_id,action,actor,at,revision,detail)
                   VALUES (?,'approved',?,?,?,?)""",
                (item_id, approver, timestamp, revision, batch_id),
            )
            snapshot_id = str(uuid4())
            connection.execute(
                """INSERT INTO approval_snapshots
                   (id,brand_id,account_ref,campaign_id,asset_membership_id,resource_type,
                    resource_id,action_type,destination,intended_schedule,revision,approver,
                    approved_at,material_fingerprint,material_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (snapshot_id, scope["brand_id"], scope["account_ref"], scope["campaign_id"],
                 scope["asset_membership_id"], scope["resource_type"], scope["resource_id"],
                 scope["action_type"], scope["destination"], scope["intended_schedule"],
                 revision, approver, timestamp, scope["material_fingerprint"], "{}", timestamp),
            )
            snapshot = connection.execute(
                "SELECT * FROM approval_snapshots WHERE id=?", (snapshot_id,),
            ).fetchone()
        approved = SQLiteDispatchStore(self.database).get(item_id)
        return approved, self._decode(snapshot)

    def approve_dispatch_batch(
        self, members: list[Mapping[str, Any]], *, approver: str, batch_id: str,
    ) -> list[tuple[Any, dict[str, Any]]]:
        from brandman.dispatch import Lifecycle, SQLiteDispatchStore, validate_payload
        from brandman.distribution_governance import assert_package_dispatch_governance
        if not members or len({str(member["id"]) for member in members}) != len(members):
            raise ValueError("batch item ids must be unique")
        timestamp = _now()
        prepared: list[tuple[Any, dict[str, Any], str]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for member in members:
                item_id, revision = str(member["id"]), int(member["revision"])
                row = connection.execute(
                    "SELECT * FROM dispatch_items WHERE id=?", (item_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(item_id)
                item = SQLiteDispatchStore._from_row(row)
                if item.status is not Lifecycle.AWAITING_APPROVAL or item.revision != revision:
                    raise ValueError("every batch member must await approval at its displayed revision")
                assert_package_dispatch_governance(connection, item_id, revision=revision)
                validation = validate_payload(item.connector, item.payload)
                if not validation.valid:
                    raise ValueError("batch member is not approval-ready: " + "; ".join(validation.errors))
                scope = _dispatch_scope(connection, item)
                if member.get("review_token") != _review_token(scope):
                    raise ValueError("displayed approval scope no longer matches the current action")
                prepared.append((item, scope, str(uuid4())))
            for item, scope, snapshot_id in prepared:
                connection.execute(
                    """UPDATE dispatch_items SET status='approved',approval_approver=?,
                       approval_revision=?,approval_at=?,approval_batch_id=?,updated_at=? WHERE id=?""",
                    (approver, item.revision, timestamp, batch_id, timestamp, item.id),
                )
                connection.execute(
                    """INSERT INTO dispatch_audit(item_id,action,actor,at,revision,detail)
                       VALUES (?,'approved',?,?,?,?)""",
                    (item.id, approver, timestamp, item.revision, batch_id),
                )
                connection.execute(
                    """INSERT INTO approval_snapshots
                       (id,brand_id,account_ref,campaign_id,asset_membership_id,resource_type,
                        resource_id,action_type,destination,intended_schedule,revision,approver,
                        approved_at,material_fingerprint,material_json,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (snapshot_id, scope["brand_id"], scope["account_ref"], scope["campaign_id"],
                     scope["asset_membership_id"], scope["resource_type"], scope["resource_id"],
                     scope["action_type"], scope["destination"], scope["intended_schedule"],
                     item.revision, approver, timestamp, scope["material_fingerprint"], "{}", timestamp),
                )
        dispatch_store = SQLiteDispatchStore(self.database)
        return [
            (dispatch_store.get(item.id), self.get(snapshot_id))
            for item, _scope, snapshot_id in prepared
        ]

    def capture_newsletter(self, issue: Mapping[str, Any]) -> dict[str, Any]:
        revision = issue.get("approved_revision")
        if not issue.get("approval_valid") or revision != issue.get("current_revision"):
            raise ValueError("newsletter approval must cover the exact current revision")
        with self._connect() as connection:
            scope = _newsletter_scope(connection, issue)
        material = _newsletter_evidence_material(scope, issue["content"])
        return self._capture(
            brand_id=str(issue["brand_id"]), account_ref="assisted:beehiiv",
            resource_type="newsletter_issue", resource_id=str(issue["id"]),
            action_type="create_draft", destination="beehiiv:draft",
            intended_schedule=_optional_text(issue.get("scheduled_for")), revision=int(revision),
            approver=str(issue["approved_by"]), approved_at=str(issue["approved_at"]),
            material=material, guideline_version_id=scope.get("guideline_version_id"),
            guideline_fingerprint=scope.get("guideline_fingerprint"),
        )

    def proposed_newsletter(self, issue: Mapping[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            scope = _newsletter_scope(connection, issue)
        return {**scope, "review_token": _review_token(scope)}

    def approve_newsletter(
        self, issue_id: str, *, revision: int, review_token: str, approver: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from brandman.editorial import (
            EditorialStore, editorial_completeness_blockers,
            source_provenance_blockers, volatile_claim_blockers,
        )
        from brandman.brand_guidelines import BrandGuidelineStore
        guideline_store = BrandGuidelineStore(self.database)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            issue = connection.execute(
                "SELECT * FROM newsletter_issues WHERE id=?", (issue_id,),
            ).fetchone()
            if issue is None:
                raise KeyError(issue_id)
            if issue["lifecycle"] != "fact_checked":
                raise ValueError("only a fact_checked issue may be approved")
            if issue["current_revision"] != revision:
                raise ValueError("approval revision does not match the current revision")
            revision_row = connection.execute(
                "SELECT * FROM newsletter_revisions WHERE issue_id=? AND revision=?",
                (issue_id, revision),
            ).fetchone()
            content = _newsletter_material(revision_row)
            blockers = editorial_completeness_blockers(content)
            blockers.extend(source_provenance_blockers(
                content["claims"], content["source_provenance"],
            ))
            blockers.extend(volatile_claim_blockers(content["claims"]))
            policy = guideline_store.evaluate_newsletter(
                brand_id=issue["brand_id"], issue_id=issue_id,
                revision=revision, content=content,
            )
            blockers.extend(item["message"] for item in policy["blockers"])
            if blockers:
                raise ValueError("newsletter is not approval-ready: " + "; ".join(blockers))
            fact_check = connection.execute(
                """SELECT passed,guideline_version_id,guideline_fingerprint
                   FROM newsletter_fact_checks WHERE issue_id=? AND revision=?""",
                (issue_id, revision),
            ).fetchone()
            if fact_check is None or not fact_check["passed"]:
                raise ValueError("the current revision lacks a governed fact-check record")
            guideline = policy.get("guideline")
            if guideline and (
                fact_check["guideline_version_id"] != guideline["version_id"]
                or fact_check["guideline_fingerprint"] != guideline["content_fingerprint"]
            ):
                raise ValueError("the fact check predates the active brand guideline version")
            proposed = {**dict(issue), "content": content}
            scope = _newsletter_scope(connection, proposed)
            if not review_token or review_token != _review_token(scope):
                raise ValueError("displayed approval scope no longer matches the current action")
            timestamp, snapshot_id = _now(), str(uuid4())
            connection.execute(
                """UPDATE newsletter_issues SET lifecycle='approved',approved_revision=?,
                   approved_by=?,approved_at=?,updated_at=? WHERE id=?""",
                (revision, approver, timestamp, timestamp, issue_id),
            )
            connection.execute(
                """INSERT INTO approval_snapshots
                   (id,brand_id,account_ref,campaign_id,asset_membership_id,resource_type,
                    resource_id,action_type,destination,intended_schedule,revision,approver,
                    approved_at,material_fingerprint,material_json,created_at,
                    guideline_version_id,guideline_fingerprint)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (snapshot_id, scope["brand_id"], scope["account_ref"], scope["campaign_id"],
                 scope["asset_membership_id"], scope["resource_type"], scope["resource_id"],
                 scope["action_type"], scope["destination"], scope["intended_schedule"],
                 revision, approver, timestamp, scope["material_fingerprint"], "{}", timestamp,
                 scope.get("guideline_version_id"), scope.get("guideline_fingerprint")),
            )
            snapshot = connection.execute(
                "SELECT * FROM approval_snapshots WHERE id=?", (snapshot_id,),
            ).fetchone()
        return EditorialStore(self.database).get_issue(issue_id), self._decode(snapshot)

    def verify_dispatch_review(self, item: Any, review_token: str) -> None:
        if not review_token or review_token != self.proposed_dispatch(item)["review_token"]:
            raise ValueError("displayed approval scope no longer matches the current action")

    def verify_newsletter_review(self, issue: Mapping[str, Any], review_token: str) -> None:
        if not review_token or review_token != self.proposed_newsletter(issue)["review_token"]:
            raise ValueError("displayed approval scope no longer matches the current action")

    def _capture(self, **values: Any) -> dict[str, Any]:
        material = values.pop("material")
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        fingerprint = "sha256:" + sha256(encoded.encode()).hexdigest()
        snapshot_id = str(uuid4()); created_at = _now()
        with self._connect() as connection:
            campaign_id, membership_id = _campaign_membership(
                connection, values["resource_type"], values["resource_id"],
            )
            connection.execute(
                """INSERT OR IGNORE INTO approval_snapshots
                   (id,brand_id,account_ref,campaign_id,asset_membership_id,
                    resource_type,resource_id,action_type,destination,
                    intended_schedule,revision,approver,approved_at,material_fingerprint,
                    material_json,created_at,guideline_version_id,guideline_fingerprint)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (snapshot_id, values["brand_id"], values["account_ref"], campaign_id, membership_id,
                 values["resource_type"],
                 values["resource_id"], values["action_type"], values["destination"],
                 values["intended_schedule"], values["revision"], values["approver"],
                 values["approved_at"], fingerprint, "{}", created_at,
                 values.get("guideline_version_id"), values.get("guideline_fingerprint")),
            )
            row = connection.execute(
                """SELECT * FROM approval_snapshots WHERE resource_type=? AND resource_id=?
                   AND revision=? AND material_fingerprint=? AND approved_at=?
                   AND campaign_id IS ? AND asset_membership_id IS ?""",
                (values["resource_type"], values["resource_id"], values["revision"], fingerprint,
                 values["approved_at"], campaign_id, membership_id),
            ).fetchone()
        return self._decode(row)

    def invalidate_resource(self, resource_id: str, *, actor: str, reason: str) -> int:
        if not actor.strip() or not reason.strip():
            raise ValueError("invalidation actor and reason are required")
        timestamp = _now()
        with self._connect() as connection:
            snapshots = connection.execute(
                """SELECT id FROM approval_snapshots WHERE resource_id=? AND id NOT IN
                   (SELECT snapshot_id FROM approval_snapshot_invalidations)""", (resource_id,),
            ).fetchall()
            for snapshot in snapshots:
                connection.execute(
                    """INSERT INTO approval_snapshot_invalidations
                       (snapshot_id,actor,reason,invalidated_at) VALUES (?,?,?,?)""",
                    (snapshot["id"], actor, reason, timestamp),
                )
        return len(snapshots)

    def list(self, brand_id: str, *, active_only: bool = False) -> list[dict[str, Any]]:
        self.reconcile_invalidations()
        query = """SELECT s.*,i.actor AS invalidated_by,i.reason AS invalidation_reason,
                          i.invalidated_at FROM approval_snapshots s
                   LEFT JOIN approval_snapshot_invalidations i ON i.snapshot_id=s.id
                   WHERE s.brand_id=?"""
        if active_only:
            query += " AND i.snapshot_id IS NULL"
        with self._connect() as connection:
            rows = connection.execute(query + " ORDER BY s.approved_at DESC,s.id", (brand_id,)).fetchall()
        return [self._decode(row) for row in rows]

    def get(self, snapshot_id: str) -> dict[str, Any]:
        self.reconcile_invalidations()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT s.*,i.actor AS invalidated_by,i.reason AS invalidation_reason,
                          i.invalidated_at FROM approval_snapshots s
                   LEFT JOIN approval_snapshot_invalidations i ON i.snapshot_id=s.id WHERE s.id=?""",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            raise KeyError("approval snapshot not found")
        return self._decode(row)

    def backfill_current(self) -> int:
        """Migrate current governed approvals without inventing missing facts."""
        count = 0
        with self._connect() as connection:
            tables = {row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            dispatch_rows = connection.execute(
                """SELECT * FROM dispatch_items WHERE approval_approver IS NOT NULL
                   AND approval_revision=revision"""
            ).fetchall() if "dispatch_items" in tables else []
            issue_ids = [row["id"] for row in connection.execute(
                """SELECT id FROM newsletter_issues WHERE approved_by IS NOT NULL
                   AND approved_revision=current_revision"""
            )] if "newsletter_issues" in tables else []
        # Callers capture rich domain objects during normal operation. Existing
        # rows are migrated by the application lifespan, after those stores bind.
        for row in dispatch_rows:
            from brandman.dispatch import SQLiteDispatchStore
            try:
                self.capture_dispatch(SQLiteDispatchStore(self.database).get(row["id"])); count += 1
            except (KeyError, ValueError):
                pass
        if issue_ids:
            from brandman.editorial import EditorialStore
            editorial = EditorialStore(self.database)
            for issue_id in issue_ids:
                try:
                    self.capture_newsletter(editorial.get_issue(issue_id)); count += 1
                except (KeyError, ValueError):
                    pass
        return count

    def reconcile_invalidations(self) -> int:
        """Invalidate evidence whose canonical material or approval no longer matches.

        This closes the crash window between a canonical edit and the best-effort
        invalidation written by an HTTP handler.  It never mutates evidence; it
        appends a system invalidation to the immutable ledger.
        """
        with self._connect() as connection:
            tables = {row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            rows = connection.execute(
                """SELECT s.* FROM approval_snapshots s
                   LEFT JOIN approval_snapshot_invalidations i ON i.snapshot_id=s.id
                   WHERE i.snapshot_id IS NULL"""
            ).fetchall()
            invalid = [row["id"] for row in rows if not self._canonical_matches(connection, row, tables)]
            timestamp = _now()
            for snapshot_id in invalid:
                connection.execute(
                    """INSERT OR IGNORE INTO approval_snapshot_invalidations
                       (snapshot_id,actor,reason,invalidated_at) VALUES (?,?,?,?)""",
                    (snapshot_id, "approval-reconciler", "canonical material or approval changed", timestamp),
                )
        return len(invalid)

    @staticmethod
    def _canonical_matches(
        connection: sqlite3.Connection, snapshot: sqlite3.Row, tables: set[str],
    ) -> bool:
        resource_type = snapshot["resource_type"]
        campaign_id, membership_id = _campaign_membership(
            connection, resource_type, snapshot["resource_id"],
        )
        if (
            campaign_id != snapshot["campaign_id"]
            or membership_id != snapshot["asset_membership_id"]
        ):
            return False
        if resource_type in {"dispatch_item", "engagement_action"}:
            if "dispatch_items" not in tables:
                return False
            row = connection.execute(
                """SELECT payload,revision,approval_revision,approval_approver,approval_at,status
                   FROM dispatch_items WHERE id=?""", (snapshot["resource_id"],),
            ).fetchone()
            if row is None or row["status"] in {"draft", "awaiting_approval", "rejected", "cancelled"}:
                return False
            try:
                from brandman.distribution_governance import assert_package_dispatch_governance
                assert_package_dispatch_governance(
                    connection, snapshot["resource_id"], revision=snapshot["revision"],
                )
            except ValueError:
                return False
            material = json.loads(row["payload"])
            return (
                row["revision"] == snapshot["revision"]
                and row["approval_revision"] == snapshot["revision"]
                and row["approval_approver"] == snapshot["approver"]
                and row["approval_at"] == snapshot["approved_at"]
                and _fingerprint(material) == snapshot["material_fingerprint"]
            )
        if resource_type == "newsletter_issue":
            if "newsletter_issues" not in tables or "newsletter_revisions" not in tables:
                return False
            issue = connection.execute(
                """SELECT current_revision,approved_revision,approved_by,approved_at,lifecycle
                   FROM newsletter_issues WHERE id=?""", (snapshot["resource_id"],),
            ).fetchone()
            revision = connection.execute(
                """SELECT * FROM newsletter_revisions
                   WHERE issue_id=? AND revision=?""",
                (snapshot["resource_id"], snapshot["revision"]),
            ).fetchone()
            return bool(
                issue is not None and revision is not None
                and issue["lifecycle"] not in {"draft", "in_review", "rejected"}
                and issue["current_revision"] == snapshot["revision"]
                and issue["approved_revision"] == snapshot["revision"]
                and issue["approved_by"] == snapshot["approver"]
                and issue["approved_at"] == snapshot["approved_at"]
                and _newsletter_snapshot_matches(connection, snapshot, _newsletter_material(revision))
            )
        return False

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row); result.pop("material_json", None)
        result["material_reference"] = {
            "resource_type": result["resource_type"], "resource_id": result["resource_id"],
            "revision": result["revision"],
        }
        result["active"] = result.get("invalidated_at") is None
        return result


def _dispatch_action(payload: Mapping[str, Any]) -> tuple[str, str]:
    if payload.get("reply_to_post_id"):
        return "reply", f"x:post:{payload['reply_to_post_id']}"
    if payload.get("target_post_id"):
        return "like", f"x:post:{payload['target_post_id']}"
    if payload.get("target_user_id"):
        return "follow", f"x:user:{payload['target_user_id']}"
    return "post", "x:public"


def _optional_text(value: Any) -> str | None:
    return None if value is None or not str(value).strip() else str(value)


def _fingerprint(material: Mapping[str, Any]) -> str:
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return "sha256:" + sha256(encoded.encode()).hexdigest()


def _review_token(scope: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(scope), sort_keys=True, separators=(",", ":"))
    return "review-v1:" + sha256(encoded.encode()).hexdigest()


def _dispatch_scope(connection: sqlite3.Connection, item: Any) -> dict[str, Any]:
    payload = dict(item.payload)
    action, destination = _dispatch_action(payload)
    resource_type = "engagement_action" if action in {"reply", "like", "follow"} else "dispatch_item"
    campaign_id, membership_id = _campaign_membership(connection, resource_type, item.id)
    return {
        "brand_id": str(item.brand_id or "unscoped"),
        "account_ref": str(payload.get("connector_account_id") or f"assisted:{item.connector}"),
        "resource_type": resource_type, "resource_id": item.id,
        "action_type": action, "destination": str(payload.get("destination") or destination),
        "intended_schedule": _optional_text(payload.get("scheduled_for") or payload.get("intended_schedule")),
        "revision": item.revision, "campaign_id": campaign_id,
        "asset_membership_id": membership_id, "material_fingerprint": _fingerprint(payload),
    }


def _newsletter_scope(
    connection: sqlite3.Connection, issue: Mapping[str, Any],
) -> dict[str, Any]:
    campaign_id, membership_id = _campaign_membership(
        connection, "newsletter_issue", str(issue["id"]),
    )
    guideline_ref = _active_guideline_ref(
        connection, str(issue["brand_id"]), "newsletter", "beehiiv",
    )
    material = _newsletter_evidence_material(
        {"guideline": guideline_ref}, issue["content"],
    )
    return {
        "brand_id": str(issue["brand_id"]), "account_ref": "assisted:beehiiv",
        "resource_type": "newsletter_issue", "resource_id": str(issue["id"]),
        "action_type": "create_draft", "destination": "beehiiv:draft",
        "intended_schedule": _optional_text(issue.get("scheduled_for")),
        "revision": int(issue["current_revision"]), "campaign_id": campaign_id,
        "asset_membership_id": membership_id,
        "material_fingerprint": _fingerprint(material),
        "guideline": guideline_ref,
        "guideline_version_id": guideline_ref["version_id"] if guideline_ref else None,
        "guideline_fingerprint": guideline_ref["content_fingerprint"] if guideline_ref else None,
    }


_NEWSLETTER_REVIEW_FIELDS = (
    "editorial_thesis", "target_reader", "intended_outcome", "working_title",
    "final_title", "subject", "preview_text", "sections", "cta", "seo",
    "content_basis", "delivery_metadata", "claims", "source_provenance",
)


def _reviewable_newsletter_material(content: Mapping[str, Any]) -> dict[str, Any]:
    """Match the immutable editorial fact-check material, excluding row metadata."""

    arrays = {"sections", "claims", "source_provenance"}
    objects = {"cta", "seo", "content_basis", "delivery_metadata"}
    return {
        key: content.get(key, [] if key in arrays else {} if key in objects else "")
        for key in _NEWSLETTER_REVIEW_FIELDS
    }


def _newsletter_material(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for key in ("sections", "cta", "seo", "content_basis", "delivery_metadata", "claims", "source_provenance"):
        result[key] = json.loads(result[key])
    return result


def _newsletter_evidence_material(
    scope: Mapping[str, Any], content: Mapping[str, Any],
) -> dict[str, Any]:
    material = _reviewable_newsletter_material(content)
    guideline = scope.get("guideline")
    return {"content": material, "guideline": guideline} if guideline else material


def _newsletter_snapshot_matches(
    connection: sqlite3.Connection, snapshot: sqlite3.Row, content: Mapping[str, Any],
) -> bool:
    issue = connection.execute(
        "SELECT id,brand_id,current_revision FROM newsletter_issues WHERE id=?",
        (snapshot["resource_id"],),
    ).fetchone()
    if issue is None:
        return False
    proposed = {"id": issue["id"], "brand_id": issue["brand_id"],
                "current_revision": issue["current_revision"], "content": content}
    scope = _newsletter_scope(connection, proposed)
    return (
        scope.get("guideline_version_id") == snapshot["guideline_version_id"]
        and scope.get("guideline_fingerprint") == snapshot["guideline_fingerprint"]
        and scope["material_fingerprint"] == snapshot["material_fingerprint"]
    )


def _active_guideline_ref(
    connection: sqlite3.Connection, brand_id: str, content_type: str, channel: str,
) -> dict[str, Any] | None:
    tables = {row["name"] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not {"brand_guidelines", "brand_guideline_versions"} <= tables:
        return None
    for content_scope, channel_scope in (
        (content_type, channel), (content_type, "*"), ("*", channel), ("*", "*"),
    ):
        row = connection.execute(
            """SELECT g.id,g.name,g.content_type,g.channel,v.id AS version_id,v.version,
                      v.content_fingerprint
               FROM brand_guidelines g JOIN brand_guideline_versions v
                 ON v.id=g.active_version_id
               WHERE g.brand_id=? AND g.content_type=? AND g.channel=?
                 AND g.status='active'""",
            (brand_id, content_scope, channel_scope),
        ).fetchone()
        if row is not None:
            return dict(row)
    return None


def _campaign_membership(
    connection: sqlite3.Connection, resource_type: str, resource_id: str,
) -> tuple[str | None, str | None]:
    tables = {row["name"] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "campaign_asset_memberships" not in tables:
        return None, None
    asset_type, asset_id = resource_type, resource_id
    if resource_type in {"dispatch_item", "engagement_action"}:
        asset_type = "x_post"
        asset_id = None
        if "newsletter_distribution_artifacts" in tables:
            artifact = connection.execute(
                """SELECT post_id FROM newsletter_distribution_artifacts
                   WHERE dispatch_item_id=?""", (resource_id,),
            ).fetchone()
            if artifact:
                asset_id = artifact["post_id"]
        if asset_id is None and "dispatch_items" in tables:
            dispatch = connection.execute(
                "SELECT canonical_post_id FROM dispatch_items WHERE id=?", (resource_id,),
            ).fetchone()
            if dispatch:
                asset_id = dispatch["canonical_post_id"]
        if not asset_id:
            return None, None
    row = connection.execute(
        """SELECT id,campaign_id FROM campaign_asset_memberships
           WHERE asset_type=? AND asset_id=? AND active=1
           ORDER BY attribution_primary DESC,updated_at DESC,id LIMIT 1""",
        (asset_type, asset_id),
    ).fetchone()
    return (row["campaign_id"], row["id"]) if row else (None, None)


def invalidate_membership_approval(
    connection: sqlite3.Connection, membership: Mapping[str, Any], *,
    actor: str, reason: str, timestamp: str,
) -> list[str]:
    """Atomically withdraw approval when governed campaign context changes."""
    resource_ids: list[str] = []
    asset_type, asset_id = membership["asset_type"], membership["asset_id"]
    if asset_type == "newsletter_issue":
        resource_ids.append(asset_id)
        connection.execute(
            """UPDATE newsletter_issues SET lifecycle='fact_checked',approved_revision=NULL,
               approved_by=NULL,approved_at=NULL,updated_at=?
               WHERE id=? AND lifecycle='approved'""", (timestamp, asset_id),
        )
    elif asset_type == "x_post":
        if _table_exists(connection, "newsletter_distribution_artifacts"):
            resource_ids.extend(row["dispatch_item_id"] for row in connection.execute(
                """SELECT dispatch_item_id FROM newsletter_distribution_artifacts
                   WHERE post_id=?""", (asset_id,),
            ))
        if _table_exists(connection, "dispatch_items"):
            resource_ids.extend(row["id"] for row in connection.execute(
                "SELECT id FROM dispatch_items WHERE canonical_post_id=?", (asset_id,),
            ))
        for resource_id in set(resource_ids):
            connection.execute(
                """UPDATE dispatch_items SET status='awaiting_approval',approval_approver=NULL,
                   approval_revision=NULL,approval_at=NULL,approval_batch_id=NULL,
                   idempotency_key=NULL,dispatch_claim=NULL,updated_at=?
                   WHERE id=? AND status IN ('approved','queued') AND external_id IS NULL""",
                (timestamp, resource_id),
            )
    resource_ids = list(dict.fromkeys(resource_ids))
    if resource_ids and _table_exists(connection, "approval_snapshots"):
        placeholders = ",".join("?" for _ in resource_ids)
        snapshots = connection.execute(
            f"""SELECT id FROM approval_snapshots WHERE
                 (asset_membership_id=? OR resource_id IN ({placeholders})) AND id NOT IN
                 (SELECT snapshot_id FROM approval_snapshot_invalidations)""",
            (membership["id"], *resource_ids),
        ).fetchall()
        connection.executemany(
            """INSERT INTO approval_snapshot_invalidations
               (snapshot_id,actor,reason,invalidated_at) VALUES (?,?,?,?)""",
            [(row["id"], actor, reason, timestamp) for row in snapshots],
        )
        if _table_exists(connection, "execution_tasks"):
            tasks = connection.execute(
                f"""SELECT id FROM execution_tasks
                   WHERE (asset_membership_id=? OR resource_id IN ({placeholders}))
                     AND status IN ('pending','claimed')""",
                (membership["id"], *resource_ids),
            ).fetchall()
            connection.execute(
                f"""UPDATE execution_tasks SET status='stale',claimed_by=NULL,claimed_at=NULL,
                   claim_expires_at=NULL,claim_token_hash=NULL,updated_at=?
                   WHERE (asset_membership_id=? OR resource_id IN ({placeholders}))
                     AND status IN ('pending','claimed')""",
                (timestamp, membership["id"], *resource_ids),
            )
            if tasks and _table_exists(connection, "execution_task_audit"):
                connection.executemany(
                    """INSERT INTO execution_task_audit
                       (task_id,action,actor,at,detail_json) VALUES (?,?,?,?,?)""",
                    [(row["id"], "invalidated", actor, timestamp,
                      '{"reason":"campaign membership changed; fresh approval required"}')
                     for row in tasks],
                )
    return resource_ids


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["ApprovalSnapshotStore", "invalidate_membership_approval"]
