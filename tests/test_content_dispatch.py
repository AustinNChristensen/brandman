from __future__ import annotations

import sqlite3

import pytest

from app import store
from app.attribution_store import AttributionStore
from app.content_dispatch import CanonicalPostDispatchService, CanonicalPostNotFound
from app.dispatch import (
    DispatchItem,
    DispatchValidationError,
    GovernedDispatcher,
    InMemoryDispatchStore,
    Lifecycle,
    SQLiteDispatchStore,
    validate_payload,
)


def canonical_post(tmp_path, *, body="A useful pricing insight", channel="x"):
    store.DATA_PATH = tmp_path / "content.db"
    store.init_db()
    brand = store.get_brand("demo-brand")
    campaign = store.insert(
        "campaigns",
        {
            "brand_id": brand["id"],
            "source_id": None,
            "name": "Campaign",
            "objective": "Growth",
            "status": "active",
        },
    )
    post = store.insert(
        "posts",
        {
            "campaign_id": campaign["id"],
            "channel": channel,
            "body": body,
            "status": "draft",
            "scheduled_for": None,
            "external_post_id": None,
        },
    )
    dispatcher = GovernedDispatcher(SQLiteDispatchStore(store.DATA_PATH))
    return brand, post, dispatcher


def test_create_from_post_is_idempotent_and_persists_canonical_link(tmp_path):
    brand, post, dispatcher = canonical_post(tmp_path)
    service = CanonicalPostDispatchService(dispatcher)
    first = service.create_from_post(post["id"])
    repeated = service.create_from_post(post["id"])

    assert repeated.id == first.id
    assert first.brand_id == brand["id"]
    assert first.canonical_post_id == post["id"]
    assert first.payload == {"body": post["body"]}
    assert first.revision == 1
    reloaded = SQLiteDispatchStore(store.DATA_PATH).get(first.id)
    assert reloaded.canonical_post_id == post["id"]


def test_dispatch_uses_persisted_canonical_tracked_link(tmp_path):
    brand, post, dispatcher = canonical_post(
        tmp_path, body="Read https://demo.example/deal?utm_source=old now",
    )
    attribution = AttributionStore(store.DATA_PATH)
    item = CanonicalPostDispatchService(dispatcher, attribution=attribution).create_from_post(post["id"])
    links = attribution.list_tracked_links(brand["id"])
    assert len(links) == 1
    assert links[0]["tracked_url"] in item.payload["body"]
    assert "utm_source=x" in item.payload["body"]
    assert "utm_source=old" not in item.payload["body"]
    repeated = CanonicalPostDispatchService(dispatcher, attribution=attribution).create_from_post(post["id"])
    assert repeated.id == item.id
    assert len(attribution.list_tracked_links(brand["id"])) == 1


def test_canonical_body_edit_advances_revision_and_invalidates_approval(tmp_path):
    _, post, dispatcher = canonical_post(tmp_path)
    service = CanonicalPostDispatchService(dispatcher)
    item = service.create_from_post(post["id"])
    dispatcher.submit_for_approval(item.id, actor="agent")
    dispatcher.approve(item.id, revision=1, approver="chris")
    with store.connection() as connection:
        connection.execute(
            "UPDATE posts SET body=?, updated_at=? WHERE id=?",
            ("A materially better hook", store.now(), post["id"]),
        )

    refreshed = service.create_from_post(post["id"], actor="agent")
    assert refreshed.id == item.id
    assert refreshed.revision == 2
    assert refreshed.payload == {"body": "A materially better hook"}
    assert refreshed.status is Lifecycle.DRAFT
    assert refreshed.approval is None


