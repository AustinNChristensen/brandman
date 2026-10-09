import base64
import os

from fastapi.testclient import TestClient

from app import store
from app.main import app


HEADERS = {
    "Authorization": "Basic " + base64.b64encode(b"operator:test-only-password").decode(),
}


def test_rest_drafts_recommends_and_human_accepts_without_publishing(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_PATH", tmp_path / "experiment-api.db")
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "test-only-password")
    with TestClient(app, headers=HEADERS) as client:
        source = client.post("/api/brands/demo-brand/sources", json={
            "title": "Offer update", "source_type": "rss", "body_summary": "The source reports 80,000 credits.",
            "url": "https://source.test/offer", "lifecycle_state": "published",
        }).json()
        campaign = client.post("/api/brands/demo-brand/campaigns", json={
            "name": "Experiment campaign", "objective": "Test a grounded framing", "source_id": source["id"],
        }).json()
        created = client.post("/api/brands/demo-brand/experiments", json={
            "campaign_id": campaign["id"], "hypothesis": "Source update framing wins.",
            "metric": "clicks", "guardrails": {"min_impressions_per_variant": 10},
            "measurement_windows": [{
                "window_key": "launch", "opens_at": "2026-09-01T00:00:00Z",
                "closes_at": "2026-09-01T13:00:00Z",
                "evaluate_at": "2026-09-01T13:00:00Z",
                "late_evidence_until": "2026-09-03T13:00:00Z",
            }],
        })
        assert created.status_code == 201
        experiment = created.json()
        assert all(item["post_status"] == "draft" for item in experiment["variants"])
        assert experiment["measurement_windows"][0]["window_key"] == "launch"
        assert client.get("/api/brands/demo-brand/experiments").json()[0]["id"] == experiment["id"]
        windows = client.get(
            f"/api/experiments/{experiment['id']}/measurement-windows"
        ).json()
        assert windows[0]["metric"] == "clicks"
        assert client.get(
            f"/api/experiment-measurement-windows/{windows[0]['id']}"
        ).json()["status"] == "scheduled"

        account = store.upsert_connector_account(
            experiment["brand_id"], "x", "experiment-metrics", "X", status="healthy",
        )
        for index, variant in enumerate(experiment["variants"]):
            key = f"variant-{index}"
            store.record_connector_event(
                account["id"], "metrics", key, "metric_observed",
                {"post_id": variant["post_id"], "impressions": 100, "clicks": 3 + index * 5},
                "2026-09-01T12:00:00Z",
            )
            store.insert("performance_records", {
                "brand_id": experiment["brand_id"], "post_id": variant["post_id"], "source_id": source["id"],
                "channel": "x", "observed_at": "2026-09-01T12:00:00Z", "impressions": 100,
                "clicks": 3 + index * 5, "engagements": 0, "conversions": 0, "revenue_cents": 0,
                "notes": f"connector_event={account['id']}:{key}",
                "created_at": "2026-09-01T12:00:00Z",
            })
        recommendation = client.post(
            f"/api/experiments/{experiment['id']}/recommendations"
        ).json()
        assert recommendation["status"] == "recommended"
        plan = client.get("/api/brands/demo-brand/mission/morning-plan").json()
        assert any(action["type"] == "review_experiment_winner" for action in plan["actions"])

        accepted = client.post(
            f"/api/experiments/{experiment['id']}/recommendations/{recommendation['id']}/accept"
        ).json()
        assert accepted["status"] == "completed"
        assert accepted["recommendations"][0]["accepted_by"] == "preview-operator"
        assert all(item["post_status"] == "draft" for item in accepted["variants"])


def test_mcp_can_draft_and_recommend_but_has_no_acceptance_authority():
    from app.mcp_server import mcp

    tools = set(mcp._tool_manager._tools)
    assert {
        "draft_x_experiment", "list_x_experiments", "get_x_experiment",
        "recommend_x_experiment_winner", "list_experiment_measurement_windows",
        "get_experiment_measurement_window",
    } <= tools
    assert {
        "accept_x_experiment_winner", "accept_experiment_recommendation",
        "accept_brand_learning",
    }.isdisjoint(tools)
