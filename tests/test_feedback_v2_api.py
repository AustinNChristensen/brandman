import base64

from fastapi.testclient import TestClient

from brandman.main import app


def test_brand_feedback_lifecycle_preserves_reporter_and_derives_human_actors(monkeypatch):
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "feedback-v2-test")
    headers = {"Authorization": "Basic " + base64.b64encode(b"operator:feedback-v2-test").decode()}
    with TestClient(app, headers=headers) as client:
        reported = client.post("/api/brands/demo-brand/product-feedback", json={
            "reporter": "demo-brand-agent", "summary": "Approval helper failed",
            "details": "The agent reached a governed dead end.", "component": "approval-flow",
            "severity": "high", "actual_behavior": "No safe next action was offered.",
        })
        assert reported.status_code == 201
        item = reported.json()
        assert item["reporter"] == "demo-brand-agent"

        assert client.post(
            f"/api/brands/demo-brand/product-feedback/{item['id']}/comments",
            json={"actor": "mallory", "body": "spoof"},
        ).status_code == 422
        comment = client.post(
            f"/api/brands/demo-brand/product-feedback/{item['id']}/comments",
            json={"body": "Reproduced from the operator console."},
        )
        assert comment.json()["actor"] == "chris"
        started = client.post(
            f"/api/brands/demo-brand/product-feedback/{item['id']}/start",
            json={"assignee": "builder", "implementation_notes": "Working on the fix."},
        ).json()
        assert started["status"] == "in_progress"
        resolved = client.post(
            f"/api/brands/demo-brand/product-feedback/{item['id']}/resolve",
            json={"resolution_evidence": "Focused regression passes."},
        ).json()
        assert resolved["resolved_by"] == "chris"
        verified = client.post(
            f"/api/brands/demo-brand/product-feedback/{item['id']}/verify",
            json={"evidence": "Operator verified the workflow."},
        ).json()
        assert verified["verified_by"] == "chris"
        reopened = client.post(
            f"/api/brands/demo-brand/product-feedback/{item['id']}/reopen",
            json={"reason": "The failure recurred."},
        ).json()
        assert reopened["status"] == "open"

        detail = client.get(
            f"/api/brands/demo-brand/product-feedback/{item['id']}"
        ).json()
        history = client.get(
            f"/api/brands/demo-brand/product-feedback/{item['id']}/history"
        ).json()
        assert detail["comments"][0]["actor"] == "chris"
        assert history[0]["actor"] == "demo-brand-agent"
        assert {event["actor"] for event in history[1:]} == {"chris"}

        client.post("/api/brands", json={
            "slug": "other-brand", "name": "Other", "mission": "Other",
            "voice": "Other", "compliance_rules": "Review",
            "approval_policy": "human_approval_required",
        })
        base = f"/api/brands/other-brand/product-feedback/{item['id']}"
        assert client.get(base).status_code == 404
        assert client.get(f"{base}/history").status_code == 404
        assert client.post(f"{base}/comments", json={"body": "cross brand"}).status_code == 404
        assert client.post(f"{base}/start", json={"assignee": "builder"}).status_code == 404
        assert client.post(f"{base}/resolve", json={"resolution_evidence": "no"}).status_code == 404
        assert client.post(f"{base}/verify", json={"evidence": "no"}).status_code == 404
        assert client.post(f"{base}/reopen", json={"reason": "no"}).status_code == 404
