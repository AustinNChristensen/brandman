import json

import pytest

from brandman.beehiiv_delivery import BeehiivDeliveryError, BeehiivDraftDelivery, render_safe_body
from brandman.connectors import HttpResponse
from brandman.editorial import EditorialStore, IssueLifecycle


class FakeTransport:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def response(status, data=None, headers=None):
    return HttpResponse(status, json.dumps(data or {}).encode(), headers or {})


@pytest.fixture
def approved(tmp_path):
    store = EditorialStore(tmp_path / "editorial.db", clock=lambda: "2026-09-02T12:00:00+00:00")
    issue = store.create_issue("brand-1", {
        "editorial_thesis": "Explain the deal", "target_reader": "Readers",
        "intended_outcome": "Make an informed choice",
        "working_title": "Working", "final_title": "Final <Title>",
        "subject": "Subject", "preview_text": "Preview",
        "sections": [{"heading": "Deal <now>", "body": "Earn & redeem.\n\nNo tricks."}],
        "cta": {"label": "Join & save", "url": "https://demo.example/join?a=1&b=2"},
        "seo": {"title": "SEO"}, "claims": [],
        "source_provenance": [{"source_id": "issuer-1"}],
    }, created_by="writer")
    store.transition(issue["id"], IssueLifecycle.OUTLINE)
    store.transition(issue["id"], IssueLifecycle.DRAFT)
    store.record_fact_check(issue["id"], expected_revision=1, reviewer="Chris")
    store.approve_issue(issue["id"], approver="Chris", expected_revision=1)
    return store, issue["id"]


def adapter(store, transport, *, lookup=lambda key: None, feedback=None, waits=None, polls=3):
    return BeehiivDraftDelivery(
        store, "pub-1", transport, lambda: "Bearer secret", lookup,
        feedback if feedback is not None else lambda **fields: None,
        wait=(waits.append if waits is not None else lambda seconds: None),
        max_async_polls=polls,
    )


def test_exports_only_a_safe_draft_and_persists_receipt(approved):
    store, issue_id = approved
    transport = FakeTransport(response(201, {"data": {
        "id": "post-9", "status": "draft", "preview_url": "https://beehiiv.test/preview/9",
    }}))
    receipt = adapter(store, transport).export_approved_draft(issue_id)
    method, _, call = transport.calls[0]
    assert method == "POST"
    assert call["json_body"]["status"] == "draft"
    assert not ({"send_at", "schedule_at", "publish"} & set(call["json_body"]))
    assert "&lt;now&gt;" in call["json_body"]["body_content"]
    assert "Join &amp; save" in call["json_body"]["body_content"]
    assert call["headers"]["Idempotency-Key"].endswith(f":{issue_id}:r1")
    assert receipt.external_id == "post-9"
    persisted = store.get_issue(issue_id)
    assert persisted["lifecycle"] == "exported"
    assert persisted["beehiiv_preview_url"] == "https://beehiiv.test/preview/9"
    repeated = adapter(store, transport).export_approved_draft(issue_id)
    assert repeated.external_id == "post-9"
    assert repeated.recovered is True
    assert len(transport.calls) == 1


def test_render_body_does_not_emit_unsafe_cta_url():
    body = render_safe_body(["Safe <copy>"], {"label": "Click", "url": "javascript:alert(1)"})
    assert "&lt;copy&gt;" in body
    assert "javascript:" not in body
    assert "<strong>Click</strong>" in body


def test_202_honors_retry_after_with_bounded_injected_wait(approved):
    store, issue_id = approved
    waits = []
    transport = FakeTransport(
        response(202, {}, {"Location": "/operations/op-1", "Retry-After": "2"}),
        response(201, {"data": {"id": "post-async", "status": "draft"}}),
    )
    receipt = adapter(store, transport, waits=waits).export_approved_draft(issue_id)
    assert waits == [2.0]
    assert [call[0] for call in transport.calls] == ["POST", "GET"]
    assert receipt.external_id == "post-async"


def test_ambiguous_transport_failure_reconciles_before_retry(approved):
    store, issue_id = approved
    transport = FakeTransport(TimeoutError("unknown outcome"))
    lookups = []

    def lookup(key):
        lookups.append(key)
        # First preflight has no record; recovery lookup finds the created draft.
        return None if len(lookups) == 1 else {"id": "post-recovered", "status": "draft", "editor_url": "https://edit"}

    receipt = adapter(store, transport, lookup=lookup).export_approved_draft(issue_id)
    assert receipt.recovered is True
    assert receipt.external_id == "post-recovered"
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("status", "body", "category"),
    [(403, {"error": "missing scope"}, "permission"), (402, {"error": "upgrade plan"}, "plan"), (500, {}, "provider")],
)
def test_failures_are_safe_and_structured_feedback(approved, status, body, category):
    store, issue_id = approved
    feedback = []
    transport = FakeTransport(response(status, body))
    with pytest.raises(BeehiivDeliveryError) as caught:
        adapter(store, transport, feedback=lambda **fields: feedback.append(fields)).export_approved_draft(issue_id)
    assert caught.value.category == category
    assert feedback[0]["component"] == "beehiiv.newsletter.export"
    assert feedback[0]["related_ids"] == [issue_id, "pub-1"]
    assert "secret" not in json.dumps(feedback)
    assert store.get_issue(issue_id)["lifecycle"] == "approved"


def test_async_pending_is_bounded_and_visible(approved):
    store, issue_id = approved
    feedback, waits = [], []
    transport = FakeTransport(response(202, {}, {"Retry-After": "999"}))
    delivery = adapter(
        store, transport, lookup=lambda key: None,
        feedback=lambda **fields: feedback.append(fields), waits=waits, polls=2,
    )
    with pytest.raises(BeehiivDeliveryError, match="still pending"):
        delivery.export_approved_draft(issue_id)
    assert waits == [10, 10]
    assert len(transport.calls) == 1
    assert feedback[0]["actual_behavior"].endswith("status=202.")


def test_async_poll_transport_failure_reconciles(approved):
    store, issue_id = approved
    transport = FakeTransport(
        response(202, {}, {"Location": "/operations/op-1"}),
        TimeoutError("poll response lost"),
    )
    lookups = []

    def lookup(key):
        lookups.append(key)
        return None if len(lookups) == 1 else {"id": "post-after-poll", "status": "draft"}

    receipt = adapter(store, transport, lookup=lookup).export_approved_draft(issue_id)
    assert receipt.external_id == "post-after-poll"
    assert receipt.recovered is True


def test_unapproved_issue_never_reaches_transport(tmp_path):
    store = EditorialStore(tmp_path / "editorial.db")
    issue = store.create_issue("brand-1", {"subject": "Not approved"}, created_by="writer")
    transport = FakeTransport(response(201, {"id": "impossible"}))
    with pytest.raises(Exception, match="approved"):
        adapter(store, transport).export_approved_draft(issue["id"])
    assert transport.calls == []
