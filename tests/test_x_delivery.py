from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import store
from app.connectors import DispatchReceipt, HttpResponse, ConnectorKind, dedup_identity
from app.dispatch import GovernedDispatcher, Lifecycle, SQLiteDispatchStore
from app.jobs import JobWorker
from app.x_delivery import (
    XDeliveryJobHandler,
    XPublisherAdapter,
    X_DELIVERY_JOB_TYPE,
    X_MEASUREMENT_JOB_TYPE,
    enqueue_x_delivery,
    register_x_delivery,
)


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def response(status: int, payload: dict) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload).encode())


def setup_system(tmp_path: Path, *, transport=None, reconciler=None, scopes=None, status="healthy"):
    store.DATA_PATH = tmp_path / "brand-os.db"
    store.init_db()
    brand = store.get_brand("demo-brand")
    account = store.upsert_connector_account(
        brand["id"], "x", "demobrand", "Demo Brand",
        status=status, scopes=scopes if scopes is not None else [
            "tweet.read", "users.read", "tweet.write", "offline.access",
        ],
        capabilities=["posts.write"],
    )
    dispatch_store = SQLiteDispatchStore(store.DATA_PATH)
    adapter = XPublisherAdapter(
        transport or Transport([response(201, {"data": {"id": "x-42"}})]),
        lambda: "Bearer secret",
        reconciler=reconciler,
        base_url="https://x.test/2",
    )
    dispatcher = GovernedDispatcher(dispatch_store, {"x": adapter})
    dispatcher.set_connector_gate("x", healthy=True, write_enabled=True)
    item = dispatcher.create("x", {"body": "Hello"}, item_id="dispatch-1", brand_id=brand["id"])
    dispatcher.submit_for_approval(item.id, actor="agent")
    dispatcher.approve(item.id, revision=1, approver="preview-operator")
    queued = dispatcher.queue(item.id, actor="preview-operator")
    handler = XDeliveryJobHandler(dispatcher, account["id"])
    worker = JobWorker("x-worker", retry_base_seconds=0)
    register_x_delivery(worker, handler)
    return brand, account, dispatcher, queued, worker, adapter


def test_approved_queued_revision_publishes_and_enqueues_measurement_once(tmp_path):
    transport = Transport([response(201, {"data": {"id": "x-42", "text": "Hello"}})])
    brand, account, dispatcher, queued, worker, _ = setup_system(tmp_path, transport=transport)
    first = enqueue_x_delivery(queued, account["id"])
    duplicate = enqueue_x_delivery(queued, account["id"])
    assert duplicate["id"] == first["id"]

    result = worker.run_once()
    assert result["status"] == "completed"
    published = dispatcher.store.get(queued.id)
    assert published.status is Lifecycle.PUBLISHED
    assert published.external_id == "x-42"
    assert published.external_url == "https://x.com/i/web/status/x-42"
    assert transport.calls[0][2]["headers"]["Idempotency-Key"] == queued.idempotency_key
    assert transport.calls[0][2]["headers"]["Authorization"] == "Bearer secret"
    jobs = store.rows("SELECT * FROM durable_jobs WHERE job_type=?", (X_MEASUREMENT_JOB_TYPE,))
    assert len(jobs) == 1
    assert json.loads(jobs[0]["payload"])["external_id"] == "x-42"


@pytest.mark.parametrize("gate", ["kill", "unhealthy", "disabled"])
def test_governance_gates_make_no_external_call(tmp_path, gate):
    transport = Transport([response(201, {"data": {"id": "never"}})])
    _, account, dispatcher, queued, worker, _ = setup_system(tmp_path, transport=transport)
    if gate == "kill":
        dispatcher.set_kill_switch(True)
    else:
        dispatcher.set_connector_gate(
            "x", healthy=gate != "unhealthy", write_enabled=gate != "disabled"
        )
    enqueue_x_delivery(queued, account["id"], max_attempts=1)
    failed = worker.run_once()
    assert failed["status"] == "needs_attention"
    assert transport.calls == []
    assert dispatcher.store.get(queued.id).status is Lifecycle.QUEUED


@pytest.mark.parametrize(
    "status,scopes",
    [
        ("disconnected", ["tweet.read", "users.read", "tweet.write", "offline.access"]),
        ("healthy", ["tweet.read"]),
    ],
)
def test_connection_health_and_permission_are_checked_before_provider(tmp_path, status, scopes):
    transport = Transport([response(201, {"data": {"id": "never"}})])
    _, account, dispatcher, queued, worker, _ = setup_system(
        tmp_path, transport=transport, status=status, scopes=scopes
    )
    enqueue_x_delivery(queued, account["id"], max_attempts=1)
    failed = worker.run_once()
    assert failed["status"] == "needs_attention"
    assert transport.calls == []
    feedback = store.rows("SELECT * FROM product_feedback WHERE component='x.delivery'")
    assert len(feedback) == 1
    assert feedback[0]["severity"] == "high"


