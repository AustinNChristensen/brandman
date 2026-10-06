from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app import store
from app.connectors import ConnectorEvent, ConnectorKind, EventKind, dedup_identity
from app.dispatch import GovernedDispatcher, Lifecycle, SQLiteDispatchStore
from app.engagement import (
    AntiSpamBlocked,
    AntiSpamPolicy,
    EngagementInbox,
    InvalidEngagementTransition,
)


NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)


def event(
    post_id="200",
    *,
    kind="mention",
    text="Can you explain this offer?",
    author_id="77",
    parent_text="Original post",
):
    return ConnectorEvent(
        ConnectorKind.X,
        EventKind.POST_PUBLISHED,
        dedup_identity(ConnectorKind.X, external_id=f"{kind}:{post_id}"),
        "2026-09-02T11:00:00+00:00",
        post_id,
        {
            "evidence_type": "x_engagement_opportunity",
            "opportunity_type": kind,
            "text": text,
            "author": {"id": author_id, "username": f"reader{author_id}", "verified": True},
            "conversation_id": "190",
            "in_reply_to_user_id": "42",
            "referenced_tweets": [{"type": "replied_to", "id": "190"}],
            "parent_context": [{"id": "190", "text": parent_text}],
            "public_metrics": {"reply_count": 0},
            "source_query": "points and miles" if kind == "search" else None,
            "target_user_id": author_id if kind == "target_account" else None,
            "requires_approval": True,
            "external_url": f"https://x.com/i/web/status/{post_id}",
        },
    )


def setup(tmp_path, *, policy=AntiSpamPolicy(), clock=lambda: NOW):
    database = tmp_path / "engagement.db"
    store.DATA_PATH = database
    store.init_db()
    brand = store.get_brand("demo-brand")
    inbox = EngagementInbox(database, clock=clock, anti_spam=policy)
    dispatcher = GovernedDispatcher(SQLiteDispatchStore(database))
    return brand, inbox, dispatcher


def project(inbox, brand, item):
    return inbox.project(
        brand_id=brand["id"], connector_account_id="x-read-account", event=item
    )


def test_projection_is_unique_ranked_and_preserves_context(tmp_path):
    brand, inbox, _ = setup(tmp_path)
    first = project(inbox, brand, event())
    repeated = project(inbox, brand, event())
    assert repeated["id"] == first["id"]
    assert repeated["resurfaced_count"] == 0
    assert first["requires_approval"] is True
    assert first["ranking_score"] == 80
    assert "direct question" in " ".join(first["ranking_reasons"])
    assert first["author"]["username"] == "reader77"
    assert first["thread_context"]["parent_context"][0]["text"] == "Original post"
    assert len(inbox.list(brand_id=brand["id"])) == 1
    assert [entry["action"] for entry in inbox.history(first["id"])] == ["ingested"]


def test_only_materially_new_context_resurfaces_terminal_or_drafted_items(tmp_path):
    brand, inbox, dispatcher = setup(tmp_path)
    opportunity = project(inbox, brand, event())
    inbox.dismiss(opportunity["id"], actor="chris")
    same = project(inbox, brand, event())
    assert same["state"] == "dismissed"
    original_payload = dict(event().payload)
    metric_only = replace(event(), payload={
        **original_payload,
        "author": {**original_payload["author"], "public_metrics": {"followers_count": 999}},
        "public_metrics": {"reply_count": 8},
    })
    assert project(inbox, brand, metric_only)["state"] == "dismissed"
    changed = project(inbox, brand, event(parent_text="A corrected parent post"))
    assert changed["state"] == "new"
    assert changed["resurfaced_count"] == 1

    drafted, _ = inbox.draft_action(
        changed["id"], "reply", dispatcher, actor="agent", text="Here is the answer."
    )
    newer = project(inbox, brand, event(text="Can you explain the updated offer?", parent_text="A corrected parent post"))
    assert drafted["state"] == "drafted"
    assert newer["state"] == "needs_attention"
    assert newer["resurfaced_count"] == 2
    with pytest.raises(InvalidEngagementTransition, match="linked action is stale"):
        inbox.draft_action(
            newer["id"], "reply", dispatcher, actor="chris",
            text="A revised answer must not silently reuse the old draft.",
        )


@pytest.mark.parametrize(
    "kind,expected_connector",
    [("reply", "x"), ("like", "x.like"), ("follow", "x.follow")],
)
def test_reply_like_follow_actions_have_one_canonical_dispatch_link(
    tmp_path, kind, expected_connector
):
    brand, inbox, dispatcher = setup(tmp_path)
    opportunity = project(inbox, brand, event())
    drafted, dispatch = inbox.draft_action(
        opportunity["id"], kind, dispatcher, actor="agent",
        text="Specific, useful answer." if kind == "reply" else None,
    )
    assert drafted["dispatch_item_id"] == dispatch.id
    assert drafted["action_type"] == kind
    assert dispatch.connector == expected_connector
    assert dispatch.brand_id == brand["id"]
    repeated, same_dispatch = inbox.draft_action(
        opportunity["id"], kind, dispatcher, actor="agent",
        text="Specific, useful answer." if kind == "reply" else None,
    )
    assert repeated["id"] == drafted["id"]
    assert same_dispatch.id == dispatch.id


