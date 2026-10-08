from datetime import UTC, datetime
import sqlite3

import pytest
from fastapi.testclient import TestClient

from brandman import store
from brandman.approval_snapshots import ApprovalSnapshotStore
from brandman.dispatch import GovernedDispatcher, SQLiteDispatchStore
from brandman.editorial import EditorialStore, IssueLifecycle
from brandman.main import app


def content():
    return {
        "editorial_thesis": "Useful", "target_reader": "Readers",
        "intended_outcome": "Review", "working_title": "Title", "final_title": "Title",
        "subject": "Subject", "preview_text": "Preview",
        "sections": [{"heading": "Details", "body": "Body"}], "claims": [],
        "source_provenance": [{"source_id": "source", "url": "https://issuer.test"}],
    }


def test_dispatch_and_engagement_snapshots_capture_complete_immutable_scope(tmp_path):
    database = tmp_path / "approval.db"
    dispatcher = GovernedDispatcher(
        SQLiteDispatchStore(database), clock=lambda: datetime(2026, 9, 2, 12, tzinfo=UTC),
    )
    snapshots = ApprovalSnapshotStore(database)
    post = dispatcher.create("x", {
        "body": "Approved body", "connector_account_id": "account-1",
        "scheduled_for": "2026-09-03T12:00:00+00:00",
    }, brand_id="brand-1")
    dispatcher.submit_for_approval(post.id, actor="writer")
    approved = dispatcher.approve(post.id, revision=1, approver="chris")
    snapshot = snapshots.capture_dispatch(approved)
    assert {
        "brand_id": snapshot["brand_id"], "account_ref": snapshot["account_ref"],
        "action_type": snapshot["action_type"], "destination": snapshot["destination"],
        "intended_schedule": snapshot["intended_schedule"], "revision": snapshot["revision"],
        "approver": snapshot["approver"],
    } == {
        "brand_id": "brand-1", "account_ref": "account-1", "action_type": "post",
        "destination": "x:public", "intended_schedule": "2026-09-03T12:00:00+00:00",
        "revision": 1, "approver": "chris",
    }
    assert snapshot["material_fingerprint"].startswith("sha256:")
    assert snapshot["approved_at"] == "2026-09-02T12:00:00+00:00"
    assert snapshot["material_reference"] == {
        "resource_type": "dispatch_item", "resource_id": post.id, "revision": 1,
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT material_json FROM approval_snapshots WHERE id=?", (snapshot["id"],),
        ).fetchone()[0] == "{}"  # Fingerprint + canonical pointer, not a content copy.

    reply = dispatcher.create("x", {"body": "Reply", "reply_to_post_id": "123"}, brand_id="brand-1")
    dispatcher.submit_for_approval(reply.id, actor="writer")
    reply = dispatcher.approve(reply.id, revision=1, approver="chris")
    reply_snapshot = snapshots.capture_dispatch(reply)
    assert reply_snapshot["resource_type"] == "engagement_action"
    assert reply_snapshot["action_type"] == "reply"
    assert reply_snapshot["destination"] == "x:post:123"

    assert snapshots.invalidate_resource(post.id, actor="writer", reason="edited") == 1
    assert snapshots.get(snapshot["id"])["active"] is False
    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("UPDATE approval_snapshots SET approver='mallory' WHERE id=?", (snapshot["id"],))
    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("DELETE FROM approval_snapshot_invalidations WHERE snapshot_id=?", (snapshot["id"],))


def test_x_revisions_remain_reconstructable_after_edit_and_reject_secrets(tmp_path):
    database = tmp_path / "dispatch-revisions.db"
    dispatch_store = SQLiteDispatchStore(database)
    dispatcher = GovernedDispatcher(dispatch_store)
    item = dispatcher.create("x", {"body": "Original approved words"}, brand_id="brand-1")
    dispatcher.submit_for_approval(item.id, actor="writer")
    approved = dispatcher.approve(item.id, revision=1, approver="Chris")
    ApprovalSnapshotStore(database).capture_dispatch(approved)
    dispatcher.edit(item.id, {"body": "Later edit"}, actor="writer")

    original = dispatch_store.get_revision(item.id, 1)
    current = dispatch_store.get_revision(item.id, 2)
    assert original["material"] == {"body": "Original approved words"}
    assert current["material"] == {"body": "Later edit"}
    assert original["material_fingerprint"].startswith("sha256:")
    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE dispatch_revisions SET material_json='{}' WHERE item_id=? AND revision=1",
            (item.id,),
        )
    with pytest.raises(ValueError, match="credential-shaped"):
        dispatcher.create("x", {"body": "Bearer abcdefghijklmnop"}, brand_id="brand-1")


