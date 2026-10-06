import json

import pytest

from app import store
from app.beehiiv_delivery import BeehiivDraftDelivery, BeehiivDraftReceipt
from app.beehiiv_runtime import (
    BEEHIIV_NEWSLETTER_EXPORT_JOB,
    NewsletterExportJobError,
    enqueue_newsletter_export,
    register_beehiiv_newsletter_export,
)
from app.connectors import HttpResponse
from app.editorial import EditorialStore, IssueLifecycle
from app.jobs import JobWorker
from app.runtime import BrandOSRuntime


class Transport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def setup(tmp_path, monkeypatch):
    database = tmp_path / "brand.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    store.init_db()
    brand = store.get_brand("demo-brand")
    editorial = EditorialStore(database, clock=lambda: "2026-09-02T12:00:00Z")
    issue = editorial.create_issue(brand["id"], {
        "editorial_thesis": "Explain the deal", "target_reader": "Readers",
        "intended_outcome": "Make an informed choice", "subject": "Issue",
        "final_title": "Issue", "sections": [{"body": "Body"}],
        "content_basis": {"kind": "original_analysis", "statement": "Test editorial analysis."},
    }, created_by="writer")
    editorial.transition(issue["id"], IssueLifecycle.OUTLINE)
    editorial.transition(issue["id"], IssueLifecycle.DRAFT)
    editorial.record_fact_check(issue["id"], expected_revision=1, reviewer="Chris")
    editorial.approve_issue(issue["id"], approver="Chris", expected_revision=1)
    account = store.upsert_connector_account(brand["id"], "beehiiv", "pub-1", "Newsletter", status="healthy")
    return brand, account, editorial, issue["id"]


def delivery(editorial, transport, feedback=lambda **fields: None):
    return BeehiivDraftDelivery(
        editorial, "pub-1", transport, lambda: "Bearer secret", lambda key: None,
        feedback, wait=lambda seconds: None,
    )


def test_enqueue_is_stable_per_approved_revision(tmp_path, monkeypatch):
    brand, account, editorial, issue_id = setup(tmp_path, monkeypatch)
    first = enqueue_newsletter_export(
        editorial, issue_id, brand_id=brand["id"], connector_account_id=account["id"]
    )
    duplicate = enqueue_newsletter_export(
        editorial, issue_id, brand_id=brand["id"], connector_account_id=account["id"]
    )
    assert duplicate["id"] == first["id"]
    assert first["job_type"] == BEEHIIV_NEWSLETTER_EXPORT_JOB
    assert first["idempotency_key"].endswith(f"{issue_id}:r1")
    assert first["payload"] == {"issue_id": issue_id, "revision": 1, "approval_revision": 1}
    with pytest.raises(NewsletterExportJobError, match="conflicts"):
        enqueue_newsletter_export(editorial, issue_id, connector_account_id="different-account")


def test_unapproved_or_wrong_brand_cannot_enqueue(tmp_path, monkeypatch):
    brand, account, editorial, approved_id = setup(tmp_path, monkeypatch)
    unapproved = editorial.create_issue(brand["id"], {"subject": "No"}, created_by="writer")
    with pytest.raises(NewsletterExportJobError, match="approved"):
        enqueue_newsletter_export(editorial, unapproved["id"])
    with pytest.raises(NewsletterExportJobError, match="brand"):
        enqueue_newsletter_export(editorial, approved_id, brand_id="wrong")


def test_runtime_exports_one_draft_and_runs_reconciliation_hook(tmp_path, monkeypatch):
    brand, account, editorial, issue_id = setup(tmp_path, monkeypatch)
    transport = Transport(HttpResponse(201, json.dumps({"data": {
        "id": "beehiiv-post-1", "status": "draft", "preview_url": "https://preview",
    }}).encode()))
    seen = []

    def reconcile(receipt, job):
        seen.append((receipt.external_id, job["id"]))
        return {"sync_enqueued": True}

    runtime = BrandOSRuntime("runtime", retry_base_seconds=0)
    register_beehiiv_newsletter_export(
        runtime, editorial, delivery(editorial, transport), reconciliation_hooks=(reconcile,)
    )
    job = enqueue_newsletter_export(editorial, issue_id, connector_account_id=account["id"])
    completed = runtime.run_once(as_of="9999-12-31T23:59:59Z")
    assert completed["status"] == "completed"
    assert completed["result"]["external_id"] == "beehiiv-post-1"
    assert completed["result"]["reconciliation"] == [{"sync_enqueued": True}]
    assert seen == [("beehiiv-post-1", job["id"])]
    assert len(transport.calls) == 1
    assert editorial.get_issue(issue_id)["lifecycle"] == "exported"