def test_reply_moves_to_approval_but_cannot_be_acted_on_until_published(tmp_path):
    brand, inbox, dispatcher = setup(tmp_path)
    opportunity = project(inbox, brand, event())
    drafted, dispatch = inbox.draft_action(
        opportunity["id"], "reply", dispatcher, actor="agent", text="The fee is $95."
    )
    awaiting, submitted = inbox.submit_action_for_approval(
        drafted["id"], dispatcher, actor="agent"
    )
    assert awaiting["state"] == "awaiting_approval"
    assert submitted.status is Lifecycle.AWAITING_APPROVAL
    with pytest.raises(InvalidEngagementTransition, match="published"):
        inbox.record_result(awaiting["id"], dispatcher, {"external_id": "300"}, actor="worker")

    dispatcher.store.mutate(
        dispatch.id,
        lambda item: replace(
            item, status=Lifecycle.PUBLISHED, external_id="300",
            external_url="https://x.com/i/web/status/300",
        ),
    )
    acted = inbox.record_result(
        awaiting["id"], dispatcher,
        {"external_id": "300", "outcome": "reply_published"}, actor="worker",
    )
    assert acted["state"] == "acted_on"
    assert acted["result"]["external_id"] == "300"
    assert "acted_on" in [entry["action"] for entry in inbox.history(acted["id"])]


def test_similarity_guard_blocks_repetitive_replies_and_records_reason(tmp_path):
    policy = AntiSpamPolicy(
        max_actions_per_author_24h=10, max_actions_per_hour=10,
        similarity_threshold=0.8,
    )
    brand, inbox, dispatcher = setup(tmp_path, policy=policy)
    one = project(inbox, brand, event("201", author_id="70"))
    inbox.draft_action(
        one["id"], "reply", dispatcher, actor="agent",
        text="This offer is useful if you can use the credit.",
    )
    two = project(inbox, brand, event("202", author_id="71"))
    with pytest.raises(AntiSpamBlocked, match="too similar"):
        inbox.draft_action(
            two["id"], "reply", dispatcher, actor="agent",
            text="This offer is useful if you can use the credit!",
        )
    assert inbox.get(two["id"])["state"] == "needs_attention"
    assert "anti_spam_blocked" in [entry["action"] for entry in inbox.history(two["id"])]


def test_per_author_and_global_rate_guards(tmp_path):
    policy = AntiSpamPolicy(
        max_actions_per_author_24h=1, max_actions_per_hour=2,
        similarity_threshold=1.1,
    )
    brand, inbox, dispatcher = setup(tmp_path, policy=policy)
    first = project(inbox, brand, event("201", author_id="70"))
    inbox.draft_action(first["id"], "like", dispatcher, actor="agent")
    same_author = project(inbox, brand, event("202", author_id="70"))
    with pytest.raises(AntiSpamBlocked, match="per-author"):
        inbox.draft_action(same_author["id"], "like", dispatcher, actor="agent")

    other = project(inbox, brand, event("203", author_id="71"))
    inbox.draft_action(other["id"], "like", dispatcher, actor="agent")
    third = project(inbox, brand, event("204", author_id="72"))
    with pytest.raises(AntiSpamBlocked, match="hourly"):
        inbox.draft_action(third["id"], "like", dispatcher, actor="agent")


@pytest.mark.parametrize("kind", ["mention", "reply", "search", "target_account"])
def test_all_supported_opportunity_types_persist(tmp_path, kind):
    brand, inbox, _ = setup(tmp_path)
    saved = project(inbox, brand, event(kind=kind))
    assert saved["opportunity_type"] == kind


def test_rejects_ungoverned_or_non_x_events(tmp_path):
    brand, inbox, _ = setup(tmp_path)
    invalid = replace(event(), connector=ConnectorKind.RSS)
    with pytest.raises(ValueError, match="governed X"):
        project(inbox, brand, invalid)
    invalid = replace(event(), payload={**event().payload, "requires_approval": False})
    with pytest.raises(ValueError, match="governed X"):
        project(inbox, brand, invalid)


def test_schema_initialization_is_migration_safe(tmp_path):
    database = tmp_path / "engagement.db"
    first = EngagementInbox(database)
    second = EngagementInbox(database)
    assert first.list(brand_id="none") == second.list(brand_id="none") == []
