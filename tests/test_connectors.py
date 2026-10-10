import json
from datetime import UTC, datetime

import pytest

from brandman.connectors import (
    BeehiivConnector,
    ConnectorError,
    ConnectorKind,
    EventKind,
    HttpResponse,
    RssConnector,
    SyncCursor,
    WebsiteAnalyticsConnector,
    WebsiteMetric,
    XConnector,
    XPostRequest,
    canonical_url,
    content_fingerprint,
    dedup_identity,
)


class FakeTransport:
    def __init__(self, response: HttpResponse):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def response(document, status=200):
    return HttpResponse(status, json.dumps(document).encode())


def test_identity_strips_tracking_and_fingerprints_normalized_content():
    assert canonical_url("HTTPS://Example.COM/story/?utm_source=x&b=2&a=1#top") == "https://example.com/story?a=1&b=2"
    assert content_fingerprint("<b>Hello</b>   WORLD") == content_fingerprint("hello world")
    assert dedup_identity(ConnectorKind.RSS, url="https://x.test/a?utm_source=x") == dedup_identity(
        ConnectorKind.RSS, url="https://x.test/a"
    )


def test_rss_normalizes_items_and_stops_at_cursor():
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><guid>new</guid><title>New deal</title><link>https://example.com/new?utm_source=rss</link>
      <description><![CDATA[<b>80,000 credits</b>]]></description><pubDate>Tue, 01 Sep 2026 15:00:00 GMT</pubDate></item>
      <item><guid>old</guid><title>Old deal</title><link>https://example.com/old</link></item>
    </channel></rss>"""
    connector = RssConnector("https://example.com/feed", FakeTransport(HttpResponse(200, feed)))
    first = connector.sync()
    assert [event.external_id for event in first.events] == ["new", "old"]
    assert first.events[0].payload["url"] == "https://example.com/new"
    assert first.events[0].payload["summary"] == "80,000 credits"
    assert first.events[0].occurred_at == "2026-09-01T15:00:00+00:00"
    assert first.next_cursor == SyncCursor(first.events[0].dedup_key)

    old_cursor = SyncCursor(first.events[1].dedup_key)
    incremental = connector.sync(old_cursor)
    assert [event.external_id for event in incremental.events] == ["new"]


def test_atom_normalizes_namespaces_and_alternate_link():
    feed = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>tag:example,1</id>
      <title>Transfer bonus</title><link rel="alternate" href="https://example.com/bonus" />
      <summary>Ends Friday</summary><updated>2026-09-01T10:30:00Z</updated></entry></feed>"""
    result = RssConnector("https://example.com/atom", FakeTransport(HttpResponse(200, feed))).sync()
    assert len(result.events) == 1
    assert result.events[0].payload["title"] == "Transfer bonus"
    assert result.events[0].payload["url"] == "https://example.com/bonus"


def test_bad_feed_raises_safe_error():
    with pytest.raises(ConnectorError, match="rss parse failed"):
        RssConnector("https://example.com/feed", FakeTransport(HttpResponse(200, b"not xml"))).sync()


def test_beehiiv_incremental_post_normalization_and_cursor():
    transport = FakeTransport(
        response(
            {
                "data": [
                    {
                        "id": "post_123",
                        "title": "A new offer",
                        "subtitle": "Worth considering",
                        "web_url": "https://demo.test/p/offer?utm_campaign=launch",
                        "status": "confirmed",
                        "publish_date": "2026-09-01T12:00:00Z",
                        "free_web_content": "<p>The details.</p>",
                    }
                ],
                "pagination": {"page": 1, "total_pages": 2},
            }
        )
    )
    connector = BeehiivConnector("pub_1", transport, lambda: "Bearer secret")
    result = connector.sync()
    assert result.has_more is True
    assert result.next_cursor == SyncCursor("2")
    assert result.events[0].external_id == "post_123"
    assert result.events[0].kind == EventKind.SOURCE_ITEM
    assert result.events[0].payload["summary"] == "The details."
    method, _, kwargs = transport.calls[0]
    assert method == "GET"
    assert kwargs["params"]["limit"] == "100"
    assert kwargs["headers"]["Authorization"] == "Bearer secret"