def test_published_content_edit_creates_next_linked_revision(tmp_path):
    _, post, dispatcher = canonical_post(tmp_path)
    service = CanonicalPostDispatchService(dispatcher)
    first = service.create_from_post(post["id"])
    # Simulate the terminal state without crossing a provider boundary.
    dispatcher.store.mutate(first.id, lambda item: DispatchItem(
        id=item.id, connector=item.connector, payload=item.payload,
        brand_id=item.brand_id, canonical_post_id=item.canonical_post_id,
        status=Lifecycle.PUBLISHED, revision=item.revision,
        external_id="123", updated_at=item.updated_at,
    ))
    with store.connection() as connection:
        connection.execute("UPDATE posts SET body=? WHERE id=?", ("New angle", post["id"]))

    second = service.create_from_post(post["id"])
    assert second.id != first.id
    assert second.revision == 2
    assert second.canonical_post_id == post["id"]
    assert second.payload == {"body": "New angle"}


def test_legacy_matching_approval_candidate_is_reconciled_not_duplicated(tmp_path):
    _, post, dispatcher = canonical_post(tmp_path)
    legacy = dispatcher.create("x", {"body": post["body"]}, brand_id=store.get_brand("demo-brand")["id"])
    dispatcher.submit_for_approval(legacy.id, actor="legacy-agent")

    reconciled = CanonicalPostDispatchService(dispatcher).create_from_post(post["id"])

    assert reconciled.id == legacy.id
    assert reconciled.canonical_post_id == post["id"]
    assert reconciled.status is Lifecycle.AWAITING_APPROVAL
    assert len([
        item for item in dispatcher.store.list_items()
        if item.canonical_post_id == post["id"] and item.status is not Lifecycle.CANCELLED
    ]) == 1


def test_existing_canonical_dispatch_cancels_matching_legacy_duplicate(tmp_path):
    brand, post, dispatcher = canonical_post(tmp_path)
    service = CanonicalPostDispatchService(dispatcher)
    canonical = service.create_from_post(post["id"])
    legacy = dispatcher.create("x", {"body": post["body"]}, brand_id=brand["id"])
    dispatcher.submit_for_approval(legacy.id, actor="legacy-agent")

    repeated = service.create_from_post(post["id"])

    assert repeated.id == canonical.id
    assert dispatcher.store.get(legacy.id).status is Lifecycle.CANCELLED


def test_attributed_payload_linked_duplicate_preserves_tracking_and_invalidates_approval(tmp_path):
    brand, post, dispatcher = canonical_post(
        tmp_path, body="A useful pricing insight https://example.test/story",
    )
    service = CanonicalPostDispatchService(dispatcher)
    canonical = service.create_from_post(post["id"])
    dispatcher.submit_for_approval(canonical.id, actor="agent")
    dispatcher.approve(canonical.id, revision=1, approver="chris")
    tracked_url = (
        "https://example.test/story?utm_source=x&utm_medium=organic-social&utm_campaign="
        + post["campaign_id"] + "&utm_content=" + post["id"] + "&utm_cta=read"
    )
    tracked = "A useful pricing insight " + tracked_url
    legacy = dispatcher.create("x", {
        "body": tracked, "tracked_url": tracked_url,
        "canonical_post_id": post["id"], "campaign_id": post["campaign_id"],
        "cta_id": "read",
    }, brand_id=brand["id"])

    reconciled = service.create_from_post(post["id"], actor="migration")

    assert reconciled.id == canonical.id
    assert reconciled.payload == {"body": tracked}
    assert reconciled.revision == 2
    assert reconciled.status is Lifecycle.DRAFT
    assert reconciled.approval is None
    assert dispatcher.store.get(legacy.id).status is Lifecycle.CANCELLED
    assert "approval invalidated" in dispatcher.store.list_audit(canonical.id)[-1].action


