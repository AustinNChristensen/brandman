from __future__ import annotations

from threading import Event, Thread

import pytest

from brandman.dispatch import (
    ApprovalRequired,
    DispatchBlocked,
    DuplicateDispatch,
    GovernedDispatcher,
    InMemoryDispatchStore,
    InvalidTransition,
    Lifecycle,
    PublishResult,
    RevisionMismatch,
)


class FakePublisher:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[str] = []

    def __call__(self, item, idempotency_key: str) -> PublishResult:
        self.calls.append(idempotency_key)
        if len(self.calls) <= self.failures:
            raise TimeoutError("temporary timeout")
        return PublishResult("x-123", "https://x.test/status/x-123")


def setup_dispatcher(publisher=None):
    store = InMemoryDispatchStore()
    dispatcher = GovernedDispatcher(store, {"x": publisher or FakePublisher()})
    dispatcher.set_connector_gate("x", healthy=True, write_enabled=True)
    return dispatcher, store


def approved_item(dispatcher):
    item = dispatcher.create("x", {"body": "hello"}, item_id="post-1")
    item = dispatcher.submit_for_approval(item.id, actor="agent")
    return dispatcher.approve(item.id, revision=item.revision, approver="chris")


def test_full_lifecycle_and_audit_history():
    dispatcher, store = setup_dispatcher()
    item = approved_item(dispatcher)
    assert item.status is Lifecycle.APPROVED
    item = dispatcher.queue(item.id)
    assert item.status is Lifecycle.QUEUED
    item = dispatcher.dispatch(item.id)
    assert item.status is Lifecycle.PUBLISHED
    assert item.external_id == "x-123"
    assert item.external_url == "https://x.test/status/x-123"
    assert dispatcher.mark_measured(item.id).status is Lifecycle.MEASURED
    assert [event.action for event in store.audit] == [
        "created", "awaiting_approval", "approved", "queued", "published", "measured"
    ]


def test_edit_increments_revision_and_invalidates_approval():
    dispatcher, _ = setup_dispatcher()
    item = approved_item(dispatcher)
    edited = dispatcher.edit(item.id, {"body": "materially changed"}, actor="chris")
    assert edited.revision == 2
    assert edited.status is Lifecycle.DRAFT
    assert edited.approval is None
    with pytest.raises(ApprovalRequired):
        dispatcher.queue(edited.id)
    dispatcher.submit_for_approval(edited.id, actor="agent")
    with pytest.raises(RevisionMismatch):
        dispatcher.approve(edited.id, revision=1, approver="chris")


def test_batch_is_explicit_revision_bound_and_prevalidated():
    dispatcher, _ = setup_dispatcher()
    first = dispatcher.submit_for_approval(dispatcher.create("x", {"body": "one"}, item_id="one").id, actor="agent")
    second = dispatcher.submit_for_approval(dispatcher.create("x", {"body": "two"}, item_id="two").id, actor="agent")
    with pytest.raises(RevisionMismatch):
        dispatcher.approve_batch({first.id: 1, second.id: 99}, approver="chris")
    assert dispatcher.store.get(first.id).status is Lifecycle.AWAITING_APPROVAL
    approved = dispatcher.approve_batch({first.id: 1, second.id: 1}, approver="chris", batch_id="morning-batch")
    assert all(item.approval and item.approval.batch_id == "morning-batch" for item in approved)


def test_rejection_requires_current_revision_and_cannot_be_queued():
    dispatcher, _ = setup_dispatcher()
    item = dispatcher.submit_for_approval(dispatcher.create("x", {"body": "no"}).id, actor="agent")
    with pytest.raises(RevisionMismatch):
        dispatcher.reject(item.id, revision=2, actor="chris")
    rejected = dispatcher.reject(item.id, revision=1, actor="chris")
    assert rejected.status is Lifecycle.REJECTED
    with pytest.raises(ApprovalRequired):
        dispatcher.queue(item.id)


@pytest.mark.parametrize(
    "healthy,write_enabled,kill_switch",
    [(False, True, False), (True, False, False), (True, True, True)],
)
def test_connector_and_global_write_gates(healthy, write_enabled, kill_switch):
    publisher = FakePublisher()
    dispatcher, _ = setup_dispatcher(publisher)
    item = dispatcher.queue(approved_item(dispatcher).id)
    dispatcher.set_connector_gate("x", healthy=healthy, write_enabled=write_enabled)
    dispatcher.set_kill_switch(kill_switch)
    with pytest.raises(DispatchBlocked):
        dispatcher.dispatch(item.id)
    assert publisher.calls == []


def test_retry_reuses_idempotency_key_and_publish_once_is_enforced():
    publisher = FakePublisher(failures=1)
    dispatcher, _ = setup_dispatcher(publisher)
    item = dispatcher.queue(approved_item(dispatcher).id)
    first_key = item.idempotency_key
    failed = dispatcher.dispatch(item.id)
    assert failed.status is Lifecycle.FAILED
    assert failed.last_error == "temporary timeout"
    published = dispatcher.dispatch(item.id)
    assert published.status is Lifecycle.PUBLISHED
    assert published.attempt_count == 2
    assert publisher.calls == [first_key, first_key]
    with pytest.raises(DuplicateDispatch):
        dispatcher.dispatch(item.id)
    assert len(publisher.calls) == 2


def test_concurrent_workers_cannot_both_cross_publish_boundary():
    entered = Event()
    release = Event()

    def slow_publisher(item, idempotency_key):
        entered.set()
        assert release.wait(timeout=2)
        return PublishResult("x-concurrent")

    dispatcher, _ = setup_dispatcher(slow_publisher)
    item = dispatcher.queue(approved_item(dispatcher).id)
    result = []
    worker = Thread(target=lambda: result.append(dispatcher.dispatch(item.id)))
    worker.start()
    assert entered.wait(timeout=2)
    with pytest.raises(DuplicateDispatch, match="already being dispatched"):
        dispatcher.dispatch(item.id)
    release.set()
    worker.join(timeout=2)
    assert result[0].external_id == "x-concurrent"


def test_missing_publisher_is_visible_and_cancel_is_terminal():
    store = InMemoryDispatchStore()
    dispatcher = GovernedDispatcher(store)
    dispatcher.set_connector_gate("x", healthy=True, write_enabled=True)
    item = dispatcher.create("x", {"body": "hello"})
    dispatcher.submit_for_approval(item.id, actor="agent")
    dispatcher.approve(item.id, revision=1, approver="chris")
    dispatcher.queue(item.id)
    attention = dispatcher.dispatch(item.id)
    assert attention.status is Lifecycle.NEEDS_ATTENTION
    assert "not configured" in (attention.last_error or "")
    cancelled = dispatcher.cancel(item.id, actor="chris")
    assert cancelled.status is Lifecycle.CANCELLED
    with pytest.raises(InvalidTransition):
        dispatcher.edit(item.id, {"body": "late edit"}, actor="chris")
