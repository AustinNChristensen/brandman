import base64

import pytest
from fastapi.testclient import TestClient

from brandman.feedback import FeedbackError, FeedbackStore


def report(store: FeedbackStore, **overrides):
    values = {
        "brand_id": "brand-1",
        "reporter": "demo-brand-agent",
        "summary": "Approval queue is missing batch review",
        "details": "The operator must review items one at a time.",
        "component": "approval",
        "severity": "high",
        "reproduction": "Open the approval queue with two drafts.",
        "expected_behavior": "Review an exact batch.",
        "actual_behavior": "Only individual review is available.",
        "workaround": "Review individually.",
        "related_ids": ["dispatch-1"],
    }
    values.update(overrides)
    return store.report(**values)


def test_deduplicated_recurrence_preserves_governed_status(tmp_path):
    store = FeedbackStore(tmp_path / "feedback.db")
    first = report(store, fingerprint="approval:batch")
    started = store.start(
        first["id"], assignee="builder", actor="chris",
        implementation_links=["/app/main.py"], implementation_notes="Adding exact batch review.",
    )
    resolved = store.resolve(
        first["id"], actor="chris", resolution_evidence="Regression tests pass.",
        implementation_links=["/app/main.py", "/tests/test_dispatch_api.py"],
    )
    assert started["status"] == "in_progress"
    assert resolved["status"] == "resolved"

    recurrence = report(
        store, fingerprint="approval:batch", details="Observed again after deployment.",
        related_ids=["dispatch-2"],
    )
    assert recurrence["id"] == first["id"]
    assert recurrence["occurrence_count"] == 2
    assert recurrence["status"] == "resolved"
    assert recurrence["details"] == "Observed again after deployment."
    assert recurrence["related_ids"] == ["dispatch-2"]
    assert store.history(first["id"])[-1]["action"] == "recurrence_reported"


def test_full_lifecycle_chris_gates_and_reopen(tmp_path):
    store = FeedbackStore(tmp_path / "feedback.db")
    item = report(store)
    with pytest.raises(FeedbackError, match="cannot transition"):
        store.resolve(item["id"], actor="chris", resolution_evidence="Not started")
    item = store.start(item["id"], assignee="secure-connections", actor="agent")
    with pytest.raises(PermissionError, match="authenticated human"):
        store.resolve(item["id"], actor="agent", resolution_evidence="Trust me")
    item = store.resolve(item["id"], actor="chris", resolution_evidence="Test run 109 passed")
    with pytest.raises(PermissionError, match="authenticated human"):
        store.verify(item["id"], actor="agent", evidence="Looks good")
    item = store.verify(item["id"], actor="chris", evidence="Reproduced fixed behavior")
    assert item["status"] == "verified"
    assert item["resolved_by"] == "chris"
    assert item["verified_by"] == "chris"

    reopened = store.reopen(item["id"], actor="demo-brand-agent", reason="Regression observed")
    assert reopened["status"] == "open"
    assert reopened["resolved_by"] is None
    assert reopened["verified_by"] is None
    assert [event["action"] for event in store.history(item["id"])] == [
        "reported", "in_progress", "resolved", "verified", "open"
    ]


def test_comments_detail_filters_and_history(tmp_path):
    store = FeedbackStore(tmp_path / "feedback.db")
    target = report(store)
    report(store, summary="Another issue", component="beehiiv", severity="medium")
    comment = store.comment(target["id"], "Here is an agent reproduction.", actor="agent")
    detail = store.get(target["id"])
    assert detail["comments"] == [comment]
    assert detail["history"][-1]["action"] == "commented"
    assert [item["id"] for item in store.list(
        brand_id="brand-1", status="open", component="approval", severity="high"
    )] == [target["id"]]


def test_reconciliation_matches_generic_gaps_without_resolving(tmp_path):
    store = FeedbackStore(tmp_path / "feedback.db")
    generic = report(
        store, summary="Cannot safely save an X access token",
        details="Need encrypted connector credentials.", component="unknown",
        fingerprint="legacy:credentials",
    )
    matches = store.reconcile_shipped_component(
        "secure-connections",
        keywords=["access token", "connector credentials"],
        implementation_links=["/app/credentials.py"],
    )
    assert matches == [{
        "feedback_id": generic["id"], "component": "secure-connections",
        "status": "open", "reason": "matched keywords: access token, connector credentials",
    }]
    detail = store.get(generic["id"])
    assert detail["status"] == "open"
    assert detail["implementation_matches"][0]["implementation_links"] == ["/app/credentials.py"]
    assert store.reconcile_shipped_component(
        "secure-connections", keywords=["access token"], implementation_links=["/app/credentials.py"]
    )[0]["status"] == "open"
    assert [e["action"] for e in store.history(generic["id"])].count("implementation_match_found") == 1


