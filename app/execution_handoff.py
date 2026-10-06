"""Governed browser/MCP-assisted execution handoffs for approved content."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from app.dispatch import GovernedDispatcher, Lifecycle
from app.editorial import EditorialStore
from app.approval_snapshots import ApprovalSnapshotStore
from app.operational_feedback import report_successful_workaround
from app.distribution_governance import (
    PackageDispatchGovernanceError, assert_package_dispatch_governance,
)


class ExecutionHandoffError(ValueError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_tasks (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL, provider TEXT NOT NULL,
  resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, revision INTEGER NOT NULL,
  campaign_id TEXT, asset_membership_id TEXT, connector_account_id TEXT,
  idempotency_key TEXT NOT NULL UNIQUE, execution_payload TEXT NOT NULL,
  material_fingerprint TEXT NOT NULL, status TEXT NOT NULL,
  claimed_by TEXT, claimed_at TEXT, claim_expires_at TEXT, claim_token_hash TEXT,
  receipt_external_id TEXT, receipt_external_url TEXT, receipt_status TEXT,
  receipt_content_fingerprint TEXT, receipt_asset_fingerprint TEXT,
  receipt_recorded_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  public_action_confirmed_by TEXT, public_action_confirmed_at TEXT,
  public_action_confirmation_expires_at TEXT,
  public_action_confirmation_fingerprint TEXT,
  external_action_started_by TEXT, external_action_started_at TEXT,
  external_action_started_fingerprint TEXT,
  external_action_snapshot TEXT, external_action_snapshot_fingerprint TEXT,
  receipt_reconciliation_state TEXT,
  UNIQUE(provider,resource_type,resource_id,revision)
);
CREATE INDEX IF NOT EXISTS execution_tasks_brand_status
  ON execution_tasks(brand_id,status,created_at);
CREATE TABLE IF NOT EXISTS execution_task_audit (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
  action TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(task_id) REFERENCES execution_tasks(id)
);
CREATE INDEX IF NOT EXISTS execution_task_audit_task
  ON execution_task_audit(task_id,sequence);
CREATE TABLE IF NOT EXISTS execution_receipt_reservations (
  provider TEXT NOT NULL, external_id TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE,
  external_url TEXT NOT NULL, reserved_at TEXT NOT NULL,
  PRIMARY KEY(provider,external_id),
  FOREIGN KEY(task_id) REFERENCES execution_tasks(id)
);
CREATE TABLE IF NOT EXISTS execution_provider_controls (
  brand_id TEXT NOT NULL, provider TEXT NOT NULL, enabled INTEGER NOT NULL,
  updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(brand_id,provider),
  CHECK(provider IN ('all','beehiiv','x'))
);
CREATE TABLE IF NOT EXISTS execution_provider_control_audit (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, brand_id TEXT NOT NULL,
  provider TEXT NOT NULL, enabled INTEGER NOT NULL,
  actor TEXT NOT NULL, at TEXT NOT NULL
);
"""


