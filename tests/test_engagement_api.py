from __future__ import annotations

import base64
import os

os.environ["BRAND_OS_PREVIEW_PASSWORD"] = "test-only-password"

from fastapi.testclient import TestClient

from app import store
from app.connectors import ConnectorEvent, ConnectorKind, ConnectorResult, EventKind, dedup_identity
from app.main import app, engagement_store
from app.mcp_server import (
    dismiss_engagement_opportunity as mcp_dismiss,
    draft_engagement_action as mcp_draft,
    get_engagement_history as mcp_history,
    get_engagement_opportunity as mcp_get,
    list_engagement_opportunities as mcp_list,
    submit_engagement_action as mcp_submit,
)
from app.sync import SyncOrchestrator


HEADERS = {
    "Authorization": "Basic " + base64.b64encode(b"operator:test-only-password").decode()
}


def opportunity_event(post_id: str = "910000001") -> ConnectorEvent:
    return ConnectorEvent(
        ConnectorKind.X,
        EventKind.POST_PUBLISHED,
        dedup_identity(ConnectorKind.X, external_id=f"mention:{post_id}"),
        "2026-09-02T12:00:00+00:00",
        post_id,
        {
            "evidence_type": "x_engagement_opportunity",
            "opportunity_type": "mention",
            "text": "How should I use these credits?",
            "author": {"id": "reader-1", "username": "reader"},
            "conversation_id": post_id,
            "referenced_tweets": [],
            "parent_context": [],
            "public_metrics": {"reply_count": 0},
            "requires_approval": True,
            "external_url": f"https://x.com/i/web/status/{post_id}",
        },
    )


def seed_opportunity(post_id: str = "910000001") -> tuple[dict, dict]:
    brand = store.get_brand("demo-brand")
    saved = engagement_store.project(
        brand_id=brand["id"], connector_account_id="x-read-api",
        event=opportunity_event(post_id),
    )
    return brand, saved


def reset_engagement() -> None:
    with store.connection() as connection:
        connection.execute("DELETE FROM x_engagement_history")
        connection.execute("DELETE FROM x_engagement_opportunities")


def test_sync_optionally_projects_engagement_and_remains_compatible_without_inbox():
    with TestClient(app, headers=HEADERS):
        reset_engagement()
        brand = store.get_brand("demo-brand")
        account = store.upsert_connector_account(
            brand["id"], "x", "read-sync-api", "X read sync API",
            status="healthy", scopes=["tweet.read", "users.read"],
            capabilities=["engagement.read"],
        )
        event = opportunity_event("910000002")
        outcome = SyncOrchestrator(engagement_inbox=engagement_store).apply_result(
            ConnectorResult((event,)), connector_kind=ConnectorKind.X,
            brand_id=brand["id"], connector_account_id=account["id"],
            stream="engagement",
        )
        assert outcome.engagement_projected == 1
        assert engagement_store.list(brand_id=brand["id"], state="new")

        account_without = store.upsert_connector_account(
            brand["id"], "x", "read-no-inbox-api", "X no inbox API",
            status="healthy", scopes=["tweet.read", "users.read"],
        )
        compatible = SyncOrchestrator().apply_result(
            ConnectorResult((opportunity_event("910000003"),)),
            connector_kind=ConnectorKind.X, brand_id=brand["id"],
            connector_account_id=account_without["id"], stream="engagement",
        )
        assert compatible.engagement_projected == 0


def test_rest_brand_scoped_inbox_detail_history_draft_and_submit():
    with TestClient(app, headers=HEADERS) as client:
        reset_engagement()
        _, saved = seed_opportunity("910000004")
        listed = client.get("/api/brands/demo-brand/engagement?state=new").json()
        assert saved["id"] in {item["id"] for item in listed}
        detail = client.get(
            f"/api/brands/demo-brand/engagement/{saved['id']}"
        ).json()
        assert detail["requires_approval"] is True

        drafted = client.post(
            f"/api/brands/demo-brand/engagement/{saved['id']}/draft-action",
            json={
                "action_type": "reply",
                "text": "Start with the integration that matches your plan.",
            },
        )
        assert drafted.status_code == 200
        body = drafted.json()
        assert body["opportunity"]["state"] == "drafted"
        assert body["dispatch_item"]["status"] == "draft"

        submitted = client.post(
            f"/api/brands/demo-brand/engagement/{saved['id']}/submit-action",
            json={},
        ).json()
        assert submitted["opportunity"]["state"] == "awaiting_approval"
        assert submitted["dispatch_item"]["status"] == "awaiting_approval"
        assert submitted["dispatch_item"]["approval"] is None
        history = client.get(
            f"/api/brands/demo-brand/engagement/{saved['id']}/history"
        ).json()
        assert {entry["action"] for entry in history} >= {
            "ingested", "action_drafted", "awaiting_approval"
        }
        assert {
            entry["actor"] for entry in history
            if entry["action"] in {"action_drafted", "awaiting_approval"}
        } == {"chris"}


def test_rest_dismiss_and_brand_scope_are_enforced():
    with TestClient(app, headers=HEADERS) as client:
        reset_engagement()
        _, saved = seed_opportunity("910000005")
        dismissed = client.post(
            f"/api/brands/demo-brand/engagement/{saved['id']}/dismiss",
            json={"reason": "Not relevant"},
        ).json()
        assert dismissed["state"] == "dismissed"
        assert client.get(
            f"/api/brands/demo-personal/engagement/{saved['id']}"
        ).status_code == 404
        assert client.post(
            f"/api/brands/demo-brand/engagement/{saved['id']}/dismiss",
            json={"actor": "spoofed", "reason": "No"},
        ).status_code == 422


def test_mcp_agent_surface_can_read_draft_submit_and_dismiss_but_not_approve():
    with TestClient(app, headers=HEADERS):
        reset_engagement()
        _, first = seed_opportunity("910000006")
        assert first["id"] in {item["id"] for item in mcp_list("demo-brand")}
        assert mcp_get("demo-brand", first["id"])["id"] == first["id"]
        drafted = mcp_draft(
            "demo-brand", first["id"], "reply", "demo-brand-agent",
            "Use the airline program only after confirming award space.",
        )
        assert drafted["dispatch_item"]["status"] == "draft"
        submitted = mcp_submit("demo-brand", first["id"], "demo-brand-agent")
        assert submitted["dispatch_item"]["status"] == "awaiting_approval"
        assert submitted["dispatch_item"]["approval"] is None
        assert {entry["action"] for entry in mcp_history("demo-brand", first["id"])} >= {
            "action_drafted", "awaiting_approval"
        }

        _, second = seed_opportunity("910000007")
        assert mcp_dismiss(
            "demo-brand", second["id"], "demo-brand-agent", "Low value"
        )["state"] == "dismissed"
