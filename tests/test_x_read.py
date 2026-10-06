from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest

from app.connectors import EventKind, HttpResponse, SyncCursor
from app.x_read import (
    XRateLimitError,
    XReadConnector,
    XReadCursorError,
    XReadScopeError,
    required_x_read_scopes,
)


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def response(document, status=200, headers=None):
    return HttpResponse(status, json.dumps(document).encode(), headers or {})


def connector(transport, **kwargs):
    return XReadConnector(
        "42", "demobrand", transport, lambda: "Bearer read-secret",
        granted_scopes=["tweet.read", "users.read", "offline.access"],
        base_url="https://x.test/2",
        clock=lambda: datetime(2026, 9, 2, 12, tzinfo=UTC),
        **kwargs,
    )


def test_requires_least_privilege_read_scopes_and_never_calls_transport():
    transport = Transport([])
    with pytest.raises(XReadScopeError, match="missing read scopes"):
        XReadConnector(
            "42", "demobrand", transport, lambda: "secret",
            granted_scopes=["tweet.read"],
        )
    with pytest.raises(XReadScopeError, match="write scopes"):
        XReadConnector(
            "42", "demobrand", transport, lambda: "secret",
            granted_scopes=["tweet.read", "users.read", "offline.access", "tweet.write"],
        )
    with pytest.raises(XReadScopeError, match="unnecessary read scopes"):
        XReadConnector(
            "42", "demobrand", transport, lambda: "secret",
            granted_scopes=["tweet.read", "users.read", "offline.access", "follows.read"],
        )
    assert required_x_read_scopes() == ("offline.access", "tweet.read", "users.read")
    assert transport.calls == []


def test_profile_metrics_provide_mission_kpi_evidence():
    transport = Transport([response({
        "data": {
            "id": "42", "username": "demobrand",
            "public_metrics": {
                "followers_count": 11, "following_count": 8,
                "tweet_count": 20, "listed_count": 1,
            },
        }
    })])
    result = connector(
        transport, include_tweet_metrics=False, include_mentions=False,
        include_replies=False,
    ).sync()
    event = result.events[0]
    assert event.kind is EventKind.METRIC_OBSERVED
    assert event.payload["evidence_type"] == "x_profile_metrics"
    assert event.payload["metric"] == "x_followers"
    assert event.payload["value"] == 11
    assert event.occurred_at == "2026-09-02T12:00:00+00:00"
    assert result.has_more is False
    assert transport.calls[0][0] == "GET"


def test_tweet_public_private_organic_metrics_are_normalized_and_paginated():
    first = response({
        "data": [{
            "id": "100", "text": "A post", "author_id": "42",
            "created_at": "2026-09-01T10:00:00Z",
            "public_metrics": {
                "impression_count": 100, "like_count": 4, "reply_count": 2,
                "retweet_count": 1, "quote_count": 1, "bookmark_count": 2,
            },
            "non_public_metrics": {"url_link_clicks": 7, "user_profile_clicks": 3},
            "organic_metrics": {"impression_count": 90},
            "promoted_metrics": {"impression_count": 10},
        }],
        "includes": {"users": [{"id": "42", "username": "demobrand"}]},
        "meta": {"next_token": "page-two"},
    })
    second = response({"data": [], "meta": {}})
    transport = Transport([first, second])
    reader = connector(
        transport, include_profile_metrics=False, include_mentions=False,
        include_replies=False,
    )
    page_one = reader.sync()
    event = page_one.events[0]
    assert event.payload["impressions"] == 100
    assert event.payload["clicks"] == 7
    assert event.payload["engagements"] == 10
    assert event.payload["private_metrics"]["user_profile_clicks"] == 3
    assert event.payload["organic_metrics"]["impression_count"] == 90
    assert page_one.has_more
    page_two = reader.sync(page_one.next_cursor)
    assert not page_two.has_more
    assert transport.calls[1][2]["params"]["pagination_token"] == "page-two"
    assert all(call[0] == "GET" for call in transport.calls)