class ExecutionHandoffStore:
    def __init__(
        self, database: str | Path, editorial: EditorialStore,
        dispatcher: GovernedDispatcher, *, clock: Callable[[], datetime] | None = None,
        agent_freshness_seconds: int = 900,
    ) -> None:
        self.database = str(database)
        self.editorial = editorial
        self.dispatcher = dispatcher
        self.clock = clock or (lambda: datetime.now(UTC))
        self.agent_freshness_seconds = agent_freshness_seconds
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute(
                "PRAGMA table_info(execution_tasks)"
            )}
            migrations = {
                "campaign_id": "TEXT", "asset_membership_id": "TEXT",
                "connector_account_id": "TEXT",
                "public_action_confirmed_by": "TEXT",
                "public_action_confirmed_at": "TEXT",
                "public_action_confirmation_expires_at": "TEXT",
                "public_action_confirmation_fingerprint": "TEXT",
                "external_action_started_by": "TEXT",
                "external_action_started_at": "TEXT",
                "external_action_started_fingerprint": "TEXT",
                "external_action_snapshot": "TEXT",
                "external_action_snapshot_fingerprint": "TEXT",
                "receipt_reconciliation_state": "TEXT",
                "receipt_content_fingerprint": "TEXT",
                "receipt_asset_fingerprint": "TEXT",
            }
            for column, definition in migrations.items():
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE execution_tasks ADD COLUMN {column} {definition}"
                    )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def ensure_for_brand(self, brand_id: str) -> list[dict[str, Any]]:
        """Create internal tasks for exact approved revisions; never execute them."""
        # This additive initializer also migrates any exact approvals created by
        # older application versions before handoff tasks are considered.
        ApprovalSnapshotStore(self.database)
        with self._connect() as connection:
            issues = connection.execute(
                """SELECT id,current_revision FROM newsletter_issues
                   WHERE brand_id=? AND lifecycle='approved'
                     AND approved_revision=current_revision
                     AND EXISTS (SELECT 1 FROM approval_snapshots s
                       LEFT JOIN approval_snapshot_invalidations i ON i.snapshot_id=s.id
                       WHERE s.resource_id=newsletter_issues.id
                         AND s.revision=newsletter_issues.current_revision
                         AND i.snapshot_id IS NULL) ORDER BY id""", (brand_id,),
            ).fetchall()
            dispatches = connection.execute(
                """SELECT id,revision FROM dispatch_items
                   WHERE brand_id=? AND connector='x' AND status IN ('approved','queued')
                     AND approval_revision=revision AND external_id IS NULL
                     AND EXISTS (SELECT 1 FROM approval_snapshots s
                       LEFT JOIN approval_snapshot_invalidations i ON i.snapshot_id=s.id
                       WHERE s.resource_id=dispatch_items.id
                         AND s.revision=dispatch_items.revision
                         AND i.snapshot_id IS NULL) ORDER BY id""", (brand_id,),
            ).fetchall()
        for issue in issues:
            prepared = self.editorial.prepare_export(issue["id"], connector="beehiiv")
            self._ensure(
                brand_id=brand_id, provider="beehiiv", resource_type="newsletter_issue",
                resource_id=issue["id"], revision=issue["current_revision"],
                payload=prepared["payload"], fingerprint=prepared["payload_fingerprint"],
            )
        for dispatch in dispatches:
            item = self.dispatcher.store.get(dispatch["id"])
            # A handoff is a public-content delivery instruction, never a generic
            # transport for arbitrary dispatch metadata (which might contain a
            # token-like value in a legacy record).
            safe_payload = {
                key: value for key, value in item.payload.items()
                if key in {"body", "text", "reply_to_post_id", "media_ids"}
            }
            self._ensure(
                brand_id=brand_id, provider="x", resource_type="dispatch_item",
                resource_id=item.id, revision=item.revision, payload=safe_payload,
                fingerprint=_fingerprint(item.payload),
                connector_account_id=(
                    str(item.payload["connector_account_id"])
                    if item.payload.get("connector_account_id") else None
                ),
            )
        return self.list(brand_id)

    def _ensure(
        self, *, brand_id: str, provider: str, resource_type: str,
        resource_id: str, revision: int, payload: Mapping[str, Any], fingerprint: str,
        connector_account_id: str | None = None,
    ) -> dict[str, Any]:
        timestamp = self._now()
        task_id = str(uuid4())
        key = f"browser-handoff:{provider}:{resource_id}:r{revision}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if provider == "x":
                assert_package_dispatch_governance(
                    connection, resource_id, revision=revision,
                )
            eligible_accounts = self._eligible_assisted_accounts(connection, brand_id, provider)
            if connector_account_id is not None:
                if connector_account_id not in {account["id"] for account in eligible_accounts}:
                    raise ExecutionHandoffError(
                        "explicit destination connector account is not an eligible same-brand assisted write account"
                    )
                destination_account_id = connector_account_id
            else:
                destination_account_id = (
                    eligible_accounts[0]["id"] if len(eligible_accounts) == 1 else None
                )
            approval = connection.execute(
                """SELECT s.campaign_id,s.asset_membership_id,s.resource_type
                   FROM approval_snapshots s
                   LEFT JOIN approval_snapshot_invalidations i ON i.snapshot_id=s.id
                   WHERE s.brand_id=? AND s.resource_id=? AND s.revision=?
                     AND i.snapshot_id IS NULL
                   ORDER BY s.approved_at DESC,s.id LIMIT 1""",
                (brand_id, resource_id, revision),
            ).fetchone()
            if approval is None:
                raise ExecutionHandoffError("active immutable approval evidence is required")
            existing = connection.execute(
                "SELECT id,connector_account_id FROM execution_tasks WHERE idempotency_key=?", (key,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO execution_tasks
                       (id,brand_id,provider,resource_type,resource_id,revision,idempotency_key,
                        campaign_id,asset_membership_id,connector_account_id,
                        execution_payload,material_fingerprint,
                        status,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                    (task_id, brand_id, provider, approval["resource_type"], resource_id, revision, key,
                     approval["campaign_id"], approval["asset_membership_id"],
                     destination_account_id,
                     json.dumps(dict(payload), sort_keys=True), fingerprint, timestamp, timestamp),
                )
                self._audit(connection, task_id, "created", "brand-os", {
                    "provider": provider, "resource_id": resource_id, "revision": revision,
                })
            else:
                task_id = existing["id"]
                if (
                    destination_account_id is not None
                    and existing["connector_account_id"] not in {None, destination_account_id}
                ):
                    raise ExecutionHandoffError(
                        "execution task is already bound to a different destination account"
                    )
                connection.execute(
                    """UPDATE execution_tasks SET campaign_id=?,asset_membership_id=?,updated_at=?
                       WHERE id=? AND campaign_id IS NULL AND asset_membership_id IS NULL""",
                    (approval["campaign_id"], approval["asset_membership_id"], timestamp, task_id),
                )
                if destination_account_id is not None:
                    connection.execute(
                        """UPDATE execution_tasks SET connector_account_id=?,updated_at=?
                           WHERE id=? AND connector_account_id IS NULL AND status='pending'""",
                        (destination_account_id, timestamp, task_id),
                    )
            row = connection.execute("SELECT * FROM execution_tasks WHERE id=?", (task_id,)).fetchone()
        return self._decode(row)

    def list(self, brand_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
        self.recover_expired()
        self._reconcile_started_drift(brand_id=brand_id)
        query = "SELECT * FROM execution_tasks WHERE brand_id=?"
        values: list[Any] = [brand_id]
        if status:
            query += " AND status=?"
            values.append(status)
        with self._connect() as connection:
            rows = connection.execute(query + " ORDER BY created_at,id", values).fetchall()
        return [self._decode(row) for row in rows]

    def operator_view(self, brand_id: str) -> list[dict[str, Any]]:
        """Describe safe human/helper transitions without exposing claim secrets."""
        tasks = self.list(brand_id)
        with self._connect() as connection:
            options = {
                provider: self._eligible_assisted_accounts(connection, brand_id, provider)
                for provider in ("beehiiv", "x")
            }
        return [self._operator_task(task, options.get(task["provider"], [])) for task in tasks]

    def beehiiv_private_draft_manifest(
        self, task_id: str, *, asset_path: str | Path,
        existing_draft_id: str | None = None,
    ) -> dict[str, Any]:
        """Return a provider-neutral, exact-approved browser instruction."""

        from app.beehiiv_assisted_publisher import build_private_draft_manifest

        task = self.get(task_id)
        if task["provider"] != "beehiiv" or task["resource_type"] != "newsletter_issue":
            raise ExecutionHandoffError("private-draft manifest requires a Beehiiv newsletter task")
        reconciling_begun = bool(
            task["status"] == "needs_attention"
            and task.get("external_action_started_at")
            and existing_draft_id
        )
        if task["status"] not in {"pending", "claimed"} and not reconciling_begun:
            raise ExecutionHandoffError("Beehiiv task is not available for private-draft preparation")
        if not task.get("connector_account_id"):
            raise ExecutionHandoffError("Beehiiv destination account must be bound before preparation")
        if not self._current_resource_valid(task_id):
            raise ExecutionHandoffError("Beehiiv task no longer matches exact approval evidence")
        payload = task["execution_payload"]
        prepared = {
            "payload": payload,
            "payload_fingerprint": task["material_fingerprint"],
            "idempotency_key": (
                f"newsletter-export:beehiiv:{task['resource_id']}:r{task['revision']}"
            ),
        }
        manifest = build_private_draft_manifest(
            prepared, asset_path=asset_path, existing_draft_id=existing_draft_id,
        )
        manifest["execution_task_id"] = task_id
        manifest["connector_account_id"] = task["connector_account_id"]
        return manifest

    def _operator_task(
        self, task: Mapping[str, Any], destination_options: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result = dict(task)
        status = str(task.get("status") or "")
        begun = bool(task.get("external_action_started_at"))
        confirmation_current = bool(
            task.get("provider") == "x"
            and task.get("public_action_confirmation_expires_at")
            and self._parse(str(task["public_action_confirmation_expires_at"])) > self._clock()
        )
        if status == "pending" and not task.get("connector_account_id"):
            code = "bind_destination_account"
            text = "Select exactly one destination account. No helper can claim this action until it is bound."
        elif status == "pending":
            code = "claim_in_execution_helper"
            text = "Hand this exact approved action to a fresh browser or MCP helper, then claim it there."
        elif status == "claimed" and task.get("provider") == "x" and not confirmation_current:
            code = "confirm_exact_x_action"
            text = "Confirm this exact X revision for five minutes; confirmation does not post it."
        elif status == "claimed" and not begun:
            code = "begin_external_action"
            text = "In the claiming helper, compare the exact material, mark the action begun, then perform it once."
        elif status in {"claimed", "needs_attention"} and begun:
            code = "record_provider_receipt"
            prefix = "Do not repeat the provider action. " if status == "needs_attention" else ""
            text = prefix + "Verify the existing provider result and record its ID and URL with the original claim."
        elif status == "completed":
            code = "receipt_complete"
            text = "Provider receipt recorded. Continue to read-only measurement."
        else:
            code = "refresh_exact_approval"
            text = "This handoff is stale. Create and approve a current revision before any provider action."
        result.update({
            "destination_options": destination_options,
            "destination_binding_required": bool(
                status == "pending" and not task.get("connector_account_id")
            ),
            "action_boundary": "completed" if status == "completed" else (
                "provider_action_started" if begun else "not_started"
            ),
            "confirmation_current": confirmation_current,
            "receipt_requirements": {
                "status": "draft" if task.get("provider") == "beehiiv" else "posted",
                "external_id": "Beehiiv post ID" if task.get("provider") == "beehiiv" else "X post ID",
                "external_url": "Beehiiv editor/preview URL" if task.get("provider") == "beehiiv" else "Canonical x.com status URL",
            },
            "operator_next_action": {"code": code, "text": text},
        })
        return result

    def controls(self, brand_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            configured = {
                row["provider"]: dict(row) for row in connection.execute(
                    "SELECT * FROM execution_provider_controls WHERE brand_id=?", (brand_id,),
                )
            }
        global_enabled = bool(configured.get("all", {}).get("enabled", 1))
        result = []
        for provider in ("all", "beehiiv", "x"):
            row = configured.get(provider)
            enabled = bool(row["enabled"]) if row else True
            result.append({
                "brand_id": brand_id, "provider": provider, "enabled": enabled,
                "effective_enabled": enabled and (provider == "all" or global_enabled),
                "updated_by": row["updated_by"] if row else None,
                "updated_at": row["updated_at"] if row else None,
            })
        return result

    def set_control(
        self, brand_id: str, provider: str, *, enabled: bool, actor: str,
    ) -> dict[str, Any]:
        if provider not in {"all", "beehiiv", "x"}:
            raise ExecutionHandoffError("provider control must be all, beehiiv, or x")
        if not _SAFE_IDENTIFIER.fullmatch(actor):
            raise ExecutionHandoffError("control actor must be a safe operator identifier")
        timestamp = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO execution_provider_controls
                   (brand_id,provider,enabled,updated_by,updated_at) VALUES (?,?,?,?,?)
                   ON CONFLICT(brand_id,provider) DO UPDATE SET enabled=excluded.enabled,
                     updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                (brand_id, provider, int(enabled), actor, timestamp),
            )
            connection.execute(
                """INSERT INTO execution_provider_control_audit
                   (brand_id,provider,enabled,actor,at) VALUES (?,?,?,?,?)""",
                (brand_id, provider, int(enabled), actor, timestamp),
            )
            if not enabled and provider in {"all", "beehiiv"}:
                pull_table = connection.execute(
                    """SELECT 1 FROM sqlite_master WHERE type='table'
                       AND name='beehiiv_assisted_pull_tasks'"""
                ).fetchone()
                audit_table = connection.execute(
                    """SELECT 1 FROM sqlite_master WHERE type='table'
                       AND name='beehiiv_assisted_pull_audit'"""
                ).fetchone()
                if pull_table is not None:
                    invalidated = connection.execute(
                        """SELECT id,claimed_by FROM beehiiv_assisted_pull_tasks
                           WHERE brand_id=? AND status IN ('pending','claimed')""",
                        (brand_id,),
                    ).fetchall()
                    connection.execute(
                        """UPDATE beehiiv_assisted_pull_tasks SET status='failed',
                           claimed_by=NULL,claimed_at=NULL,claim_expires_at=NULL,
                           claim_token_hash=NULL,last_failure_code='provider_disabled',
                           updated_at=? WHERE brand_id=? AND status IN ('pending','claimed')""",
                        (timestamp, brand_id),
                    )
                    if audit_table is not None:
                        for task in invalidated:
                            connection.execute(
                                """INSERT INTO beehiiv_assisted_pull_audit
                                   (task_id,action,actor,at,detail_json)
                                   VALUES (?,'invalidated_by_provider_control',?,?,?)""",
                                (task["id"], actor, timestamp, json.dumps({
                                    "provider": provider,
                                    "previous_claimant": task["claimed_by"],
                                }, sort_keys=True)),
                            )
        return next(item for item in self.controls(brand_id) if item["provider"] == provider)

    def control_audit(self, brand_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM execution_provider_control_audit
                   WHERE brand_id=? ORDER BY sequence""", (brand_id,),
            ).fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def get(self, task_id: str) -> dict[str, Any]:
        self.recover_expired()
        self._reconcile_started_drift(task_id=task_id)
        return self._get(task_id, internal=False)

    def _get(self, task_id: str, *, internal: bool) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM execution_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError("execution task not found")
        return self._decode(row, internal=internal)

    def claim(self, task_id: str, *, actor: str, lease_seconds: int = 900) -> dict[str, Any]:
        if not _SAFE_IDENTIFIER.fullmatch(actor):
            raise ExecutionHandoffError("claim actor must be a safe operator identifier")
        if not 60 <= lease_seconds <= 3600:
            raise ExecutionHandoffError("lease_seconds must be between 60 and 3600")
        ApprovalSnapshotStore(self.database).reconcile_invalidations()
        self.recover_expired()
        token = secrets.token_urlsafe(32)
        timestamp = self._now()
        expires = (self._clock() + timedelta(seconds=lease_seconds)).isoformat()
        invalidated = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM execution_tasks WHERE id=?", (task_id,),
            ).fetchone()
            if current is None:
                raise KeyError("execution task not found")
            if current["status"] != "pending":
                raise ExecutionHandoffError("execution task is not available to claim")
            if not self._agent_is_fresh(connection, current["brand_id"], actor):
                raise ExecutionHandoffError(
                    "claim actor must be an enabled brand execution agent with a fresh heartbeat"
                )
            if not self._provider_enabled(
                connection, current["brand_id"], current["provider"],
            ):
                raise ExecutionHandoffError(
                    f"{current['provider']} assisted execution is disabled for this brand"
                )
            if not self._valid_in_connection(connection, current):
                connection.execute(
                    """UPDATE execution_tasks SET status='stale',claimed_by=NULL,claimed_at=NULL,
                       claim_expires_at=NULL,claim_token_hash=NULL,updated_at=? WHERE id=?""",
                    (timestamp, task_id),
                )
                self._audit(connection, task_id, "invalidated", "brand-os", {
                    "reason": "exact approved revision is no longer current",
                })
                invalidated = True
            else:
                self._assert_bound_destination_account(connection, current)
                connection.execute(
                    """UPDATE execution_tasks SET status='claimed',claimed_by=?,claimed_at=?,
                       claim_expires_at=?,claim_token_hash=?,updated_at=?,
                       public_action_confirmed_by=NULL,public_action_confirmed_at=NULL,
                       public_action_confirmation_expires_at=NULL,
                       public_action_confirmation_fingerprint=NULL WHERE id=?""",
                    (actor, timestamp, expires, _token_hash(token), timestamp, task_id),
                )
                self._audit(connection, task_id, "claimed", actor, {"claim_expires_at": expires})
            row = connection.execute("SELECT * FROM execution_tasks WHERE id=?", (task_id,)).fetchone()
        if invalidated:
            raise ExecutionHandoffError("exact approved revision is no longer current")
        return {**self._decode(row), "claim_token": token}

    def confirm_public_action(
        self, task_id: str, *, actor: str, expected_revision: int,
        expected_material_fingerprint: str, confirmation_phrase: str,
        validity_seconds: int = 300,
    ) -> dict[str, Any]:
        """Record a separate, short-lived human confirmation for a claimed X post.

        This changes no provider state and intentionally is not exposed as an MCP
        tool.  The confirmation is bound to the exact claimed revision and
        material fingerprint, and cannot outlive the claim lease.
        """
        if not _SAFE_IDENTIFIER.fullmatch(actor):
            raise ExecutionHandoffError("confirmation actor must be a safe operator identifier")
        if confirmation_phrase != PUBLIC_X_CONFIRMATION_PHRASE:
            raise ExecutionHandoffError(
                f"confirmation_phrase must exactly equal {PUBLIC_X_CONFIRMATION_PHRASE!r}"
            )
        if not 30 <= validity_seconds <= 300:
            raise ExecutionHandoffError("validity_seconds must be between 30 and 300")
        ApprovalSnapshotStore(self.database).reconcile_invalidations()
        self.recover_expired()
        timestamp = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM execution_tasks WHERE id=?", (task_id,),
            ).fetchone()
            if current is None:
                raise KeyError("execution task not found")
            if current["provider"] != "x":
                raise ExecutionHandoffError(
                    "action-time confirmation applies only to public X handoffs"
                )
            if current["status"] != "claimed":
                raise ExecutionHandoffError(
                    "a currently claimed X handoff is required for confirmation"
                )
            if (
                int(expected_revision) != int(current["revision"])
                or expected_material_fingerprint != current["material_fingerprint"]
            ):
                raise ExecutionHandoffError(
                    "confirmation must match the exact claimed revision and material fingerprint"
                )
            if not self._valid_in_connection(connection, current):
                connection.execute(
                    """UPDATE execution_tasks SET status='stale',claimed_by=NULL,
                       claimed_at=NULL,claim_expires_at=NULL,claim_token_hash=NULL,
                       public_action_confirmed_by=NULL,public_action_confirmed_at=NULL,
                       public_action_confirmation_expires_at=NULL,
                       public_action_confirmation_fingerprint=NULL,updated_at=? WHERE id=?""",
                    (timestamp.isoformat(), task_id),
                )
                self._audit(connection, task_id, "invalidated", "brand-os", {
                    "reason": "exact approved revision is no longer current",
                })
                invalidated = True
            else:
                invalidated = False
                claim_expires = self._parse(current["claim_expires_at"])
                expires = min(
                    claim_expires, timestamp + timedelta(seconds=validity_seconds),
                )
                if expires <= timestamp:
                    raise ExecutionHandoffError("execution claim expired")
                connection.execute(
                    """UPDATE execution_tasks SET public_action_confirmed_by=?,
                       public_action_confirmed_at=?,public_action_confirmation_expires_at=?,
                       public_action_confirmation_fingerprint=?,updated_at=? WHERE id=?""",
                    (actor, timestamp.isoformat(), expires.isoformat(),
                     expected_material_fingerprint, timestamp.isoformat(), task_id),
                )
                self._audit(connection, task_id, "public_action_confirmed", actor, {
                    "revision": int(expected_revision),
                    "material_fingerprint": expected_material_fingerprint,
                    "confirmation_expires_at": expires.isoformat(),
                })
            row = connection.execute(
                "SELECT * FROM execution_tasks WHERE id=?", (task_id,),
            ).fetchone()
        if invalidated:
            raise ExecutionHandoffError("exact approved revision is no longer current")
        return self._decode(row)

    def bind_destination_account(
        self, task_id: str, *, connector_account_id: str, actor: str,
    ) -> dict[str, Any]:
        """Bind one pending handoff to an exact same-brand assisted write account."""
        if not _SAFE_IDENTIFIER.fullmatch(actor):
            raise ExecutionHandoffError("binding actor must be a safe operator identifier")
        timestamp = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT * FROM execution_tasks WHERE id=?", (task_id,),
            ).fetchone()
            if task is None:
                raise KeyError("execution task not found")
            if task["status"] != "pending":
                raise ExecutionHandoffError("only a pending execution task can bind a destination")
            eligible = self._eligible_assisted_accounts(
                connection, task["brand_id"], task["provider"],
            )
            selected = [account for account in eligible if account["id"] == connector_account_id]
            if len(selected) != 1:
                raise ExecutionHandoffError(
                    "destination must be an eligible same-brand assisted write account"
                )
            if task["provider"] == "x" and not selected[0].get("username"):
                raise ExecutionHandoffError(
                    "X destination account must configure its canonical username"
                )
            connection.execute(
                "UPDATE execution_tasks SET connector_account_id=?,updated_at=? WHERE id=?",
                (connector_account_id, timestamp, task_id),
            )
            self._audit(connection, task_id, "destination_account_bound", actor, {
                "connector_account_id": connector_account_id,
            })
            row = connection.execute(
                "SELECT * FROM execution_tasks WHERE id=?", (task_id,),
            ).fetchone()
        return self._decode(row)

    def submit_receipt(
        self, task_id: str, *, claim_token: str, external_id: str,
        external_url: str | None, status: str,
        content_fingerprint: str | None = None,
        asset_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        # Convert an expired in-flight provider action to needs_attention before
        # validating the receipt. The original capability remains valid only for
        # reconciling that exact action; the task is never offered to a new worker.
        self.recover_expired()
        task = self._get(task_id, internal=True)
        if task["status"] == "completed":
            if (
                task["receipt_external_id"] == external_id
                and task["receipt_external_url"] == external_url
                and task["receipt_status"] == status
                and (content_fingerprint is None or task.get("receipt_content_fingerprint") == content_fingerprint)
                and (asset_fingerprint is None or task.get("receipt_asset_fingerprint") == asset_fingerprint)
            ):
                return self.get(task_id)
            raise ExecutionHandoffError("execution task already has a different receipt")
        if task["status"] not in {"claimed", "needs_attention"} or not secrets.compare_digest(
            str(task.pop("_claim_token_hash") or ""), _token_hash(claim_token)
        ):
            raise ExecutionHandoffError("a valid active claim is required")
        if task["status"] == "claimed" and self._parse(task["claim_expires_at"]) <= self._clock():
            self.recover_expired()
            raise ExecutionHandoffError("execution claim expired")
        begun = bool(
            task.get("external_action_started_by") == task.get("claimed_by")
            and task.get("external_action_started_at")
            and task.get("external_action_started_fingerprint") == task.get("material_fingerprint")
        )
        if not begun:
            raise ExecutionHandoffError(
                "receipt requires the claimed helper to mark the exact external action started"
            )
        begin_snapshot = self._validated_begin_snapshot(task)
        current_valid = self._current_resource_valid(task_id)
        if not current_valid:
            # The provider action was authorized against an immutable exact
            # snapshot before this later drift. Preserve the sole capability and
            # record reality; never turn it into reclaimable work or project the
            # receipt onto a now-different canonical resource revision.
            self._mark_post_begin_drift(task_id)
            task["status"] = "needs_attention"
        if not _SAFE_EXTERNAL_ID.fullmatch(external_id):
            raise ExecutionHandoffError("external_id must be a safe provider identifier")
        _validate_url(external_url)
        canonical_match = current_valid and self._canonical_receipt_matches(
            task, external_id=external_id, external_url=external_url, status=status,
        )
        _validate_provider_receipt(task["provider"], external_id, external_url)
        if content_fingerprint is not None:
            _validate_sha256_fingerprint(content_fingerprint, "content_fingerprint")
            if content_fingerprint != task["material_fingerprint"]:
                raise ExecutionHandoffError(
                    "receipt content fingerprint does not match the exact approved material"
                )
        if asset_fingerprint is not None:
            _validate_sha256_fingerprint(asset_fingerprint, "asset_fingerprint")
        if task["provider"] != "beehiiv" and asset_fingerprint is not None:
            raise ExecutionHandoffError("asset fingerprint is supported only for Beehiiv draft receipts")
        self._validate_receipt_destination(begin_snapshot, external_url)
        self._reserve_receipt_identity(
            task_id, provider=task["provider"], external_id=external_id,
            external_url=str(external_url),
        )
        if task["provider"] == "beehiiv":
            if status != "draft":
                raise ExecutionHandoffError("Beehiiv handoff accepts only a draft receipt")
            if current_valid and not canonical_match:
                prepared = self.editorial.prepare_export(task["resource_id"], connector="beehiiv")
                self.editorial.record_export_receipt(
                    task["resource_id"], expected_revision=task["revision"],
                    idempotency_key=prepared["idempotency_key"], external_id=external_id,
                    preview_url=external_url, payload_fingerprint=prepared["payload_fingerprint"],
                    connector="beehiiv",
                )
        elif task["provider"] == "x":
            if status != "posted":
                raise ExecutionHandoffError("X handoff requires a posted receipt")
            if current_valid and not canonical_match:
                self.dispatcher.record_external_receipt(
                    task["resource_id"], revision=task["revision"], external_id=external_id,
                    external_url=external_url, actor=f"browser-handoff:{task['claimed_by']}",
                )
        else:
            raise ExecutionHandoffError("unsupported execution provider")
        timestamp = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """UPDATE execution_tasks SET status='completed',receipt_external_id=?,
                   receipt_external_url=?,receipt_status=?,receipt_recorded_at=?,updated_at=?,
                   receipt_content_fingerprint=?,receipt_asset_fingerprint=?,
                   receipt_reconciliation_state=?,
                   claim_token_hash=NULL,claim_expires_at=NULL,
                   external_action_started_by=NULL,external_action_started_at=NULL,
                   external_action_started_fingerprint=NULL
                   WHERE id=? AND status IN ('claimed','needs_attention')""",
                (external_id, external_url, status, timestamp, timestamp,
                 content_fingerprint, asset_fingerprint,
                 "canonical_projected" if current_valid else "immutable_begin_snapshot",
                 task_id),
            )
            if updated.rowcount != 1:
                raise ExecutionHandoffError("execution task claim changed before receipt commit")
            self._audit(connection, task_id, "receipt_recorded", task["claimed_by"], {
                "external_id": external_id, "status": status,
                "content_fingerprint": content_fingerprint,
                "asset_fingerprint": asset_fingerprint,
                "canonical_projection": current_valid,
                "approval_or_material_drift_after_begin": not current_valid,
            })
            row = connection.execute("SELECT * FROM execution_tasks WHERE id=?", (task_id,)).fetchone()
        if canonical_match:
            try:
                report_successful_workaround(
                    brand_id=task["brand_id"], component="execution-handoff.receipt",
                    workaround_code="canonical-receipt-recovery",
                    related_ids=[task_id, task["resource_id"]],
                )
            except Exception:
                pass
        return self._decode(row)

    def _reserve_receipt_identity(
        self, task_id: str, *, provider: str, external_id: str, external_url: str,
    ) -> None:
        """Bind one provider object to one handoff before canonical projection.

        The reservation survives a crash between provider reconciliation and the
        final task update, making retry of the same task safe while preventing a
        different task from laundering the same external object as its receipt.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_provider = connection.execute(
                """SELECT task_id,external_url FROM execution_receipt_reservations
                   WHERE provider=? AND external_id=?""", (provider, external_id),
            ).fetchone()
            existing_task = connection.execute(
                """SELECT provider,external_id,external_url FROM execution_receipt_reservations
                   WHERE task_id=?""", (task_id,),
            ).fetchone()
            if existing_provider is not None and (
                existing_provider["task_id"] != task_id
                or existing_provider["external_url"] != external_url
            ):
                raise ExecutionHandoffError(
                    "provider receipt is already bound to a different execution task"
                )
            if existing_task is not None and (
                existing_task["provider"] != provider
                or existing_task["external_id"] != external_id
                or existing_task["external_url"] != external_url
            ):
                raise ExecutionHandoffError(
                    "execution task is already bound to a different provider receipt"
                )
            if existing_provider is None and existing_task is None:
                connection.execute(
                    """INSERT INTO execution_receipt_reservations
                       (provider,external_id,task_id,external_url,reserved_at)
                       VALUES (?,?,?,?,?)""",
                    (provider, external_id, task_id, external_url, self._now()),
                )

    def begin_external_action(
        self, task_id: str, *, actor: str, claim_token: str,
    ) -> dict[str, Any]:
        """Durably mark the last safe boundary immediately before a provider write.

        A browser helper calls this only after it has loaded and compared the exact
        material.  For X, the separate human confirmation must still be current at
        this boundary.  Receipt reconciliation may happen later: expiration after
        the provider click must never turn the action into a reclaimable duplicate.
        """
        if not _SAFE_IDENTIFIER.fullmatch(actor):
            raise ExecutionHandoffError("action actor must be a safe operator identifier")
        ApprovalSnapshotStore(self.database).reconcile_invalidations()
        self.recover_expired()
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM execution_tasks WHERE id=?", (task_id,),
            ).fetchone()
            if current is None:
                raise KeyError("execution task not found")
            self._assert_bound_destination_account(connection, current)
            if (
                current["status"] != "claimed" or current["claimed_by"] != actor
                or not current["claim_token_hash"]
                or not secrets.compare_digest(current["claim_token_hash"], _token_hash(claim_token))
            ):
                raise ExecutionHandoffError("a valid active claim is required")
            if self._parse(current["claim_expires_at"]) <= now:
                raise ExecutionHandoffError("execution claim expired")
            if not self._provider_enabled(connection, current["brand_id"], current["provider"]):
                raise ExecutionHandoffError(
                    f"{current['provider']} assisted execution is disabled for this brand"
                )
            if not self._valid_in_connection(connection, current):
                connection.execute(
                    """UPDATE execution_tasks SET status='stale',claimed_by=NULL,
                       claimed_at=NULL,claim_expires_at=NULL,claim_token_hash=NULL,
                       updated_at=? WHERE id=?""", (now.isoformat(), task_id),
                )
                self._audit(connection, task_id, "invalidated", "brand-os", {
                    "reason": "exact approved revision is no longer current",
                })
                invalidated = True
            else:
                invalidated = False
                decoded = self._decode(current)
                if current["provider"] == "x" and not self._public_action_confirmation_valid(decoded):
                    raise ExecutionHandoffError(
                        "public X handoff requires a current action-time confirmation"
                    )
                if current["external_action_started_at"]:
                    if (
                        current["external_action_started_by"] != actor
                        or current["external_action_started_fingerprint"]
                        != current["material_fingerprint"]
                    ):
                        raise ExecutionHandoffError("external action start identity conflict")
                else:
                    begin_snapshot = self._build_begin_snapshot(connection, current, actor)
                    snapshot_json = json.dumps(begin_snapshot, sort_keys=True, separators=(",", ":"))
                    connection.execute(
                        """UPDATE execution_tasks SET external_action_started_by=?,
                           external_action_started_at=?,external_action_started_fingerprint=?,
                           external_action_snapshot=?,external_action_snapshot_fingerprint=?,
                           updated_at=? WHERE id=? AND status='claimed'""",
                        (actor, now.isoformat(), current["material_fingerprint"], snapshot_json,
                         "sha256:" + sha256(snapshot_json.encode()).hexdigest(),
                         now.isoformat(), task_id),
                    )
                    self._audit(connection, task_id, "external_action_started", actor, {
                        "provider": current["provider"], "revision": current["revision"],
                        "material_fingerprint": current["material_fingerprint"],
                    })
            row = connection.execute(
                "SELECT * FROM execution_tasks WHERE id=?", (task_id,),
            ).fetchone()
        if invalidated:
            raise ExecutionHandoffError("exact approved revision is no longer current")
        return self._decode(row)

    def _build_begin_snapshot(
        self, connection: sqlite3.Connection, task: sqlite3.Row, actor: str,
    ) -> dict[str, Any]:
        accounts = self._eligible_assisted_accounts(
            connection, task["brand_id"], task["provider"],
        )
        accounts = [account for account in accounts if account["id"] == task["connector_account_id"]]
        if len(accounts) != 1:
            raise ExecutionHandoffError("exactly one bound assisted destination account is required")
        return {
            "brand_id": task["brand_id"], "provider": task["provider"],
            "resource_type": task["resource_type"], "resource_id": task["resource_id"],
            "revision": int(task["revision"]), "campaign_id": task["campaign_id"],
            "asset_membership_id": task["asset_membership_id"],
            "connector_account_id": task["connector_account_id"],
            "material_fingerprint": task["material_fingerprint"],
            "execution_payload": json.loads(task["execution_payload"]),
            "claimed_by": actor, "provider_accounts": accounts,
        }

    def _validated_begin_snapshot(self, task: Mapping[str, Any]) -> dict[str, Any]:
        raw = task.get("external_action_snapshot")
        expected = task.get("external_action_snapshot_fingerprint")
        if not isinstance(raw, str) or not raw or not isinstance(expected, str):
            raise ExecutionHandoffError("immutable external action snapshot is missing")
        actual = "sha256:" + sha256(raw.encode()).hexdigest()
        if not secrets.compare_digest(actual, expected):
            raise ExecutionHandoffError("immutable external action snapshot integrity check failed")
        try:
            snapshot = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise ExecutionHandoffError("immutable external action snapshot is invalid") from error
        expected_fields = {
            "brand_id": task["brand_id"], "provider": task["provider"],
            "resource_type": task["resource_type"], "resource_id": task["resource_id"],
            "revision": int(task["revision"]), "campaign_id": task.get("campaign_id"),
            "asset_membership_id": task.get("asset_membership_id"),
            "connector_account_id": task.get("connector_account_id"),
            "material_fingerprint": task["material_fingerprint"],
            "execution_payload": task["execution_payload"], "claimed_by": task["claimed_by"],
        }
        if any(snapshot.get(key) != value for key, value in expected_fields.items()):
            raise ExecutionHandoffError("immutable external action snapshot no longer matches the task")
        return snapshot

    def _validate_receipt_destination(
        self, snapshot: Mapping[str, Any], external_url: str | None,
    ) -> None:
        if snapshot.get("provider") != "x" or not external_url:
            return
        usernames = {
            str(account.get("username") or "").lower().lstrip("@")
            for account in snapshot.get("provider_accounts", [])
            if account.get("username")
        }
        if len(usernames) == 1:
            segments = [segment for segment in urlsplit(external_url).path.split("/") if segment]
            if not segments or segments[-3].lower() not in usernames:
                raise ExecutionHandoffError(
                    "X receipt URL account does not match the immutable begun provider account"
                )

    @staticmethod
    def _eligible_assisted_accounts(
        connection: sqlite3.Connection, brand_id: str, provider: str,
    ) -> list[dict[str, Any]]:
        has_accounts = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='connector_accounts'"
        ).fetchone()
        if not has_accounts:
            return []
        rows = connection.execute(
            """SELECT a.id,a.account_key,a.display_name,
                      COALESCE(c.configuration,'{}') configuration
               FROM connector_accounts a
               LEFT JOIN connector_account_configurations c ON c.connector_account_id=a.id
               WHERE a.brand_id=? AND a.connector_type=?
                 AND a.status IN ('connected','healthy','active') ORDER BY a.id""",
            (brand_id, provider),
        ).fetchall()
        result = []
        write_roles = {"x": {"x_write"}, "beehiiv": {"beehiiv", "beehiiv_write"}}[provider]
        for row in rows:
            configuration = json.loads(row["configuration"] or "{}")
            if (
                configuration.get("delivery_mode") in {"browser_assisted", "mcp_assisted"}
                and configuration.get("connection_role") in write_roles
            ):
                result.append({
                    "id": row["id"], "account_key": row["account_key"],
                    "display_name": row["display_name"],
                    "username": configuration.get("username"),
                })
        return result

    def _assert_bound_destination_account(
        self, connection: sqlite3.Connection, task: sqlite3.Row,
    ) -> None:
        account_id = task["connector_account_id"]
        if not account_id:
            eligible = self._eligible_assisted_accounts(
                connection, task["brand_id"], task["provider"],
            )
            qualifier = "multiple" if len(eligible) > 1 else "no"
            raise ExecutionHandoffError(
                f"execution task has {qualifier} eligible destination accounts; bind exactly one before claim"
            )
        eligible = self._eligible_assisted_accounts(
            connection, task["brand_id"], task["provider"],
        )
        matching = [account for account in eligible if account["id"] == account_id]
        if len(matching) != 1:
            raise ExecutionHandoffError(
                "bound destination account is no longer an eligible same-brand assisted write account"
            )
        if task["provider"] == "x" and not matching[0].get("username"):
            raise ExecutionHandoffError(
                "bound X destination account must configure its canonical username"
            )

    def _current_resource_valid(self, task_id: str) -> bool:
        ApprovalSnapshotStore(self.database).reconcile_invalidations()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_tasks WHERE id=?", (task_id,),
            ).fetchone()
            return bool(row and self._valid_in_connection(connection, row))

    def _mark_post_begin_drift(self, task_id: str) -> None:
        timestamp = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """UPDATE execution_tasks SET status='needs_attention',updated_at=?
                   WHERE id=? AND status='claimed' AND external_action_started_at IS NOT NULL""",
                (timestamp, task_id),
            )
            if updated.rowcount:
                self._audit(connection, task_id, "post_begin_drift_detected", "brand-os", {
                    "safe_to_reclaim": False,
                    "reconciliation": "original capability and immutable begin snapshot only",
                })

    def _reconcile_started_drift(
        self, *, task_id: str | None = None, brand_id: str | None = None,
    ) -> None:
        query = (
            "SELECT id FROM execution_tasks WHERE status='claimed' "
            "AND external_action_started_at IS NOT NULL"
        )
        values: list[Any] = []
        if task_id is not None:
            query += " AND id=?"; values.append(task_id)
        if brand_id is not None:
            query += " AND brand_id=?"; values.append(brand_id)
        with self._connect() as connection:
            candidates = [row["id"] for row in connection.execute(query, values).fetchall()]
        for candidate in candidates:
            if not self._current_resource_valid(candidate):
                self._mark_post_begin_drift(candidate)

    def recover_expired(self) -> int:
        timestamp = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = connection.execute(
                """SELECT id,claimed_by,external_action_started_at FROM execution_tasks
                   WHERE status='claimed' AND claim_expires_at<=?""", (timestamp,),
            ).fetchall()
            for row in expired:
                ambiguous = bool(row["external_action_started_at"])
                if ambiguous:
                    connection.execute(
                        """UPDATE execution_tasks SET status='needs_attention',updated_at=?
                           WHERE id=?""", (timestamp, row["id"]),
                    )
                else:
                    connection.execute(
                        """UPDATE execution_tasks SET status='pending',claimed_by=NULL,claimed_at=NULL,
                           claim_expires_at=NULL,claim_token_hash=NULL,
                           public_action_confirmed_by=NULL,public_action_confirmed_at=NULL,
                           public_action_confirmation_expires_at=NULL,
                           public_action_confirmation_fingerprint=NULL,updated_at=? WHERE id=?""",
                        (timestamp, row["id"]),
                    )
                self._audit(connection, row["id"],
                            "external_outcome_ambiguous" if ambiguous else "claim_expired",
                            "brand-os", {
                    "previous_claimant": row["claimed_by"],
                    "safe_to_reclaim": not ambiguous,
                })
        return len(expired)

    def audit(self, task_id: str) -> list[dict[str, Any]]:
        self.get(task_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_task_audit WHERE task_id=? ORDER BY sequence", (task_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row); item["detail"] = json.loads(item.pop("detail_json")); result.append(item)
        return result

    def _revalidate(self, task_id: str) -> None:
        task = self.get(task_id)
        valid = self._approval_snapshot_valid(task)
        if task["provider"] == "beehiiv":
            issue = self.editorial.get_issue(task["resource_id"])
            valid = valid and (
                issue["lifecycle"] == "approved" and issue["approval_valid"]
                and issue["current_revision"] == task["revision"]
                and issue["approved_revision"] == task["revision"]
            )
        elif task["provider"] == "x":
            item = self.dispatcher.store.get(task["resource_id"])
            valid = valid and (
                item.connector == "x" and item.status in {Lifecycle.APPROVED, Lifecycle.QUEUED}
                and item.revision == task["revision"] and item.approval is not None
                and item.approval.revision == task["revision"] and item.external_id is None
                and _fingerprint(item.payload) == task["material_fingerprint"]
            )
        if valid:
            return
        self._invalidate_for_approval_change(task_id)

    def _approval_snapshot_valid(self, task: Mapping[str, Any]) -> bool:
        ApprovalSnapshotStore(self.database).reconcile_invalidations()
        with self._connect() as connection:
            return connection.execute(
                """SELECT 1 FROM approval_snapshots s
                   LEFT JOIN approval_snapshot_invalidations i ON i.snapshot_id=s.id
                   WHERE s.brand_id=? AND s.resource_type=? AND s.resource_id=?
                     AND s.revision=? AND s.campaign_id IS ? AND s.asset_membership_id IS ?
                     AND i.snapshot_id IS NULL LIMIT 1""",
                (task["brand_id"], task["resource_type"], task["resource_id"],
                 task["revision"], task.get("campaign_id"), task.get("asset_membership_id")),
            ).fetchone() is not None

    def _public_action_confirmation_valid(self, task: Mapping[str, Any]) -> bool:
        raw_expiry = task.get("public_action_confirmation_expires_at")
        return bool(
            task.get("public_action_confirmed_by")
            and task.get("public_action_confirmation_fingerprint")
            == task.get("material_fingerprint")
            and raw_expiry
            and self._parse(str(raw_expiry)) > self._clock()
        )

    def _invalidate_for_approval_change(self, task_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE execution_tasks SET status='stale',claimed_by=NULL,claimed_at=NULL,
                   claim_expires_at=NULL,claim_token_hash=NULL,
                   public_action_confirmed_by=NULL,public_action_confirmed_at=NULL,
                   public_action_confirmation_expires_at=NULL,
                   public_action_confirmation_fingerprint=NULL,updated_at=?
                   WHERE id=? AND status IN ('pending','claimed')""", (self._now(), task_id),
            )
            self._audit(connection, task_id, "invalidated", "brand-os", {
                "reason": "exact approved revision is no longer current",
            })
        raise ExecutionHandoffError("exact approved revision is no longer current")

    def _valid_in_connection(self, connection: sqlite3.Connection, task: sqlite3.Row) -> bool:
        """Check exact approval under the same SQLite writer lock as claiming."""
        if task["provider"] == "beehiiv":
            issue = connection.execute(
                """SELECT lifecycle,current_revision,approved_revision
                   FROM newsletter_issues WHERE id=?""", (task["resource_id"],),
            ).fetchone()
            return bool(
                issue and issue["lifecycle"] == "approved"
                and issue["current_revision"] == task["revision"]
                and issue["approved_revision"] == task["revision"]
                and self._has_active_snapshot(connection, task["resource_id"], task["revision"])
            )
        if task["provider"] == "x":
            item = connection.execute(
                """SELECT connector,status,revision,approval_revision,external_id,payload
                   FROM dispatch_items WHERE id=?""", (task["resource_id"],),
            ).fetchone()
            valid = bool(
                item and item["connector"] == "x" and item["status"] in {"approved", "queued"}
                and item["revision"] == task["revision"]
                and item["approval_revision"] == task["revision"]
                and item["external_id"] is None
                and _fingerprint(json.loads(item["payload"])) == task["material_fingerprint"]
                and self._has_active_snapshot(connection, task["resource_id"], task["revision"])
            )
            if not valid:
                return False
            try:
                assert_package_dispatch_governance(
                    connection, task["resource_id"], revision=task["revision"],
                )
            except PackageDispatchGovernanceError:
                return False
            return True
        return False

    @staticmethod
    def _has_active_snapshot(
        connection: sqlite3.Connection, resource_id: str, revision: int,
    ) -> bool:
        return connection.execute(
            """SELECT 1 FROM approval_snapshots s
               LEFT JOIN approval_snapshot_invalidations i ON i.snapshot_id=s.id
               WHERE s.resource_id=? AND s.revision=? AND i.snapshot_id IS NULL LIMIT 1""",
            (resource_id, revision),
        ).fetchone() is not None

    def _agent_is_fresh(
        self, connection: sqlite3.Connection, brand_id: str, actor: str,
    ) -> bool:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_agents'"
        ).fetchone()
        if table is None:
            return False
        agent = connection.execute(
            """SELECT last_heartbeat_at FROM execution_agents
               WHERE brand_id=? AND agent_id=? AND enabled=1""", (brand_id, actor),
        ).fetchone()
        if agent is None or not agent["last_heartbeat_at"]:
            return False
        try:
            observed = self._parse(agent["last_heartbeat_at"])
        except (TypeError, ValueError):
            return False
        age = (self._clock() - observed).total_seconds()
        return 0 <= age <= self.agent_freshness_seconds

    @staticmethod
    def _provider_enabled(
        connection: sqlite3.Connection, brand_id: str, provider: str,
    ) -> bool:
        rows = connection.execute(
            """SELECT provider,enabled FROM execution_provider_controls
               WHERE brand_id=? AND provider IN ('all',?)""", (brand_id, provider),
        ).fetchall()
        values = {row["provider"]: bool(row["enabled"]) for row in rows}
        return values.get("all", True) and values.get(provider, True)

    def _canonical_receipt_matches(
        self, task: Mapping[str, Any], *, external_id: str,
        external_url: str | None, status: str,
    ) -> bool:
        """Recover safely if the canonical receipt committed before the task did."""
        if task["provider"] == "beehiiv" and status == "draft":
            issue = self.editorial.get_issue(task["resource_id"])
            return bool(
                issue["current_revision"] == task["revision"]
                and issue["approved_revision"] == task["revision"]
                and issue["beehiiv_external_id"] == external_id
                and issue["beehiiv_preview_url"] == external_url
            )
        if task["provider"] == "x" and status == "posted":
            item = self.dispatcher.store.get(task["resource_id"])
            return bool(
                item.revision == task["revision"] and item.approval is not None
                and item.approval.revision == task["revision"]
                and item.status in {Lifecycle.PUBLISHED, Lifecycle.MEASURED}
                and item.external_id == external_id and item.external_url == external_url
            )
        return False

    def _decode(self, row: sqlite3.Row, *, internal: bool = False) -> dict[str, Any]:
        result = dict(row)
        result["execution_payload"] = json.loads(result["execution_payload"])
        # The snapshot repeats exact material and account identity for internal
        # verification; public callers need only its integrity fingerprint.
        if not internal:
            raw_snapshot = result.get("external_action_snapshot")
            if raw_snapshot:
                try:
                    snapshot = json.loads(raw_snapshot)
                    result["provider_account_binding"] = snapshot.get("provider_accounts", [])
                except (TypeError, json.JSONDecodeError):
                    result["provider_account_binding"] = []
            result.pop("external_action_snapshot", None)
        token_hash = result.pop("claim_token_hash")
        if internal:
            result["_claim_token_hash"] = token_hash
        return result

    def _clock(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("execution handoff clock must be timezone aware")
        return value.astimezone(UTC)

    def _now(self) -> str:
        return self._clock().isoformat()

    @staticmethod
    def _parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)

    def _audit(
        self, connection: sqlite3.Connection, task_id: str, action: str,
        actor: str, detail: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """INSERT INTO execution_task_audit(task_id,action,actor,at,detail_json)
               VALUES (?,?,?,?,?)""",
            (task_id, action, actor, self._now(), json.dumps(dict(detail), sort_keys=True)),
        )


def _fingerprint(payload: Mapping[str, Any]) -> str:
    return "sha256:" + sha256(
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _token_hash(token: str) -> str:
    return sha256(token.encode()).hexdigest()


_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,99}")
_SAFE_EXTERNAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,499}")
_SHA256_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")
_BEEHIIV_POST_ID = re.compile(
    r"(?:post_[A-Za-z0-9-]{1,200}|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})",
    re.IGNORECASE,
)
PUBLIC_X_CONFIRMATION_PHRASE = "CONFIRM PUBLIC X POST"


def _validate_url(url: str | None) -> None:
    if url is None:
        return
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ExecutionHandoffError("external_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ExecutionHandoffError(
            "external_url cannot contain credentials, query parameters, or fragments"
        )


def _validate_sha256_fingerprint(value: str, field: str) -> None:
    if _SHA256_FINGERPRINT.fullmatch(value) is None:
        raise ExecutionHandoffError(f"{field} must be a sha256 fingerprint")


def _validate_provider_receipt(provider: str, external_id: str, url: str | None) -> None:
    if url is None:
        raise ExecutionHandoffError("external_url is required for provider receipt verification")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or parsed.port is not None:
        raise ExecutionHandoffError("provider receipt URL must use canonical HTTPS")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if provider == "beehiiv":
        valid = (
            _BEEHIIV_POST_ID.fullmatch(external_id) is not None
            and host == "app.beehiiv.com" and len(segments) >= 2
            and segments[-2] == "posts" and segments[-1] == external_id
        )
        if not valid:
            raise ExecutionHandoffError(
                "Beehiiv receipt requires a canonical post ID and URL "
                "https://app.beehiiv.com/.../posts/{external_id}"
            )
    elif provider == "x":
        valid = (
            host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
            and len(segments) >= 3 and segments[-2] == "status"
            and segments[-1] == external_id
        )
        if not valid:
            raise ExecutionHandoffError(
                "X receipt URL must be a canonical x.com or twitter.com status URL matching external_id"
            )
    else:
        raise ExecutionHandoffError("unsupported execution provider")


__all__ = ["ExecutionHandoffError", "ExecutionHandoffStore"]
