from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from app.main import app


def headers(password: str) -> dict[str, str]:
    token = base64.b64encode(f"operator:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_http_content_authorship_is_session_derived_and_resubmission_is_governed(monkeypatch):
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "content-authoring-test")
    with TestClient(app, headers=headers("content-authoring-test")) as client:
        spoofed_issue = client.post("/api/brands/demo-brand/newsletter-issues", json={
            "content": {"working_title": "Spoof attempt"}, "created_by": "mallory",
        })
        assert spoofed_issue.status_code == 422
        issue = client.post("/api/brands/demo-brand/newsletter-issues", json={
            "content": {"working_title": "Principal-owned draft"},
        })
        assert issue.status_code == 201
        assert issue.json()["content"]["created_by"] == "preview-operator"

        created = client.post("/api/brands/demo-brand/dispatch-items", json={
            "connector": "x", "payload": {"body": "First review"},
        }).json()
        assert client.post(
            f"/api/dispatch-items/{created['id']}/submit", json={"actor": "mallory"},
        ).status_code == 422
        submitted = client.post(f"/api/dispatch-items/{created['id']}/submit", json={})
        assert submitted.status_code == 200
        rejected = client.post(f"/api/dispatch-items/{created['id']}/reject", json={"revision": 1})
        assert rejected.json()["status"] == "rejected"
        assert client.patch(
            f"/api/dispatch-items/{created['id']}",
            json={"payload": {"body": "Spoof edit"}, "actor": "mallory"},
        ).status_code == 422
        revised = client.patch(
            f"/api/dispatch-items/{created['id']}", json={"payload": {"body": "Second review"}},
        )
        assert revised.json()["status"] == "draft"
        assert revised.json()["revision"] == 2
        assert client.post(f"/api/dispatch-items/{created['id']}/submit", json={}).json()["status"] == "awaiting_approval"
        audit = client.get(f"/api/dispatch-items/{created['id']}/audit").json()
        assert [(entry["action"], entry["actor"]) for entry in audit] == [
            ("created", "preview-operator"), ("awaiting_approval", "preview-operator"),
            ("rejected", "preview-operator"), ("edited; approval invalidated", "preview-operator"),
            ("awaiting_approval", "preview-operator"),
        ]


def test_editorial_candidate_to_newsletter_preserves_brand_and_source_lineage(monkeypatch):
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "candidate-parity-test")
    with TestClient(app, headers=headers("candidate-parity-test")) as client:
        brand = client.get("/api/brands/demo-brand/context").json()
        source = client.post("/api/brands/demo-brand/sources", json={
            "title": "Official terms", "source_type": "manual", "body_summary": "Terms",
            "url": "https://issuer.test/terms", "lifecycle_state": "published",
        }).json()
        candidate = client.post("/api/brands/demo-brand/editorial-candidates", json={
            "title": "Explain the offer", "summary": "Decision support",
            "dimensions": {"relevance": 1, "confidence": 1},
            "recommended_treatment": "newsletter_and_social",
            "rationale": ["High reader value"],
            "supporting_sources": [{"source_id": source["id"], "url": source["url"]}],
        })
        assert candidate.status_code == 201
        candidate_body = candidate.json()
        assert candidate_body["brand_id"] == brand["id"]
        issue = client.post("/api/brands/demo-brand/newsletter-issues", json={
            "candidate_id": candidate_body["id"],
            "content": {
                "working_title": candidate_body["title"],
                "source_provenance": candidate_body["supporting_sources"],
            },
        })
        assert issue.status_code == 201
        assert issue.json()["candidate_id"] == candidate_body["id"]
        assert issue.json()["content"]["source_provenance"][0]["source_id"] == source["id"]
        listed = client.get("/api/brands/demo-brand/editorial-candidates?include_inactive=true").json()
        assert next(item for item in listed if item["id"] == candidate_body["id"])["status"] == "selected"

        other = client.post("/api/brands", json={
            "slug": "other-brand", "name": "Other", "mission": "Other mission",
            "voice": "Other voice", "compliance_rules": "Review everything",
            "approval_policy": "human_approval_required",
        }).json()
        foreign = client.post("/api/brands/other-brand/newsletter-issues", json={
            "candidate_id": candidate_body["id"], "content": {"working_title": "Wrong brand"},
        })
        assert other["id"] != brand["id"]
        assert foreign.status_code == 409
        assert "different brand" in foreign.json()["detail"]