def test_reconciliation_attaches_evidence_to_already_classified_gap(tmp_path):
    store = FeedbackStore(tmp_path / "feedback.db")
    classified = report(
        store,
        summary="Mission plan date ignores the mission timezone",
        details="A UTC instant is rendered as the wrong mission day.",
        component="orchestration.mission-plan",
        fingerprint="mission-plan:timezone",
    )

    matches = store.reconcile_shipped_component(
        "orchestration.mission-plan",
        keywords=["mission timezone"],
        implementation_links=["/app/operating_plan.py", "/tests/test_operating_plan.py"],
    )

    assert matches == [{
        "feedback_id": classified["id"],
        "component": "orchestration.mission-plan",
        "status": "open",
        "reason": "matched keywords: mission timezone",
    }]
    detail = store.get(classified["id"])
    assert detail["implementation_matches"] == [{
        "id": detail["implementation_matches"][0]["id"],
        "feedback_id": classified["id"],
        "component": "orchestration.mission-plan",
        "reason": "matched keywords: mission timezone",
        "implementation_links": ["/app/operating_plan.py", "/tests/test_operating_plan.py"],
        "matched_at": detail["implementation_matches"][0]["matched_at"],
    }]
    assert detail["status"] == "open"


def test_rest_lifecycle_uses_authenticated_human_and_mcp_is_agent_only(tmp_path, monkeypatch):
    from brandman import store as brand_store
    from brandman.main import app
    from brandman.mcp_server import mcp

    brand_store.DATA_PATH = tmp_path / "api-feedback.db"
    monkeypatch.setenv("BRANDMAN_PREVIEW_PASSWORD", "feedback-test-password")
    headers = {"Authorization": "Basic " + base64.b64encode(
        b"operator:feedback-test-password"
    ).decode()}
    with TestClient(app, headers=headers) as client:
        created = client.post("/api/brands/demo-brand/product-feedback", json={
            "reporter": "demo-brand-agent",
            "summary": "Need a durable feedback lifecycle",
            "details": "Generic gaps cannot be verified today.",
            "component": "agent-workflow",
            "severity": "high",
        }).json()
        comment = client.post(f"/api/product-feedback/{created['id']}/comments", json={
            "body": "Reproduced during the morning plan."
        })
        assert comment.status_code == 201
        started = client.post(f"/api/product-feedback/{created['id']}/start", json={
            "assignee": "builder", "implementation_links": ["/app/feedback.py"],
            "implementation_notes": "Implementing lifecycle storage.",
        }).json()
        assert started["status"] == "in_progress"

        spoof = client.post(f"/api/product-feedback/{created['id']}/resolve", json={
            "resolution_evidence": "Tests pass", "actor": "mallory",
        })
        assert spoof.status_code == 422
        resolved = client.post(f"/api/product-feedback/{created['id']}/resolve", json={
            "resolution_evidence": "Focused and full tests pass.",
            "implementation_links": ["/app/feedback.py", "/tests/test_feedback.py"],
        }).json()
        assert resolved["resolved_by"] == "chris"
        verified = client.post(f"/api/product-feedback/{created['id']}/verify", json={
            "evidence": "Demo Brand adversarial review confirmed the behavior."
        }).json()
        assert verified["status"] == "verified"
        assert verified["verified_by"] == "chris"

        filtered = client.get(
            "/api/brands/demo-brand/product-feedback?status=verified&assignee=builder"
        ).json()
        assert [item["id"] for item in filtered] == [created["id"]]
        detail = client.get(f"/api/product-feedback/{created['id']}").json()
        history = client.get(f"/api/product-feedback/{created['id']}/history").json()
        assert detail["comments"][0]["actor"] == "chris"
        assert history[-1]["action"] == "verified"

    tools = set(mcp._tool_manager._tools)
    assert {"report_product_gap", "comment_on_product_gap", "list_product_gaps"} <= tools
    assert {"resolve_product_gap", "verify_product_gap", "reopen_product_gap",
            "reconcile_product_gap"}.isdisjoint(tools)