def test_beehiiv_normalizes_post_and_publication_measurements_with_stable_ids():
    class SequenceTransport:
        def __init__(self):
            self.calls = []
            self.responses = [
                response({"data": [{
                    "id": "post_123", "title": "Measured issue", "status": "confirmed",
                    "publish_date": "2026-09-01T12:00:00Z",
                    "stats": {
                        "email": {"delivered": 100, "unique_opens": 45,
                                  "unique_clicks": 8, "unsubscribes": 1},
                        "web": {"views": 20, "clicks": 4}, "upgrades": 2,
                    },
                }], "pagination": {"page": 1, "total_pages": 1}}),
                response({"data": {"id": "pub_1", "stats": {
                    "active_subscriptions": 18, "active_free_subscriptions": 16,
                    "active_premium_subscriptions": 2,
                }}}),
            ]

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return self.responses.pop(0)

    transport = SequenceTransport()
    connector = BeehiivConnector(
        "pub_1", transport, lambda: "Bearer secret",
        include_publication_stats=True,
        clock=lambda: datetime(2026, 9, 2, 12, tzinfo=UTC),
    )
    result = connector.sync()

    assert [event.kind for event in result.events] == [
        EventKind.SOURCE_ITEM, EventKind.METRIC_OBSERVED, EventKind.SUBSCRIBER_CHANGED,
    ]
    post_metric = result.events[1]
    assert post_metric.payload["native_metrics"] == {
        "delivered": 100, "opens": 45, "clicks": 8, "unsubscribes": 1,
        "web_views": 20, "web_clicks": 4, "upgrades": 2,
    }
    assert post_metric.payload["conversions"] == 2
    subscriber = result.events[2]
    assert subscriber.payload["value"] == 18
    assert subscriber.payload["evidence_type"] == "beehiiv_publication_stats"
    assert all("secret" not in repr(event) for event in result.events)
    assert transport.calls[1][1].endswith("/publications/pub_1")
    assert transport.calls[1][2]["params"] == {"expand[]": "stats"}


@pytest.mark.parametrize("stats", [
    {"email": {"delivered": 10, "unique_opens": -1}},
    {"email": {"delivered": 10, "unique_clicks": True}},
    {"email": {"delivered": 10, "unsubscribes": "not-a-count"}},
    {"email": {"delivered": 10, "unique_opens": 11}},
    {"email": {"delivered": 10, "unique_clicks": 11}},
    {"web": {"views": 2, "clicks": 3}},
])
def test_beehiiv_rejects_entire_malformed_or_relationally_invalid_observation(stats):
    connector = BeehiivConnector(
        "pub_1", FakeTransport(response({
            "data": [{"id": "post_123", "title": "Bad stats", "stats": stats}],
            "pagination": {"page": 1, "total_pages": 1},
        })), lambda: "Bearer secret",
    )
    with pytest.raises(ConnectorError, match="post stats parse"):
        connector.sync()


def test_x_requires_approval_and_normalizes_receipt_with_idempotency():
    transport = FakeTransport(response({"data": {"id": "42", "text": "Useful insight"}}, status=201))
    connector = XConnector(transport, lambda: "Bearer secret")
    with pytest.raises(PermissionError, match="explicit approval"):
        connector.publish(XPostRequest("Nope", "job-1"))
    assert transport.calls == []

    receipt = connector.publish(XPostRequest("Useful insight", "job-1", approved=True, reply_to_post_id="10"))
    assert receipt.external_id == "42"
    assert receipt.external_url == "https://x.com/i/web/status/42"
    _, _, kwargs = transport.calls[0]
    assert kwargs["headers"]["Idempotency-Key"] == "job-1"
    assert kwargs["json_body"]["reply"] == {"in_reply_to_tweet_id": "10"}


def test_errors_and_reprs_do_not_expose_credentials():
    connector = BeehiivConnector("pub_1", FakeTransport(response({}, status=401)), lambda: "Bearer super-secret")
    with pytest.raises(ConnectorError) as error:
        connector.sync()
    assert "super-secret" not in str(error.value)
    assert "super-secret" not in repr(connector)


