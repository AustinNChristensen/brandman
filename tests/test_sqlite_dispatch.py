from __future__ import annotations

from dataclasses import replace
from threading import Event, Thread

import pytest

from app.dispatch import DispatchBlocked, DuplicateDispatch, Lifecycle, PublishResult
from app.sqlite_dispatch import SQLiteDispatchStore, SQLiteGovernedDispatcher


class Publisher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, item, key):
        self.calls.append(key)
        return PublishResult("x-42", "https://x.test/x-42")


def approved_and_queued(dispatcher):
    item = dispatcher.create("x", {"body": "hello"}, item_id="post-1")
    item = dispatcher.submit_for_approval(item.id, actor="agent")
    dispatcher.approve(item.id, revision=item.revision, approver="preview-operator")
    return dispatcher.queue(item.id)


def test_round_trip_approval_audit_and_publish_survive_restart(tmp_path):
    database = tmp_path / "dispatch.sqlite3"
    publisher = Publisher()
    first = SQLiteGovernedDispatcher(SQLiteDispatchStore(database), {"x": publisher})
    first.set_connector_gate("x", healthy=True, write_enabled=True)
    queued = approved_and_queued(first)

    second_store = SQLiteDispatchStore(database)
    second = SQLiteGovernedDispatcher(second_store, {"x": publisher})
    loaded = second_store.get(queued.id)
    assert loaded.approval is not None
    assert loaded.approval.approver == "preview-operator"
    assert loaded.idempotency_key == queued.idempotency_key

    published = second.dispatch(queued.id)
    assert published.status is Lifecycle.PUBLISHED
    assert published.external_id == "x-42"
    assert published.external_url == "https://x.test/x-42"
    assert published.attempt_count == 1
    assert [event.action for event in second_store.list_audit(queued.id)] == [
        "created",
        "awaiting_approval",
        "approved",
        "queued",
        "published",
    ]

    third = SQLiteGovernedDispatcher(SQLiteDispatchStore(database), {"x": publisher})
    with pytest.raises(DuplicateDispatch):
        third.dispatch(queued.id)
    assert publisher.calls == [queued.idempotency_key]


def test_atomic_claim_across_dispatcher_instances(tmp_path):
    database = tmp_path / "dispatch.sqlite3"
    entered = Event()
    release = Event()

    def slow_publisher(item, key):
        entered.set()
        assert release.wait(timeout=2)
        return PublishResult("external-once")

    first = SQLiteGovernedDispatcher(SQLiteDispatchStore(database), {"x": slow_publisher})
    first.set_connector_gate("x", healthy=True, write_enabled=True)
    queued = approved_and_queued(first)
    second = SQLiteGovernedDispatcher(SQLiteDispatchStore(database), {"x": slow_publisher})

    result = []
    worker = Thread(target=lambda: result.append(first.dispatch(queued.id)))
    worker.start()
    assert entered.wait(timeout=2)
    with pytest.raises(DuplicateDispatch, match="already being dispatched"):
        second.dispatch(queued.id)
    release.set()
    worker.join(timeout=2)
    assert result[0].external_id == "external-once"


def test_persisted_gates_and_global_kill_switch(tmp_path):
    database = tmp_path / "dispatch.sqlite3"
    first = SQLiteGovernedDispatcher(SQLiteDispatchStore(database), {"x": Publisher()})
    first.set_connector_gate("x", healthy=True, write_enabled=True, detail="ready")
    first.set_kill_switch(True)
    queued = approved_and_queued(first)

    store = SQLiteDispatchStore(database)
    second = SQLiteGovernedDispatcher(store, {"x": Publisher()})
    assert store.get_connector_gate("x").detail == "ready"
    assert store.get_kill_switch() is True
    with pytest.raises(DispatchBlocked, match="kill switch"):
        second.dispatch(queued.id)

    second.set_kill_switch(False)
    assert second.dispatch(queued.id).status is Lifecycle.PUBLISHED


def test_orphan_claim_can_be_reconciled_without_changing_stable_key(tmp_path):
    store = SQLiteDispatchStore(tmp_path / "dispatch.sqlite3")
    dispatcher = SQLiteGovernedDispatcher(store, {"x": Publisher()})
    dispatcher.set_connector_gate("x", healthy=True, write_enabled=True)
    queued = approved_and_queued(dispatcher)
    claimed = store.mutate(
        queued.id,
        lambda item: replace(item, dispatch_claim="dead-worker", attempt_count=1),
    )

    recovered = SQLiteDispatchStore(store.database).release_dispatch_claim(
        claimed.id, "dead-worker", error="reconciled after worker restart"
    )
    assert recovered.status is Lifecycle.NEEDS_ATTENTION
    assert recovered.dispatch_claim is None
    assert recovered.idempotency_key == queued.idempotency_key
    assert dispatcher.dispatch(queued.id).attempt_count == 2


def test_duplicate_ids_and_unknown_items_match_store_contract(tmp_path):
    store = SQLiteDispatchStore(tmp_path / "dispatch.sqlite3")
    dispatcher = SQLiteGovernedDispatcher(store)
    dispatcher.create("x", {"body": "one"}, item_id="same")
    with pytest.raises(KeyError):
        dispatcher.create("x", {"body": "two"}, item_id="same")
    with pytest.raises(KeyError, match="unknown dispatch item"):
        store.get("missing")


def test_schema_initialization_is_idempotent(tmp_path):
    database = tmp_path / "dispatch.sqlite3"
    SQLiteDispatchStore(database)
    SQLiteDispatchStore(database)