def test_stale_revision_job_never_publishes(tmp_path):
    transport = Transport([response(201, {"data": {"id": "never"}})])
    _, account, dispatcher, queued, worker, _ = setup_system(tmp_path, transport=transport)
    job = enqueue_x_delivery(queued, account["id"], max_attempts=1)
    dispatcher.edit(queued.id, {"body": "changed"}, actor="agent")
    failed = worker.run_once()
    assert failed["id"] == job["id"]
    assert failed["status"] == "needs_attention"
    assert transport.calls == []


def test_ambiguous_provider_success_is_reconciled_without_second_post(tmp_path):
    transport = Transport([TimeoutError("response lost after provider commit")])
    reconciled = []

    def reconcile(key, expected_external_id):
        reconciled.append((key, expected_external_id))
        return DispatchReceipt(
            ConnectorKind.X,
            "x-recovered",
            "https://x.test/x-recovered",
            dedup_identity(ConnectorKind.X, external_id="x-recovered"),
            "Hello",
        )

    _, account, dispatcher, queued, worker, _ = setup_system(
        tmp_path, transport=transport, reconciler=reconcile
    )
    enqueue_x_delivery(queued, account["id"])
    completed = worker.run_once()
    assert completed["status"] == "completed"
    assert dispatcher.store.get(queued.id).external_id == "x-recovered"
    assert len(transport.calls) == 1
    assert reconciled == [(queued.idempotency_key, None)]


def test_provider_permission_failure_is_visible_and_deduplicated_on_retry(tmp_path):
    transport = Transport([response(403, {}), response(403, {})])
    _, account, dispatcher, queued, worker, _ = setup_system(tmp_path, transport=transport)
    enqueue_x_delivery(queued, account["id"], max_attempts=2)
    assert worker.run_once()["status"] == "retry"
    assert worker.run_once(as_of="9999-12-31T00:00:00+00:00")["status"] == "needs_attention"
    feedback = store.rows(
        "SELECT * FROM product_feedback WHERE component='x.delivery' AND summary='X delivery permission failure'"
    )
    assert len(feedback) == 1
    assert feedback[0]["occurrence_count"] == 2
    assert dispatcher.store.get(queued.id).external_id is None


def test_terminal_x_provider_error_never_leaks_exception_text_to_feedback(tmp_path):
    transport = Transport([RuntimeError("Bearer x-provider-secret")])
    _, account, _, queued, worker, _ = setup_system(tmp_path, transport=transport)
    enqueue_x_delivery(queued, account["id"], max_attempts=1)
    assert worker.run_once()["status"] == "needs_attention"
    feedback = store.rows("SELECT * FROM product_feedback")
    assert {item["component"] for item in feedback} == {"x.delivery"}
    persisted = " ".join(
        str(item[field]) for item in feedback
        for field in ("details", "actual_behavior", "reproduction", "workaround")
    )
    assert "x-provider-secret" not in persisted


def test_retry_after_worker_restart_reuses_original_idempotency_key(tmp_path):
    transport = Transport([
        response(503, {}),
        response(201, {"data": {"id": "x-after-restart"}}),
    ])
    _, account, dispatcher, queued, worker, adapter = setup_system(
        tmp_path, transport=transport
    )
    enqueue_x_delivery(queued, account["id"], max_attempts=2)
    assert worker.run_once()["status"] == "retry"

    restarted_dispatcher = GovernedDispatcher(
        SQLiteDispatchStore(store.DATA_PATH), {"x": adapter}
    )
    restarted_dispatcher.set_connector_gate("x", healthy=True, write_enabled=True)
    restarted_worker = JobWorker("x-worker-after-restart", retry_base_seconds=0)
    register_x_delivery(
        restarted_worker,
        XDeliveryJobHandler(restarted_dispatcher, account["id"]),
    )
    completed = restarted_worker.run_once(as_of="9999-12-31T00:00:00+00:00")
    assert completed["status"] == "completed"
    assert restarted_dispatcher.store.get(queued.id).external_id == "x-after-restart"
    keys = [call[2]["headers"]["Idempotency-Key"] for call in transport.calls]
    assert keys == [queued.idempotency_key, queued.idempotency_key]


def test_enqueue_rejects_unapproved_item(tmp_path):
    store.DATA_PATH = tmp_path / "brand-os.db"
    store.init_db()
    brand = store.get_brand("demo-brand")
    dispatcher = GovernedDispatcher(SQLiteDispatchStore(store.DATA_PATH))
    draft = dispatcher.create("x", {"body": "No"}, brand_id=brand["id"])
    with pytest.raises(ValueError, match="queued"):
        enqueue_x_delivery(draft, "account")
