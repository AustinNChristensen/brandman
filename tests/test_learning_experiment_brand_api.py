import base64

from fastapi.testclient import TestClient

from brandman import store
from brandman.main import app


HEADERS = {"Authorization": "Basic " + base64.b64encode(b"operator:test-only-password").decode()}


def test_brand_scoped_learning_lifecycle_uses_authenticated_actor_and_refuses_cross_brand(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_PATH", tmp_path / "learning-brand-api.db")
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "test-only-password")
    with TestClient(app, headers=HEADERS) as client:
        assert client.post("/api/brands/demo-brand/learnings", json={
            "hypothesis": "Spoof attempt", "evidence": "Evidence",
            "proposed_change": "Change", "actor": "agent",
        }).status_code == 422
        proposed = client.post("/api/brands/demo-brand/learnings", json={
            "hypothesis": "Numeric source-grounded hooks improve clicks",
            "evidence": "Two aggregate campaign observations",
            "proposed_change": "Test verified numbers in X hooks",
            "scope": {"channel": "x"},
        })
        assert proposed.status_code == 201
        learning = proposed.json()
        audit = client.get(f"/api/brands/demo-brand/learnings/{learning['id']}/audit").json()
        assert audit[0]["actor"] == "chris"
        assert audit[0]["action"] == "proposed"
        assert client.post(
            f"/api/brands/demo-brand/learnings/{learning['id']}/accept"
        ).status_code == 409
        assert client.post(
            f"/api/brands/demo-brand/learnings/{learning['id']}/testing"
        ).json()["status"] == "testing"
        accepted = client.post(
            f"/api/brands/demo-brand/learnings/{learning['id']}/accept"
        ).json()
        assert accepted["status"] == "accepted"
        assert accepted["active"] is True

        other = store.insert("brands", {
            "slug": "other-brand", "name": "Other Brand", "mission": "Other",
            "voice": "Other", "compliance_rules": "Other",
            "approval_policy": "human_approval_required",
            "updated_at": store.now(),
        })
        assert other["id"] != learning["brand_id"]
        assert client.get(
            f"/api/brands/other-brand/learnings/{learning['id']}/audit"
        ).status_code == 404
        assert client.post(
            f"/api/brands/other-brand/learnings/{learning['id']}/supersede"
        ).status_code == 404
        superseded = client.post(
            f"/api/brands/demo-brand/learnings/{learning['id']}/supersede"
        ).json()
        assert superseded["status"] == "superseded"
        assert superseded["active"] is False
        history = client.get(
            f"/api/brands/demo-brand/learnings/{learning['id']}/audit"
        ).json()
        assert [item["action"] for item in history] == ["proposed", "testing", "accepted", "superseded"]
        assert all(item["actor"] == "chris" for item in history)

        rejected = client.post("/api/brands/demo-brand/learnings", json={
            "hypothesis": "Unsupported hook", "evidence": "A weak aggregate result",
            "proposed_change": "Do not apply this without testing",
        }).json()
        rejected = client.post(
            f"/api/brands/demo-brand/learnings/{rejected['id']}/reject"
        ).json()
        assert rejected["status"] == "rejected"
        assert rejected["active"] is False


def test_brand_scoped_experiment_detail_and_decisions_never_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_PATH", tmp_path / "experiment-brand-api.db")
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "test-only-password")
    with TestClient(app, headers=HEADERS) as client:
        source = client.post("/api/brands/demo-brand/sources", json={
            "title": "Verified offer", "source_type": "rss", "body_summary": "The source reports 80,000 credits.",
            "url": "https://source.test/offer", "lifecycle_state": "published",
        }).json()
        campaign = client.post("/api/brands/demo-brand/campaigns", json={
            "name": "Experiment campaign", "objective": "Test framing", "source_id": source["id"],
        }).json()
        created = client.post("/api/brands/demo-brand/experiments", json={
            "campaign_id": campaign["id"], "hypothesis": "Source update framing wins", "metric": "clicks",
            "guardrails": {"min_impressions_per_variant": 10},
            "measurement_windows": [{
                "window_key": "launch", "opens_at": "2026-09-01T00:00:00Z",
                "closes_at": "2026-09-01T13:00:00Z", "evaluate_at": "2026-09-01T13:00:00Z",
                "late_evidence_until": "2026-09-03T13:00:00Z",
            }],
        }).json()
        detail = client.get(f"/api/brands/demo-brand/experiments/{created['id']}")
        assert detail.status_code == 200
        assert all(item["post_status"] == "draft" for item in detail.json()["variants"])
        store.insert("brands", {
            "slug": "other-brand", "name": "Other Brand", "mission": "Other", "voice": "Other",
            "compliance_rules": "Other", "approval_policy": "human_approval_required",
            "updated_at": store.now(),
        })
        assert client.get(f"/api/brands/other-brand/experiments/{created['id']}").status_code == 404
        assert client.post(
            f"/api/brands/other-brand/experiments/{created['id']}/recommendations"
        ).status_code == 404
        assert store.rows("SELECT status,scheduled_for,external_post_id FROM posts WHERE campaign_id=?", (campaign["id"],)) == [
            {"status": "draft", "scheduled_for": None, "external_post_id": None},
            {"status": "draft", "scheduled_for": None, "external_post_id": None},
        ]
