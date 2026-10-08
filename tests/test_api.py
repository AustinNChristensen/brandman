import os
import base64
from datetime import datetime
from uuid import uuid4

os.environ["BRAND_OS_PREVIEW_PASSWORD"] = "test-only-password"

from fastapi.testclient import TestClient
from brandman.main import app
from brandman import store


def newsletter_review_token(client, issue_id):
    issues = client.get("/api/brands/demo-brand/newsletter-issues").json()
    return next(issue for issue in issues if issue["id"] == issue_id)["approval_scope"]["review_token"]


def test_approval_gate_and_schedule_flow():
    token = base64.b64encode(b"operator:test-only-password").decode()
    with TestClient(app, headers={"Authorization": f"Basic {token}"}) as client:
        assert client.get("/health").json()["status"] == "ok"
        points = client.get("/api/brands/demo-brand/context").json()
        campaign = client.post("/api/brands/demo-brand/campaigns", json={"name": "Test campaign", "objective": "Verify governed flow"}).json()
        post = client.post(f"/api/campaigns/{campaign['id']}/posts", json={"channel": "x", "body": "Draft text"}).json()
        scheduled = client.post(
            f"/api/posts/{post['id']}/schedule",
            params={"scheduled_for": "2026-09-02T15:00:00Z"},
        )
        approved = client.post(f"/api/posts/{post['id']}/approve")
        assert scheduled.status_code == approved.status_code == 410
        assert "canonical dispatch" in scheduled.json()["detail"]
        assert "exact-review" in approved.json()["detail"]
        unchanged = client.get(f"/api/campaigns/{campaign['id']}/posts").json()[0]
        assert unchanged["status"] == "draft"


def test_operator_workflow_endpoint_is_the_authoritative_safe_progress_contract():
    token = base64.b64encode(b"operator:test-only-password").decode()
    with TestClient(app, headers={"Authorization": f"Basic {token}"}) as client:
        response = client.get("/api/brands/demo-brand/operator-workflow")
        assert response.status_code == 200
        workflow = response.json()
        assert workflow["schema_version"] == 1
        assert workflow["total_steps"] == 9
        assert len(workflow["stages"]) == 9
        assert sum(stage["status"] == "current" for stage in workflow["stages"]) <= 1
        assert set(workflow["next_action"]) == {"code", "text", "target"}
        assert "cannot approve or publish" in workflow["safety"]
        console_response = client.get("/api/brands/demo-brand/execution-console")
        assert console_response.status_code == 200
        console = console_response.json()
        assert console["schema_version"] == 1
        assert set(console) == {
            "schema_version", "tasks", "execution_agents",
            "beehiiv_aggregate_pulls", "safety",
        }
        assert console["safety"] == {
            "provider_write_performed": False,
            "claim_grants_approval": False,
            "receipt_reconciles_existing_result": True,
            "subscriber_data_allowed": False,
        }
        assert all("claim_token" not in task for task in console["tasks"])


def test_beehiiv_sync_is_idempotent_and_appears_on_calendar():
    token = base64.b64encode(b"operator:test-only-password").decode()
    post = {"id": "post_test", "title": "Live Beehiiv Post", "editor_url": "https://example.test/edit", "status": "scheduled", "scheduled_at": "2026-09-02T15:30:00Z", "content_tags": [{"display": "test"}]}
    with TestClient(app, headers={"Authorization": f"Basic {token}"}) as client:
        assert client.post("/api/brands/demo-brand/sources/beehiiv/sync", json=[post]).json()["synced"] == 1
        assert client.post("/api/brands/demo-brand/sources/beehiiv/sync", json=[post]).json()["synced"] == 1
        calendar = client.get("/api/brands/demo-brand/calendar").json()
        assert len([item for item in calendar if item["external_source_id"] == "post_test"]) == 1


