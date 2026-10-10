from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app import store
from app.main import app
from app.editorial import EditorialStore
from app.publishing_planner import PublishingPlanner, PublishingPlannerError


NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)


def seeded_plan(tmp_path):
    database = tmp_path / "publishing-plan.db"
    store.DATA_PATH = database
    store.init_db(profile="test")
    EditorialStore(database)
    brand = store.create_brand({
        "slug": "planner-test", "name": "Planner test", "mission": "Test safely",
        "voice": "Clear", "compliance_rules": "No provider actions",
        "approval_policy": "human_approval_required",
    })
    campaign = store.insert("campaigns", {
        "brand_id": brand["id"], "source_id": None, "name": "Fall launch",
        "objective": "Coordinate launch", "status": "draft",
    })
    posts = [store.insert("posts", {
        "campaign_id": campaign["id"], "channel": "x", "body": title,
        "status": "draft", "scheduled_for": None, "external_post_id": None,
    }) for title in ("First", "Second", "Third")]
    planner = PublishingPlanner(database, clock=lambda: NOW)
    return planner, brand, campaign, posts


def settings(planner, brand):
    return planner.update_settings(
        brand["id"], timezone="America/Denver",
        windows=[{"weekday": 0, "start": "09:00", "end": "17:00"}],
        cadence_minutes={"x": 120, "newsletter": 1440}, actor="preview-operator",
    )


def test_deterministic_reflow_commit_and_exact_undo_never_touch_canonical_schedule(tmp_path):
    planner, brand, campaign, posts = seeded_plan(tmp_path)
    configured = settings(planner, brand)
    assert configured["timezone"] == "America/Denver"
    planner.update_item(
        brand["id"], "post", posts[0]["id"], initiative_id=campaign["id"],
        planned_for="2026-09-07T10:00:00-06:00", pinned=True, locked=False, actor="preview-operator",
    )
    planner.update_item(
        brand["id"], "post", posts[2]["id"], initiative_id=campaign["id"],
        planned_for=None, pinned=False, locked=True, actor="preview-operator",
    )

    first = planner.preview_reflow(
        brand["id"], start_at="2026-09-07T09:00:00-06:00", actor="preview-operator",
    )
    second = planner.preview_reflow(
        brand["id"], start_at="2026-09-07T09:00:00-06:00", actor="preview-operator",
    )
    assert first["changes"] == second["changes"]
    assert [change["item_id"] for change in first["changes"]] == [posts[1]["id"]]
    assert first["changes"][0]["after"] == "2026-09-07T15:00:00+00:00"

    committed = planner.commit_reflow(brand["id"], first["id"], actor="preview-operator")
    assert planner.commit_reflow(brand["id"], first["id"], actor="preview-operator")["id"] == committed["id"]
    assert store.row("SELECT scheduled_for FROM posts WHERE id=?", (posts[1]["id"],))["scheduled_for"] is None
    view = planner.view(brand["id"])
    assert view["safety"] == {
        "planning_only": True, "provider_write_performed": False, "approval_granted": False,
    }
    assert view["initiatives"][0]["id"] == campaign["id"]

    undone = planner.undo_reflow(brand["id"], committed["id"], actor="preview-operator")
    assert undone["undone_by"] == "preview-operator"
    assert planner.undo_reflow(brand["id"], committed["id"], actor="preview-operator")["undone_at"] == undone["undone_at"]
    current = {item["item_id"]: item for item in planner.view(brand["id"])["initiatives"][0]["items"]}
    assert current[posts[1]["id"]]["planned_for"] is None


def test_preview_stales_and_exact_undo_refuses_intervening_change(tmp_path):
    planner, brand, campaign, posts = seeded_plan(tmp_path)
    settings(planner, brand)
    preview = planner.preview_reflow(
        brand["id"], start_at="2026-09-07T09:00:00-06:00", actor="preview-operator",
    )
    planner.update_item(
        brand["id"], "post", posts[0]["id"], initiative_id=campaign["id"],
        planned_for=None, pinned=True, locked=False, actor="preview-operator",
    )
    with pytest.raises(PublishingPlannerError, match="changed after preview"):
        planner.commit_reflow(brand["id"], preview["id"], actor="preview-operator")

    fresh = planner.preview_reflow(
        brand["id"], start_at="2026-09-07T09:00:00-06:00", actor="preview-operator",
    )
    commit = planner.commit_reflow(brand["id"], fresh["id"], actor="preview-operator")
    changed = fresh["changes"][0]
    planner.update_item(
        brand["id"], "post", changed["item_id"], initiative_id=campaign["id"],
        planned_for="2026-09-14T09:00:00-06:00", pinned=False, locked=False, actor="preview-operator",
    )
    with pytest.raises(PublishingPlannerError, match="exact undo is unsafe"):
        planner.undo_reflow(brand["id"], commit["id"], actor="preview-operator")


def test_planner_api_is_brand_scoped_and_uses_authenticated_principal(monkeypatch):
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "planner-api-secret")
    auth = base64.b64encode(b"operator:planner-api-secret").decode()
    headers = {"Authorization": f"Basic {auth}"}
    with TestClient(app, headers=headers) as client:
        campaign = client.post(
            "/api/brands/demo-brand/campaigns",
            json={"name": "Planner API", "objective": "Prove safe planning"},
        ).json()
        post = client.post(
            f"/api/campaigns/{campaign['id']}/posts", json={"channel": "x", "body": "Draft"},
        ).json()
        configured = client.put("/api/brands/demo-brand/publishing-plan/settings", json={
            "timezone": "America/Denver",
            "windows": [{"weekday": 0, "start": "09:00", "end": "17:00"}],
            "cadence_minutes": {"x": 120, "newsletter": 1440},
        })
        assert configured.status_code == 200
        assert configured.json()["updated_by"] == "preview-operator"
        item = client.put(
            f"/api/brands/demo-brand/publishing-plan/items/post/{post['id']}",
            json={"initiative_id": campaign["id"], "planned_for": None, "pinned": False, "locked": False},
        )
        assert item.status_code == 200
        preview = client.post(
            "/api/brands/demo-brand/publishing-plan/reflow/preview",
            json={"start_at": "2026-09-07T09:00:00-06:00"},
        )
        assert preview.status_code == 200
        committed = client.post(
            "/api/brands/demo-brand/publishing-plan/reflow/commit",
            json={"preview_id": preview.json()["id"]},
        )
        assert committed.status_code == 200
        assert committed.json()["committed_by"] == "preview-operator"
        undone = client.post(
            "/api/brands/demo-brand/publishing-plan/reflow/undo",
            json={"commit_id": committed.json()["id"]},
        )
        assert undone.status_code == 200
        assert undone.json()["undone_by"] == "preview-operator"
        plan = client.get("/api/brands/demo-brand/publishing-plan").json()
        assert plan["safety"]["planning_only"] is True
        assert client.put(
            "/api/brands/demo-brand/publishing-plan/items/post/not-owned",
            json={"initiative_id": campaign["id"], "pinned": False, "locked": False},
        ).status_code == 404