def test_website_metric_normalizes_to_deduplicated_event():
    metric = WebsiteMetric("2026-09-01T10:00:00Z", "newsletter_conversion", 1, campaign_id="campaign-1")
    event = metric.as_event("analytics-row-9")
    assert event.connector == ConnectorKind.WEBSITE
    assert event.kind == EventKind.METRIC_OBSERVED
    assert event.payload["campaign_id"] == "campaign-1"
    assert event.dedup_key == dedup_identity(ConnectorKind.WEBSITE, external_id="analytics-row-9")


def test_website_connector_preserves_exact_or_explicitly_unattributed_conversion():
    exact = WebsiteAnalyticsConnector(
        "https://analytics.example.test/events",
        FakeTransport(response({"data": [{
            "id": "conversion-1", "observed_at": "2026-09-02T12:00:00Z",
            "metric": "conversion", "value": 1,
            "evidence_type": "website_conversion",
            "attribution_confidence": "tracked_link_exact",
            "campaign_id": "campaign-1", "post_id": "post-1",
            "tracked_link_id": "link-1",
        }]})), lambda: "Bearer redacted", granted_scopes=["analytics.read"],
    ).sync().events[0]
    assert exact.payload["tracked_link_id"] == "link-1"
    assert exact.payload["attribution_confidence"] == "tracked_link_exact"

    unattributed = WebsiteAnalyticsConnector(
        "https://analytics.example.test/events",
        FakeTransport(response({"data": [{
            "id": "conversion-2", "observed_at": "2026-09-02T12:00:00Z",
            "metric": "conversion", "value": 1,
            "evidence_type": "website_conversion",
            "attribution_confidence": "unattributed",
        }]})), lambda: "Bearer redacted", granted_scopes=["analytics.read"],
    ).sync().events[0]
    assert unattributed.payload["campaign_id"] is None


@pytest.mark.parametrize("record", [
    {"metric": "conversion", "value": -1, "attribution_confidence": "unattributed"},
    {"metric": "conversion", "value": True, "attribution_confidence": "unattributed"},
    {"metric": "conversion", "value": 1.5, "attribution_confidence": "unattributed"},
    {"metric": "conversion", "value": 1, "attribution_confidence": "tracked_link_exact"},
    {"metric": "conversion", "value": 1, "attribution_confidence": "unattributed",
     "campaign_id": "must-not-be-attributed"},
])
def test_website_connector_rejects_malformed_conversion_evidence(record):
    document = {"data": [{
        "id": "bad-conversion", "observed_at": "2026-09-02T12:00:00Z",
        "evidence_type": "website_conversion", **record,
    }]}
    connector = WebsiteAnalyticsConnector(
        "https://analytics.example.test/events", FakeTransport(response(document)),
        lambda: "Bearer redacted", granted_scopes=["analytics.read"],
    )
    with pytest.raises(ConnectorError, match="analytics parse"):
        connector.sync()


def test_website_analytics_connector_is_read_only_bounded_and_normalized():
    transport = FakeTransport(response({
        "data": [{
            "id": "row-1", "observed_at": "2026-09-02T12:00:00Z",
            "metric": "active_beehiiv_subscribers", "value": 18,
            "source": "signup", "evidence_type": "website_subscriber_count",
        }],
        "has_more": True, "next_cursor": "page-2",
    }))
    connector = WebsiteAnalyticsConnector(
        "https://demo.test/analytics", transport,
        lambda: "Bearer website-read-value", granted_scopes=["analytics.read"],
    )

    result = connector.sync(SyncCursor("page-1"))
    event = result.events[0]
    assert result.has_more is True
    assert result.next_cursor == SyncCursor("page-2")
    assert event.connector == ConnectorKind.WEBSITE
    assert event.payload["evidence_type"] == "website_subscriber_count"
    assert event.payload["value"] == 18
    method, url, kwargs = transport.calls[0]
    assert method == "GET"
    assert url == "https://demo.test/analytics"
    assert kwargs["params"] == {"cursor": "page-1"}