def test_performance_learning_and_product_feedback_loop():
    token = base64.b64encode(b"operator:test-only-password").decode()
    headers = {"Authorization": f"Basic {token}"}
    with TestClient(app, headers=headers) as client:
        performance = client.post("/api/brands/demo-brand/performance", json={
            "channel": "x",
            "observed_at": "2026-09-03T15:30:00Z",
            "impressions": 420,
            "clicks": 21,
            "engagements": 35,
            "conversions": 3,
            "notes": "Opinion-led post with a single CTA",
        })
        assert performance.status_code == 201
        assert client.get("/api/brands/demo-brand/performance").json()[0]["clicks"] == 21

        proposed = client.post("/api/brands/demo-brand/learnings", json={
            "hypothesis": "Opinion-led posts generate more qualified traffic.",
            "evidence": "This post produced 21 clicks and 3 conversions.",
            "proposed_change": "Use one opinion-led X post per issue.",
        }).json()
        context = client.get("/api/brands/demo-brand/context").json()
        assert proposed["id"] not in {item["id"] for item in context["accepted_learnings"]}
        assert client.post(f"/api/learnings/{proposed['id']}/testing").json()["status"] == "testing"
        assert client.post(f"/api/learnings/{proposed['id']}/accept").json()["status"] == "accepted"
        context = client.get("/api/brands/demo-brand/context").json()
        assert proposed["id"] in {item["id"] for item in context["accepted_learnings"]}

        feedback = client.post("/api/brands/demo-brand/product-feedback", json={
            "reporter": "demo-brand-agent",
            "summary": "Need subscriber cohort attribution",
            "details": "Conversions cannot yet be segmented by acquisition source.",
        })
        assert feedback.status_code == 201
        assert feedback.json()["status"] == "open"


def test_learning_actor_is_authenticated_principal_and_testing_is_mandatory():
    token = base64.b64encode(b"operator:test-only-password").decode()
    with TestClient(app, headers={"Authorization": f"Basic {token}"}) as client:
        proposed = client.post("/api/brands/demo-brand/learnings", json={
            "hypothesis": "Authenticated lifecycle actor", "evidence": "One measured outcome",
            "proposed_change": "Test a specific hook",
        }).json()
        assert client.post(f"/api/learnings/{proposed['id']}/accept").status_code == 409
        assert client.post(f"/api/learnings/{proposed['id']}/testing", json={"actor": "spoofed"}).status_code == 422
        transitioned = client.post(f"/api/learnings/{proposed['id']}/testing").json()
        assert transitioned["status"] == "testing"
        history = store.row(
            "SELECT actor FROM brand_learning_audit WHERE learning_id=? AND action='testing'",
            (proposed["id"],),
        )
        assert history["actor"] == "chris"