def test_metric_snapshot_dedup_is_stable_and_changes_with_metrics():
    document = {"data": [{"id": "100", "public_metrics": {"impression_count": 10}}]}
    reader = connector(
        Transport([response(document), response(document)]),
        include_profile_metrics=False, include_mentions=False, include_replies=False,
    )
    first = reader.sync().events[0]
    second = reader.sync().events[0]
    assert first.dedup_key == second.dedup_key
    changed = connector(
        Transport([response({"data": [{"id": "100", "public_metrics": {"impression_count": 11}}]})]),
        include_profile_metrics=False, include_mentions=False, include_replies=False,
    ).sync().events[0]
    assert changed.dedup_key != first.dedup_key


def test_mentions_retain_author_thread_and_parent_context():
    transport = Transport([response({
        "data": [{
            "id": "200", "text": "Is this card worth it?", "author_id": "77",
            "created_at": "2026-09-02T11:00:00Z", "conversation_id": "190",
            "in_reply_to_user_id": "42",
            "referenced_tweets": [{"type": "replied_to", "id": "190"}],
            "public_metrics": {"reply_count": 0},
        }],
        "includes": {
            "users": [{"id": "77", "username": "reader", "verified": False}],
            "tweets": [{"id": "190", "text": "Original Demo Brand post", "author_id": "42"}],
        },
    })])
    event = connector(
        transport, include_profile_metrics=False, include_tweet_metrics=False,
        include_replies=False,
    ).sync().events[0]
    assert event.kind is EventKind.POST_PUBLISHED
    assert event.payload["opportunity_type"] == "mention"
    assert event.payload["author"]["username"] == "reader"
    assert event.payload["conversation_id"] == "190"
    assert event.payload["parent_context"][0]["text"] == "Original Demo Brand post"
    assert event.payload["requires_approval"] is True


def test_replies_search_and_configured_search_target_endpoints_advance_cursor():
    transport = Transport([
        response({"data": []}),
        response({"data": [{"id": "301", "text": "pricing change", "author_id": "80"}]}),
        response({"data": [{"id": "302", "text": "target account post", "author_id": "99"}]}),
    ])
    reader = connector(
        transport, include_profile_metrics=False, include_tweet_metrics=False,
        include_mentions=False, searches=["product launch -is:retweet"],
        target_user_ids=["99"],
    )
    replies = reader.sync()
    assert transport.calls[0][2]["params"]["query"] == "to:demobrand -from:demobrand"
    searched = reader.sync(replies.next_cursor)
    assert searched.events[0].payload["opportunity_type"] == "search"
    assert searched.events[0].payload["source_query"] == "product launch -is:retweet"
    targeted = reader.sync(searched.next_cursor)
    assert targeted.events[0].payload["opportunity_type"] == "target_account"
    assert targeted.events[0].payload["target_user_id"] == "99"
    assert transport.calls[2][1].endswith("/users/99/tweets")
    assert "target_user_id" not in transport.calls[2][2]["params"]
    assert targeted.next_cursor is None


def test_rate_limit_and_provider_errors_are_credential_safe():
    limited = connector(
        Transport([response({}, 429, {"retry-after": "60", "x-rate-limit-reset": "1788360000"})]),
        include_tweet_metrics=False, include_mentions=False, include_replies=False,
    )
    with pytest.raises(XRateLimitError) as caught:
        limited.sync()
    assert caught.value.retry_after_seconds == 60
    assert caught.value.reset_at == 1788360000
    assert "read-secret" not in str(caught.value)
    assert "read-secret" not in repr(limited)


def test_invalid_or_stale_cursor_is_rejected_without_request():
    transport = Transport([])
    reader = connector(
        transport, include_tweet_metrics=False, include_mentions=False,
        include_replies=False,
    )
    with pytest.raises(XReadCursorError, match="invalid"):
        reader.sync(SyncCursor('{"endpoint":"removed"}'))
    assert transport.calls == []