def test_retry_after_post_export_hook_failure_does_not_create_twice(tmp_path, monkeypatch):
    _, account, editorial, issue_id = setup(tmp_path, monkeypatch)
    transport = Transport(HttpResponse(201, json.dumps({"data": {"id": "post-once", "status": "draft"}}).encode()))
    hook_calls = 0

    def flaky_hook(receipt, job):
        nonlocal hook_calls
        hook_calls += 1
        if hook_calls == 1:
            raise RuntimeError("follow-up unavailable")
        return {"reconciled": True}

    worker = JobWorker("worker", retry_base_seconds=0)
    feedback = []
    register_beehiiv_newsletter_export(
        worker, editorial, delivery(editorial, transport),
        reconciliation_hooks=(flaky_hook,), feedback_reporter=lambda **fields: feedback.append(fields),
    )
    enqueue_newsletter_export(editorial, issue_id, connector_account_id=account["id"], max_attempts=2)
    first = worker.run_once(as_of="9999-12-31T23:59:59Z")
    assert first["status"] == "retry"
    second = worker.run_once(as_of="9999-12-31T23:59:59Z")
    assert second["status"] == "completed"
    assert second["result"]["recovered"] is True
    assert len(transport.calls) == 1
    assert feedback[0]["related_ids"][1] == issue_id


def test_terminal_failure_needs_attention_and_links_feedback(tmp_path, monkeypatch):
    _, account, editorial, issue_id = setup(tmp_path, monkeypatch)

    class FailingDelivery:
        def export_approved_draft(self, issue):
            raise RuntimeError("provider unavailable")

    worker = JobWorker("worker", retry_base_seconds=0)
    linked = []
    register_beehiiv_newsletter_export(
        worker, editorial, FailingDelivery(), feedback_reporter=lambda **fields: linked.append(fields)
    )
    job = enqueue_newsletter_export(editorial, issue_id, connector_account_id=account["id"], max_attempts=1)
    result = worker.run_once(as_of="9999-12-31T23:59:59Z")
    assert result["status"] == "needs_attention"
    assert linked[0]["component"] == BEEHIIV_NEWSLETTER_EXPORT_JOB
    assert linked[0]["related_ids"] == [job["id"], issue_id, account["id"]]
    durable_feedback = store.rows("SELECT * FROM product_feedback WHERE fingerprint LIKE 'job-exhausted:%'")
    assert len(durable_feedback) == 1


def test_terminal_beehiiv_error_never_leaks_exception_text_to_feedback(tmp_path, monkeypatch):
    _, account, editorial, issue_id = setup(tmp_path, monkeypatch)

    class SecretBearingFailure:
        def export_approved_draft(self, issue):
            raise RuntimeError("api_key=beehiiv-provider-secret")

    worker = JobWorker("worker", retry_base_seconds=0)
    register_beehiiv_newsletter_export(worker, editorial, SecretBearingFailure())
    enqueue_newsletter_export(
        editorial, issue_id, connector_account_id=account["id"], max_attempts=1,
    )
    assert worker.run_once(as_of="9999-12-31T23:59:59Z")["status"] == "needs_attention"
    feedback = store.rows("SELECT * FROM product_feedback")
    persisted = " ".join(
        str(item[field]) for item in feedback
        for field in ("details", "actual_behavior", "reproduction", "workaround")
    )
    assert "beehiiv-provider-secret" not in persisted
    assert {item["component"] for item in feedback} == {BEEHIIV_NEWSLETTER_EXPORT_JOB}


def test_stale_revision_job_is_rejected_before_delivery(tmp_path, monkeypatch):
    _, account, editorial, issue_id = setup(tmp_path, monkeypatch)
    enqueue_newsletter_export(editorial, issue_id, connector_account_id=account["id"], max_attempts=1)
    editorial.revise_issue(issue_id, {"subject": "Changed"}, created_by="writer")

    class NeverDelivery:
        def export_approved_draft(self, issue):
            raise AssertionError("must not be called")

    worker = JobWorker("worker")
    register_beehiiv_newsletter_export(worker, editorial, NeverDelivery(), feedback_reporter=lambda **fields: None)
    result = worker.run_once(as_of="9999-12-31T23:59:59Z")
    assert result["status"] == "needs_attention"
    assert "revision changed" in result["last_error"]