def test_website_connector_models_authenticated_funnel_and_deployment_events():
    document = {"data": [
        {
            "id": "session-1", "event_type": "authenticated_session",
            "observed_at": "2026-09-02T12:00:00Z", "session_id": "opaque-session-1",
            "authenticated": True,
        },
        {
            "id": "tool-1", "event_type": "tool_use",
            "observed_at": "2026-09-02T12:01:00Z", "session_id": "opaque-session-1",
            "authenticated": True, "tool_key": "award-search", "value": 1,
            "attribution_confidence": "tracked_link_exact", "brand_id": "brand-1",
            "campaign_id": "campaign-1", "asset_id": "post-1",
            "tracked_link_id": "link-1", "cta_id": "subscribe", "source": "newsletter",
        },
        {
            "id": "deploy-1", "event_type": "deployment_changed",
            "observed_at": "2026-09-02T12:02:00Z", "deployment_id": "site-production",
            "deployment_revision": "git-abc123", "environment": "production",
        },
    ]}
    events = WebsiteAnalyticsConnector(
        "https://analytics.example.test/events", FakeTransport(response(document)),
        lambda: "Bearer redacted", granted_scopes=["analytics.read"],
    ).sync().events

    assert [event.kind for event in events] == [
        EventKind.METRIC_OBSERVED, EventKind.METRIC_OBSERVED,
        EventKind.DEPLOYMENT_CHANGED,
    ]
    assert events[0].payload["event_type"] == "authenticated_session"
    assert events[0].payload["session_ref"].startswith("sha256:")
    assert "opaque-session-1" not in repr(events)
    assert events[1].payload["tool_key"] == "award-search"
    assert events[1].payload["asset_id"] == "post-1"
    assert events[2].payload["deployment_revision"] == "git-abc123"


@pytest.mark.parametrize("record", [
    {"event_type": "authenticated_session", "session_id": "s", "authenticated": False},
    {"event_type": "tool_use", "session_id": "s", "authenticated": True,
     "tool_key": "award-search", "attribution_confidence": "tracked_link_exact",
     "brand_id": "brand-1", "campaign_id": "campaign-1", "asset_id": "post-1",
     "tracked_link_id": "link-1", "cta_id": "subscribe"},
    {"event_type": "deployment_changed", "deployment_id": "site",
     "deployment_revision": "abc"},
    {"event_type": "deployment_changed", "deployment_id": "site",
     "deployment_revision": "abc", "environment": "production",
     "campaign_id": "must-not-be-attributed"},
    {"event_type": "authenticated_session", "session_id": "s", "authenticated": True,
     "email": "must-not-persist@example.test"},
    {"event_type": "authenticated_session", "session_id": "s", "authenticated": True,
     "observed_at": "not-a-timestamp"},
])
def test_website_connector_rejects_incomplete_or_identity_bearing_typed_events(record):
    connector = WebsiteAnalyticsConnector(
        "https://analytics.example.test/events",
        FakeTransport(response({"data": [{
            "id": "bad", "observed_at": "2026-09-02T12:00:00Z", **record,
        }]})),
        lambda: "Bearer redacted", granted_scopes=["analytics.read"],
    )
    with pytest.raises(ConnectorError, match="analytics parse"):
        connector.sync()


def test_website_analytics_connector_fails_closed_before_transport():
    transport = FakeTransport(response({"data": []}))
    with pytest.raises(PermissionError, match="missing read scopes"):
        WebsiteAnalyticsConnector(
            "https://demo.test/analytics", transport,
            lambda: "unused", granted_scopes=[],
        )
    with pytest.raises(PermissionError, match="unnecessary scopes"):
        WebsiteAnalyticsConnector(
            "https://demo.test/analytics", transport,
            lambda: "unused", granted_scopes=["analytics.read", "analytics.write"],
        )
    with pytest.raises(ValueError, match="HTTPS"):
        WebsiteAnalyticsConnector(
            "http://demo.test/analytics", transport,
            lambda: "unused", granted_scopes=["analytics.read"],
        )
    assert transport.calls == []