def test_demo_brand_mission_connectors_and_deduplicated_gaps(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_PATH", tmp_path / "mission-api.db")
    token = base64.b64encode(b"operator:test-only-password").decode()
    with TestClient(app, headers={"Authorization": f"Basic {token}"}) as client:
        mission = client.get("/api/brands/demo-brand/mission").json()
        goals = {goal["metric"]: goal for goal in mission["goals"]}
        assert goals["x_followers"]["baseline"] == 0
        assert goals["x_followers"]["target"] == 100
        assert goals["active_beehiiv_subscribers"]["baseline"] == 0
        assert goals["active_beehiiv_subscribers"]["target"] == 25

        snapshot = client.post("/api/brands/demo-brand/mission/kpis", json={
            "metric": "x_followers", "value": 9,
            "observed_at": "2026-09-01T18:00:00Z", "source": "manual",
            "verification_note": "Authenticated preview baseline check",
        })
        assert snapshot.status_code == 201
        refreshed = {
            goal["metric"]: goal
            for goal in client.get("/api/brands/demo-brand/mission").json()["goals"]
        }
        assert refreshed["x_followers"]["current"] == 9
        assert refreshed["x_followers"]["trajectory_status"] in {"ahead", "behind", "on_pace"}
        plan = client.get("/api/brands/demo-brand/mission/morning-plan").json()
        assert plan["kind"] == "morning_plan"
        assert {goal["metric"] for goal in plan["goals"]} == {
            "x_followers", "active_beehiiv_subscribers",
        }
        assert client.get("/api/brands/demo-brand/mission/scorecard").json()["kind"] == "end_of_day_scorecard"

        client.post("/api/brands/demo-brand/mission/kpis", json={
            "metric": "x_followers", "value": 12,
            "observed_at": "2026-09-02T18:00:00Z", "source": "manual",
            "verification_note": "Newer verified observation",
        })
        client.post("/api/brands/demo-brand/mission/kpis", json={
            "metric": "x_followers", "value": 7,
            "observed_at": "2026-08-31T18:00:00Z", "source": "manual",
            "verification_note": "Late-arriving older observation",
        })
        current = {
            goal["metric"]: goal
            for goal in client.get("/api/brands/demo-brand/mission").json()["goals"]
        }
        assert current["x_followers"]["current"] == 12
        assert datetime.fromisoformat(
            current["x_followers"]["latest_observed_at"].replace("Z", "+00:00")
        ) == datetime.fromisoformat("2026-09-02T18:00:00+00:00")

        tracked = client.post("/api/brands/demo-brand/tracked-url", json={
            "base_url": "https://demo.example/start",
            "source": "x", "medium": "organic-social",
            "campaign_id": "campaign-1", "artifact_id": "post-1", "cta_id": "subscribe",
        }).json()
        assert "utm_campaign=campaign-1" in tracked["url"]
        assert "utm_content=post-1" in tracked["url"]

        connector = {
            "connector_type": "x", "account_key": "demo-brand",
            "display_name": "@Demo_Brand", "status": "healthy",
            "scopes": ["tweet.read"], "capabilities": ["metrics.read"],
        }
        first = client.post("/api/brands/demo-brand/connectors", json=connector).json()
        second = client.post("/api/brands/demo-brand/connectors", json=connector).json()
        assert first["id"] == second["id"]
        connectors = client.get("/api/brands/demo-brand/connectors").json()
        assert next(item for item in connectors if item["id"] == first["id"])["scopes"] == ["tweet.read"]

        gap = {
            "reporter": "demo-agent", "summary": "Missing read permission",
            "details": "X metrics unavailable", "component": "x.sync",
            "severity": "high", "fingerprint": "x:missing-read",
        }
        first_gap = client.post("/api/brands/demo-brand/product-feedback", json=gap).json()
        second_gap = client.post("/api/brands/demo-brand/product-feedback", json=gap).json()
        assert second_gap["id"] == first_gap["id"]
        assert second_gap["occurrence_count"] == first_gap["occurrence_count"] + 1


def test_editorial_issue_and_canonical_dispatch_integration():
    token = base64.b64encode(b"operator:test-only-password").decode()
    with TestClient(app, headers={"Authorization": f"Basic {token}"}) as client:
        campaign = client.post("/api/brands/demo-brand/campaigns", json={
            "name": "Canonical dispatch", "objective": "Test one content source",
        }).json()
        post = client.post(f"/api/campaigns/{campaign['id']}/posts", json={
            "channel": "x", "body": "One governed source of truth.",
        }).json()
        first = client.post(f"/api/posts/{post['id']}/dispatch").json()
        second = client.post(f"/api/posts/{post['id']}/dispatch").json()
        assert first["id"] == second["id"]
        assert first["canonical_post_id"] == post["id"]
        assert client.get(f"/api/dispatch-items/{first['id']}/validation").json()["valid"] is True

        content = {
            "editorial_thesis": "Brand OS should own the issue.",
            "target_reader": "Readers", "intended_outcome": "Subscribe",
            "working_title": "A better pricing decision", "final_title": "A better pricing decision",
            "subject": "Make one better pricing decision", "preview_text": "The useful math.",
            "sections": [{"heading": "The decision", "body": "Details"}],
            "cta": {"label": "Start", "url": "https://demo.example/start"},
            "seo": {"title": "Pricing decision", "description": "Practical guidance"},
            "content_basis": {"kind": "original_analysis", "statement": "DemoBrand decision framework."},
            "claims": [], "source_provenance": [],
        }
        issue = client.post("/api/brands/demo-brand/newsletter-issues", json={
            "content": content,
        }).json()
        for target in ("outline", "draft"):
            issue = client.post(f"/api/newsletter-issues/{issue['id']}/transition", json={"target": target}).json()
        checked = client.post(f"/api/newsletter-issues/{issue['id']}/fact-check", json={
            "revision": 1, "verdicts": [], "notes": "Reviewed against source material",
        })
        assert checked.status_code == 200
        approved = client.post(
            f"/api/newsletter-issues/{issue['id']}/approve",
            json={"revision": 1, "review_token": newsletter_review_token(client, issue["id"])},
        ).json()
        assert approved["approved_by"] == "chris"
        preview = client.get(f"/api/newsletter-issues/{issue['id']}/export-preview").json()
        assert preview["idempotency_key"].endswith(":r1")


def test_newsletter_rejection_is_revision_exact_and_returns_work_for_changes():
    token = base64.b64encode(b"operator:test-only-password").decode()
    content = {
        "editorial_thesis": "Make a useful decision.",
        "target_reader": "Readers", "intended_outcome": "Review the offer",
        "working_title": "Offer review", "final_title": "Offer review",
        "subject": "Review this offer", "preview_text": "The useful details.",
        "sections": [{"heading": "The offer", "body": "Current details"}],
        "cta": {"label": "Read more", "url": "https://demo.example/offer"},
        "seo": {"title": "Offer review", "description": "Current offer details"},
        "content_basis": {"kind": "original_analysis", "statement": "DemoBrand analysis."},
        "claims": [], "source_provenance": [],
    }
    with TestClient(app, headers={"Authorization": f"Basic {token}"}) as client:
        issue = client.post("/api/brands/demo-brand/newsletter-issues", json={
            "content": content,
        }).json()
        for target in ("outline", "draft"):
            client.post(
                f"/api/newsletter-issues/{issue['id']}/transition", json={"target": target},
            )
        client.post(f"/api/newsletter-issues/{issue['id']}/fact-check", json={
            "revision": 1, "verdicts": [], "notes": "Reviewed",
        })

        stale = client.post(f"/api/newsletter-issues/{issue['id']}/reject", json={
            "revision": 2, "reason": "Needs a clearer headline",
        })
        assert stale.status_code == 409
        rejected = client.post(f"/api/newsletter-issues/{issue['id']}/reject", json={
            "revision": 1, "reason": "Needs a clearer headline",
        })
        assert rejected.status_code == 200
        body = rejected.json()
        assert body["lifecycle"] == "draft"
        assert body["current_revision"] == 2
        history = client.get(f"/api/newsletter-issues/{issue['id']}/history").json()
        assert history[-1]["action"] == "rejected"
        assert history[-1]["reason"] == "Needs a clearer headline"


def test_connector_kpi_without_evidence_is_rejected_and_tracked_links_persist():
    token = base64.b64encode(b"operator:test-only-password").decode()
    with TestClient(app, headers={"Authorization": f"Basic {token}"}) as client:
        rejected = client.post("/api/brands/demo-brand/mission/kpis", json={
            "metric": "x_followers", "value": 999,
            "observed_at": "2026-09-02T18:00:00Z", "source": "x",
        })
        assert rejected.status_code == 409
        payload = {
            "base_url": "https://demo.example/start", "source": "x",
            "medium": "organic-social", "campaign_id": "persisted-campaign",
            "artifact_id": "persisted-post", "cta_id": "subscribe",
        }
        created = client.post("/api/brands/demo-brand/tracked-url", json=payload).json()
        repeated = client.post("/api/brands/demo-brand/tracked-url", json=payload).json()
        assert repeated["id"] == created["id"]
        assert created["id"] in {
            item["id"] for item in client.get("/api/brands/demo-brand/tracked-links").json()
        }


def test_editorial_cleanup_routes_use_authenticated_actor_and_preserve_history():
    token = base64.b64encode(b"operator:test-only-password").decode()
    with TestClient(app, headers={"Authorization": f"Basic {token}"}) as client:
        candidate = client.post("/api/brands/demo-brand/editorial-candidates", json={
            "title": f"Cleanup candidate {uuid4()}",
            "dimensions": {"relevance": .2, "confidence": .2},
        }).json()
        abandoned = client.post(
            f"/api/editorial-candidates/{candidate['id']}/abandon",
            json={"reason": "No longer timely"},
        )
        assert abandoned.status_code == 200
        assert abandoned.json()["status"] == "abandoned"
        history = client.get(f"/api/editorial-candidates/{candidate['id']}/history").json()
        assert history[0]["actor"] == "chris"
        assert history[0]["reason"] == "No longer timely"
        active_ids = {
            item["id"] for item in client.get("/api/brands/demo-brand/editorial-candidates").json()
        }
        all_ids = {
            item["id"] for item in client.get(
                "/api/brands/demo-brand/editorial-candidates?include_inactive=true"
            ).json()
        }
        assert candidate["id"] not in active_ids
        assert candidate["id"] in all_ids

        issue = client.post("/api/brands/demo-brand/newsletter-issues", json={
            "content": {"working_title": f"Unused issue {uuid4()}"},
        }).json()
        assert client.post(
            f"/api/newsletter-issues/{issue['id']}/archive",
            json={"reason": "Should be abandoned first"},
        ).status_code == 409
        assert client.post(
            f"/api/newsletter-issues/{issue['id']}/abandon",
            json={"reason": "Superseded before drafting"},
        ).json()["lifecycle"] == "abandoned"
        assert client.post(
            f"/api/newsletter-issues/{issue['id']}/archive",
            json={"reason": "Cleanup reviewed"},
        ).json()["lifecycle"] == "archived"
        issue_history = client.get(f"/api/newsletter-issues/{issue['id']}/history").json()
        assert [event["to_state"] for event in issue_history] == ["abandoned", "archived"]


def test_governed_third_party_source_api_enrolls_and_controls_polling():
    token = base64.b64encode(b"operator:test-only-password").decode()
    with TestClient(app, headers={"Authorization": f"Basic {token}"}) as client:
        bypass = client.post("/api/brands/demo-brand/connectors", json={
            "connector_type": "rss", "account_key": "https://bypass.example.test/feed",
            "display_name": "Bypass", "status": "healthy",
        })
        assert bypass.status_code == 409

        hostname = f"publisher-{uuid4().hex}.example.test"
        created = client.post("/api/brands/demo-brand/third-party-sources", json={
            "publisher_name": "Independent reporting",
            "feed_url": f"https://{hostname}/feed/",
            "homepage_url": f"https://{hostname}/",
            "feed_format": "rss", "polling_interval_seconds": 1800,
            "reason": "Evaluate public syndicated reporting for original coverage",
        })
        assert created.status_code == 201
        source = created.json()
        assert source["enabled"] is True
        assert source["schedule"]["enabled"] is True
        assert source["audit"][0]["actor"] == "chris"
        serialized_source = str(source).casefold()
        assert "access_token" not in serialized_source
        assert "api_key" not in serialized_source
        assert "encrypted_payload" not in serialized_source

        listed = client.get("/api/brands/demo-brand/third-party-sources").json()
        assert source["connector_account_id"] in {
            item["connector_account_id"] for item in listed
        }
        disabled = client.post(
            f"/api/third-party-sources/{source['connector_account_id']}/disable",
            json={"reason": "Pause pending editorial quality review"},
        ).json()
        assert disabled["enabled"] is False
        assert disabled["schedule"]["enabled"] is False
        assert disabled["audit"][-1]["actor"] == "chris"
        enabled_ids = {
            item["connector_account_id"] for item in client.get(
                "/api/brands/demo-brand/third-party-sources?include_disabled=false"
            ).json()
        }
        assert source["connector_account_id"] not in enabled_ids
        assert client.post(
            f"/api/third-party-sources/{source['connector_account_id']}/enable",
            json={"reason": "Editorial quality review passed"},
        ).json()["enabled"] is True