def test_reconciler_invalidates_snapshot_after_direct_canonical_edit(tmp_path):
    database = tmp_path / "reconcile.db"
    dispatcher = GovernedDispatcher(SQLiteDispatchStore(database))
    post = dispatcher.create("x", {"body": "Approved body"}, brand_id="brand-1")
    dispatcher.submit_for_approval(post.id, actor="writer")
    post = dispatcher.approve(post.id, revision=1, approver="chris")
    snapshots = ApprovalSnapshotStore(database)
    evidence = snapshots.capture_dispatch(post)

    # Simulate a crash after the canonical edit but before the HTTP handler can
    # append its invalidation. Reopening the evidence store fails closed.
    dispatcher.edit(post.id, {"body": "Materially changed"}, actor="writer")
    ApprovalSnapshotStore(database)
    assert snapshots.get(evidence["id"])["active"] is False
    assert snapshots.get(evidence["id"])["invalidated_by"] == "approval-reconciler"


def test_legacy_snapshot_identity_migrates_without_losing_immutable_history(tmp_path):
    database = tmp_path / "legacy-approval.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """CREATE TABLE approval_snapshots (
              id TEXT PRIMARY KEY,brand_id TEXT NOT NULL,account_ref TEXT NOT NULL,
              resource_type TEXT NOT NULL,resource_id TEXT NOT NULL,action_type TEXT NOT NULL,
              destination TEXT NOT NULL,intended_schedule TEXT,revision INTEGER NOT NULL,
              approver TEXT NOT NULL,approved_at TEXT NOT NULL,material_fingerprint TEXT NOT NULL,
              material_json TEXT NOT NULL,created_at TEXT NOT NULL,
              UNIQUE(resource_type,resource_id,revision,material_fingerprint));
            CREATE TABLE approval_snapshot_invalidations (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,snapshot_id TEXT NOT NULL,
              actor TEXT NOT NULL,reason TEXT NOT NULL,invalidated_at TEXT NOT NULL,
              UNIQUE(snapshot_id));"""
        )
        connection.execute(
            """INSERT INTO approval_snapshots VALUES
               ('snapshot-1','brand-1','assisted:x','dispatch_item','post-1','post','x:public',
                NULL,1,'Chris','2026-09-02T12:00:00Z','sha256:old','{}','2026-09-02T12:00:00Z')"""
        )
        connection.execute(
            """INSERT INTO approval_snapshot_invalidations
               (snapshot_id,actor,reason,invalidated_at) VALUES
               ('snapshot-1','audit','old context','2026-09-02T13:00:00Z')"""
        )
    snapshots = ApprovalSnapshotStore(database)
    migrated = snapshots.get("snapshot-1")
    assert migrated["active"] is False
    assert migrated["invalidation_reason"] == "old context"
    with sqlite3.connect(database) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='approval_snapshots'"
        ).fetchone()[0]
        assert "UNIQUE(resource_type,resource_id,revision,material_fingerprint)" not in table_sql
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='approval_snapshot_identity'"
        ).fetchone()


def test_newsletter_snapshot_and_backfill_are_idempotent(tmp_path):
    database = tmp_path / "newsletter.db"
    editorial = EditorialStore(database, clock=lambda: "2026-09-02T12:00:00+00:00")
    issue = editorial.create_issue("brand-1", content(), created_by="writer")
    editorial.transition(issue["id"], IssueLifecycle.OUTLINE)
    editorial.transition(issue["id"], IssueLifecycle.DRAFT)
    fact_check = editorial.record_fact_check(
        issue["id"], expected_revision=1, reviewer="Chris", verdicts=[],
    )
    proposed = ApprovalSnapshotStore(database).proposed_newsletter(
        editorial.get_issue(issue["id"]),
    )
    assert proposed["material_fingerprint"] == fact_check["content_fingerprint"]
    editorial.approve_issue(issue["id"], approver="Chris", expected_revision=1)

    snapshots = ApprovalSnapshotStore(database)
    rows = snapshots.list("brand-1")
    assert len(rows) == 1
    assert rows[0]["resource_type"] == "newsletter_issue"
    assert rows[0]["account_ref"] == "assisted:beehiiv"
    assert rows[0]["action_type"] == "create_draft"
    assert rows[0]["destination"] == "beehiiv:draft"
    assert rows[0]["material_fingerprint"] == fact_check["content_fingerprint"]
    ApprovalSnapshotStore(database)
    assert len(snapshots.list("brand-1")) == 1


def test_rest_approval_snapshot_is_readable_and_edit_invalidation_is_visible(tmp_path, monkeypatch):
    from brandman.mcp_server import list_approval_snapshots, mcp

    monkeypatch.setattr(store, "DATA_PATH", tmp_path / "api.db")
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "test")
    auth = ("operator", "test")
    with TestClient(app) as client:
        created = client.post(
            "/api/brands/demo-brand/dispatch-items", auth=auth,
            json={"connector": "x", "payload": {
                "body": "Exact approved post", "connector_account_id": "assisted-x",
                "destination": "x:public", "scheduled_for": "2026-09-03T12:00:00Z",
            }},
        ).json()
        client.post(
            f"/api/dispatch-items/{created['id']}/submit", auth=auth,
            json={},
        )
        approved = client.post(
            f"/api/dispatch-items/{created['id']}/approve", auth=auth,
            json={"revision": 1, "review_token": next(
                item for item in client.get(
                    "/api/brands/demo-brand/dispatch-items", auth=auth,
                ).json() if item["id"] == created["id"]
            )["approval_scope"]["review_token"]},
        )
        assert approved.status_code == 200
        snapshot = approved.json()["approval_snapshot"]
        assert snapshot["account_ref"] == "assisted-x"
        assert snapshot["intended_schedule"] == "2026-09-03T12:00:00Z"
        assert client.get(
            f"/api/approval-snapshots/{snapshot['id']}", auth=auth,
        ).json()["active"] is True
        edited = client.patch(
            f"/api/dispatch-items/{created['id']}", auth=auth,
            json={"payload": {"body": "Changed"}},
        )
        assert edited.status_code == 200
        evidence = client.get(
            "/api/brands/demo-brand/approval-snapshots", auth=auth,
        ).json()
        assert evidence[0]["active"] is False
        assert evidence[0]["invalidation_reason"] == "dispatch edited; approval invalidated"
        assert list_approval_snapshots("demo-brand")[0]["id"] == snapshot["id"]
        assert "list_approval_snapshots" in mcp._tool_manager._tools
        assert "approve_dispatch_item" not in mcp._tool_manager._tools


def test_displayed_scope_token_fails_closed_after_material_change(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_PATH", tmp_path / "scope-token.db")
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "test")
    auth = ("operator", "test")
    with TestClient(app) as client:
        created = client.post(
            "/api/brands/demo-brand/dispatch-items", auth=auth,
            json={"connector": "x", "payload": {"body": "Displayed text"}},
        ).json()
        client.post(
            f"/api/dispatch-items/{created['id']}/submit", auth=auth, json={},
        )
        displayed = next(
            item for item in client.get(
                "/api/brands/demo-brand/dispatch-items", auth=auth,
            ).json() if item["id"] == created["id"]
        )["approval_scope"]
        assert {
            "account_ref", "action_type", "destination", "intended_schedule",
            "campaign_id", "asset_membership_id", "material_fingerprint", "review_token",
        } <= displayed.keys()
        client.patch(
            f"/api/dispatch-items/{created['id']}", auth=auth,
            json={"payload": {"body": "Changed after display"}},
        )
        client.post(
            f"/api/dispatch-items/{created['id']}/submit", auth=auth, json={},
        )
        stale = client.post(
            f"/api/dispatch-items/{created['id']}/approve", auth=auth,
            json={"revision": 2, "review_token": displayed["review_token"]},
        )
        assert stale.status_code == 409
        assert "displayed approval scope" in stale.json()["detail"]
        assert client.get(
            f"/api/dispatch-items/{created['id']}", auth=auth,
        ).json()["status"] == "awaiting_approval"
        assert client.get(
            f"/api/dispatch-items/{created['id']}/revisions/1", auth=auth,
        ).json()["material"] == {"body": "Displayed text"}