def test_payload_identity_or_attribution_mismatch_is_not_auto_reconciled(tmp_path):
    brand, post, dispatcher = canonical_post(tmp_path)
    service = CanonicalPostDispatchService(dispatcher)
    canonical = service.create_from_post(post["id"])
    malicious = dispatcher.create("x", {
        "body": post["body"] + "?utm_source=x&utm_medium=organic-social&utm_campaign=wrong&utm_content=" + post["id"],
        "tracked_url": post["body"].split()[-1] + "?utm_source=x&utm_medium=organic-social&utm_campaign=wrong&utm_content=" + post["id"],
        "canonical_post_id": post["id"], "campaign_id": post["campaign_id"],
    }, brand_id=brand["id"])
    canonical_row = store.row(
        """SELECT p.*,c.brand_id,c.id AS canonical_campaign_id,b.slug AS brand_slug
           FROM posts p JOIN campaigns c ON c.id=p.campaign_id
           JOIN brands b ON b.id=c.brand_id WHERE p.id=?""", (post["id"],),
    )
    assert service.reconcile_legacy_attribution_duplicate(
        canonical_row, post["body"], actor="migration", apply=False,
    ) is None
    assert dispatcher.store.get(canonical.id).status is Lifecycle.DRAFT
    assert dispatcher.store.get(malicious.id).status is Lifecycle.DRAFT


def test_database_enforces_unique_connector_post_revision(tmp_path):
    _, post, dispatcher = canonical_post(tmp_path)
    dispatcher.create(
        "x", {"body": "one"}, item_id="one",
        canonical_post_id=post["id"], revision=1,
    )
    with pytest.raises(KeyError):
        dispatcher.create(
            "x", {"body": "duplicate"}, item_id="two",
            canonical_post_id=post["id"], revision=1,
        )


@pytest.mark.parametrize(
    "payload,expected_error",
    [
        ({"body": ""}, "nonempty"),
        ({"body": "x" * 281}, "maximum is 280"),
        ({"body": "hello", "reply_to_post_id": "not-an-id"}, "numeric X post ID"),
        ({"body": "hello", "surprise": True}, "unsupported X payload keys"),
        ({"body": "hello", "text": "duplicate"}, "either text or body"),
    ],
)
def test_invalid_x_payload_cannot_enter_approval(payload, expected_error):
    dispatcher = GovernedDispatcher(InMemoryDispatchStore())
    item = dispatcher.create("x", payload)
    with pytest.raises(DispatchValidationError, match=expected_error) as caught:
        dispatcher.submit_for_approval(item.id, actor="agent")
    assert caught.value.summary.valid is False
    assert dispatcher.store.get(item.id).status is Lifecycle.DRAFT


def test_x_validation_summary_uses_effective_url_length():
    summary = validate_payload("x", {"text": "Read https://example.com/" + "a" * 500})
    assert summary.valid
    assert summary.effective_length == len("Read ") + 23


def test_non_x_post_and_missing_post_are_rejected(tmp_path):
    _, post, dispatcher = canonical_post(tmp_path, channel="newsletter")
    service = CanonicalPostDispatchService(dispatcher)
    with pytest.raises(ValueError, match="canonical X"):
        service.create_from_post(post["id"])
    with pytest.raises(CanonicalPostNotFound):
        service.create_from_post("missing")


def test_legacy_dispatch_table_is_migrated(tmp_path):
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TABLE dispatch_items (
        id TEXT PRIMARY KEY, brand_id TEXT, connector TEXT NOT NULL, payload TEXT NOT NULL,
        status TEXT NOT NULL, revision INTEGER NOT NULL, approval_approver TEXT,
        approval_revision INTEGER, approval_at TEXT, approval_batch_id TEXT,
        idempotency_key TEXT, external_id TEXT, external_url TEXT, attempt_count INTEGER,
        dispatch_claim TEXT, last_error TEXT, updated_at TEXT NOT NULL)"""
    )
    connection.commit()
    connection.close()
    SQLiteDispatchStore(database)
    columns = {
        row[1] for row in sqlite3.connect(database).execute("PRAGMA table_info(dispatch_items)")
    }
    assert "canonical_post_id" in columns