def test_campaign_post_revision_dispatch_validation_and_audit_are_draft_only(monkeypatch):
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "campaign-post-test")
    with TestClient(app, headers=headers("campaign-post-test")) as client:
        campaign = client.post("/api/brands/demo-brand/campaigns", json={
            "name": "Decision support", "objective": "Help readers compare",
        }).json()
        assert client.post(f"/api/campaigns/{campaign['id']}/posts", json={
            "channel": "x", "body": "Spoofed", "actor": "mallory",
        }).status_code == 422
        created = client.post(f"/api/campaigns/{campaign['id']}/posts", json={
            "channel": "x", "body": "Canonical draft",
        })
        assert created.status_code == 201
        post = created.json()
        assert post["status"] == "draft" and post["revision"] == 1
        assert client.patch(f"/api/posts/{post['id']}", json={
            "body": "Spoofed revision", "actor": "mallory",
        }).status_code == 422
        revised = client.patch(f"/api/posts/{post['id']}", json={
            "body": "Canonical revision",
        }).json()
        assert revised["status"] == "draft" and revised["revision"] == 2
        audit = client.get(f"/api/posts/{post['id']}/audit").json()
        assert [(event["action"], event["actor"], event["revision"]) for event in audit] == [
            ("draft_created", "preview-operator", 1), ("draft_revised", "preview-operator", 2),
        ]

        dispatch = client.post(f"/api/posts/{post['id']}/dispatch", json={}).json()
        assert dispatch["status"] == "draft"
        assert dispatch["canonical_post_id"] == post["id"]
        validation = client.get(f"/api/dispatch-items/{dispatch['id']}/validation").json()
        assert validation["valid"] is True
        dispatch_audit = client.get(f"/api/dispatch-items/{dispatch['id']}/audit").json()
        assert dispatch_audit[0]["actor"] == "preview-operator"
        blocked = client.patch(f"/api/posts/{post['id']}", json={"body": "Late divergence"})
        assert blocked.status_code == 409
        assert "exact dispatch copy" in blocked.json()["detail"]


def test_candidate_promotes_atomically_to_brand_owned_draft_campaign_post(monkeypatch):
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "candidate-post-test")
    with TestClient(app, headers=headers("candidate-post-test")) as client:
        candidate = client.post("/api/brands/demo-brand/editorial-candidates", json={
            "title": "Transfer window", "summary": "Help readers decide",
            "dimensions": {"relevance": 1, "confidence": 1},
            "recommended_treatment": "newsletter_and_social",
        }).json()
        assert client.post(
            f"/api/brands/demo-brand/editorial-candidates/{candidate['id']}/campaign-post",
            json={"campaign_name": "Spoof", "objective": "Spoof", "channel": "x", "body": "Spoof", "actor": "mallory"},
        ).status_code == 422
        promoted = client.post(
            f"/api/brands/demo-brand/editorial-candidates/{candidate['id']}/campaign-post",
            json={"campaign_name": "Transfer decision", "objective": "Help readers decide", "channel": "x", "body": "A candidate-grounded draft."},
        )
        assert promoted.status_code == 201
        result = promoted.json()
        assert result["campaign"]["status"] == "draft"
        assert result["post"]["status"] == "draft"
        assert result["post"]["candidate_id"] == candidate["id"]
        assert result["post"]["created_by"] == "preview-operator"
        assert result["candidate"]["status"] == "selected"
        history = client.get(f"/api/editorial-candidates/{candidate['id']}/history").json()
        assert history[-1]["action"] == "promoted_to_campaign_post"
        assert history[-1]["actor"] == "preview-operator"

        client.post("/api/brands", json={
            "slug": "other-brand", "name": "Other", "mission": "Other",
            "voice": "Other", "compliance_rules": "Review",
            "approval_policy": "human_approval_required",
        })
        foreign_candidate = client.post("/api/brands/demo-brand/editorial-candidates", json={
            "title": "Cross brand", "summary": "Block this",
            "dimensions": {"relevance": 1}, "recommended_treatment": "social",
        }).json()
        refused = client.post(
            f"/api/brands/other-brand/editorial-candidates/{foreign_candidate['id']}/campaign-post",
            json={"campaign_name": "Wrong", "objective": "Wrong", "channel": "x", "body": "Wrong"},
        )
        assert refused.status_code == 404
