import sqlite3

import pytest

from app import store
from app.attribution_store import AttributionStore
from app.campaign_graph import CampaignGraphStore
from app.connectors import ConnectorEvent, ConnectorKind, ConnectorResult, EventKind, WebsiteMetric
from app.sync import SyncOrchestrator
from app.website_metrics import WebsiteCampaignMetricProjector


def setup_campaign(tmp_path, monkeypatch):
    database = tmp_path / "website-metrics.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    store.init_db(profile="test")
    brand = store.get_brand("demo-brand")
    account = store.upsert_connector_account(
        brand["id"], "website", "first-party", "First-party analytics",
        status="healthy", scopes=["analytics.read"], capabilities=["analytics.read"],
        configuration={"endpoint_url": "https://analytics.demo.test/events"},
    )
    graph = CampaignGraphStore(database)
    campaign = graph.create_campaign(
        brand["id"], "Conversion proof", "Measure tracked conversions", actor="Chris",
    )
    asset_id = "x-post-1"
    store.insert("posts", {
        "id": asset_id, "campaign_id": campaign["id"], "channel": "x",
        "body": "Tracked launch post", "status": "draft", "scheduled_for": None,
        "external_post_id": None,
    })
    membership = graph.attach(
        campaign["id"], asset_type="x_post", asset_id=asset_id,
        channel="x", role="anchor", actor="Chris", reason="Tracked destination",
        attribution_primary=True,
    )
    link = AttributionStore(database).create_tracked_link(
        brand_id=brand["id"], brand_slug="demo-brand",
        campaign_id=campaign["id"], artifact_id=asset_id, cta_id="subscribe",
        source="newsletter", medium="email",
        destination="https://demo.test/subscribe", actor="Chris",
    )
    return database, brand, account, graph, campaign, membership, link


def conversion_event(campaign, membership, link, *, external_id="conversion-1"):
    return WebsiteMetric(
        "2026-09-02T12:00:00Z", "newsletter_conversion", 1,
        campaign_id=campaign["id"], post_id=membership["asset_id"],
        evidence_type="website_conversion", tracked_link_id=link["id"],
        attribution_confidence="tracked_link_exact",
    ).as_event(external_id)


def typed_website_event(
    external_id, event_type, payload, *, deployment=False,
    observed_at="2026-09-02T12:00:00+00:00",
):
    return ConnectorEvent(
        ConnectorKind.WEBSITE,
        EventKind.DEPLOYMENT_CHANGED if deployment else EventKind.METRIC_OBSERVED,
        f"website:{external_id}", observed_at, external_id,
        {"event_type": event_type, **payload},
    )


def test_exact_tracked_conversion_projects_once_to_campaign(tmp_path, monkeypatch):
    database, brand, account, graph, campaign, membership, link = setup_campaign(
        tmp_path, monkeypatch,
    )
    orchestrator = SyncOrchestrator(
        campaign_metric_projector=WebsiteCampaignMetricProjector(database),
    )
    event = conversion_event(campaign, membership, link)
    first = orchestrator.apply_result(
        ConnectorResult((event,)), connector_kind=ConnectorKind.WEBSITE,
        brand_id=brand["id"], connector_account_id=account["id"], stream="metrics",
    )
    replay = orchestrator.apply_result(
        ConnectorResult((event,)), connector_kind=ConnectorKind.WEBSITE,
        brand_id=brand["id"], connector_account_id=account["id"], stream="metrics",
    )
    assert first.campaign_metrics_projected == 1
    assert replay.campaign_metrics_projected == 0
    measurement = graph.measurement(campaign["id"])
    assert measurement["cross_channel_rollup"]["conversions"] == 1
    assert measurement["conversion_confidence"] == "tracked_link_exact"
    assert len(store.rows("SELECT * FROM performance_records")) == 1


def test_unattributed_conversion_is_retained_but_never_assigned(tmp_path, monkeypatch):
    database, brand, account, graph, campaign, _, _ = setup_campaign(tmp_path, monkeypatch)
    event = WebsiteMetric(
        "2026-09-02T12:00:00Z", "conversion", 1,
        evidence_type="website_conversion", attribution_confidence="unattributed",
    ).as_event("unattributed-1")
    outcome = SyncOrchestrator(
        campaign_metric_projector=WebsiteCampaignMetricProjector(database),
    ).apply_result(
        ConnectorResult((event,)), connector_kind=ConnectorKind.WEBSITE,
        brand_id=brand["id"], connector_account_id=account["id"], stream="metrics",
    )
    assert outcome.performance_recorded == 1
    assert outcome.campaign_metrics_projected == 0
    assert graph.measurement(campaign["id"])["cross_channel_rollup"]["conversions"] == 0
    connector_event = store.row("SELECT payload FROM connector_events")
    assert '"attribution_confidence": "unattributed"' in connector_event["payload"]


def test_forged_attribution_fails_before_any_projection(tmp_path, monkeypatch):
    database, brand, account, _, campaign, membership, link = setup_campaign(
        tmp_path, monkeypatch,
    )
    event = conversion_event(campaign, membership, {**link, "id": "different-link"})
    with pytest.raises(ValueError, match="does not match"):
        SyncOrchestrator(
            campaign_metric_projector=WebsiteCampaignMetricProjector(database),
        ).apply_result(
            ConnectorResult((event,)), connector_kind=ConnectorKind.WEBSITE,
            brand_id=brand["id"], connector_account_id=account["id"], stream="metrics",
        )
    assert store.rows("SELECT * FROM connector_events") == []
    assert store.rows("SELECT * FROM performance_records") == []


def test_mixed_valid_and_forged_batch_is_rejected_without_partial_projection(
    tmp_path, monkeypatch,
):
    database, brand, account, graph, campaign, membership, link = setup_campaign(
        tmp_path, monkeypatch,
    )
    store.set_sync_cursor(account["id"], "metrics", "before-page")
    valid = conversion_event(campaign, membership, link, external_id="valid-first")
    forged = conversion_event(
        campaign, membership, {**link, "id": "wrong-link"},
        external_id="forged-second",
    )
    with pytest.raises(ValueError, match="does not match"):
        SyncOrchestrator(
            campaign_metric_projector=WebsiteCampaignMetricProjector(database),
        ).apply_result(
            ConnectorResult((valid, forged)), connector_kind=ConnectorKind.WEBSITE,
            brand_id=brand["id"], connector_account_id=account["id"],
            stream="metrics",
        )
    assert store.rows("SELECT * FROM connector_events") == []
    assert store.rows("SELECT * FROM performance_records") == []
    assert graph.measurement(campaign["id"])["cross_channel_rollup"]["conversions"] == 0
    assert store.get_sync_cursor(account["id"], "metrics")["cursor"] == "before-page"


def test_inactive_link_and_unhealthy_connector_fail_before_projection(tmp_path, monkeypatch):
    database, brand, account, _, campaign, membership, link = setup_campaign(
        tmp_path, monkeypatch,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE tracked_links SET lifecycle='deprecated' WHERE id=?", (link["id"],))
    projector = WebsiteCampaignMetricProjector(database)
    with pytest.raises(ValueError, match="does not match"):
        projector.validate(
            brand_id=brand["id"], connector_account_id=account["id"],
            event=conversion_event(campaign, membership, link),
        )
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE tracked_links SET lifecycle='active' WHERE id=?", (link["id"],))
        connection.execute("UPDATE connector_accounts SET status='unhealthy' WHERE id=?", (account["id"],))
    with pytest.raises(ValueError, match="not healthy"):
        projector.validate(
            brand_id=brand["id"], connector_account_id=account["id"],
            event=conversion_event(campaign, membership, link),
        )


def test_authenticated_funnel_and_deployment_page_projects_exactly_once(
    tmp_path, monkeypatch,
):
    database, brand, account, graph, campaign, membership, link = setup_campaign(
        tmp_path, monkeypatch,
    )
    session_ref = "sha256:" + "1" * 64
    exact = {
        "brand_id": brand["id"], "campaign_id": campaign["id"],
        "asset_id": membership["asset_id"], "post_id": membership["asset_id"],
        "tracked_link_id": link["id"], "cta_id": "subscribe", "source": "newsletter",
        "attribution_confidence": "tracked_link_exact",
    }
    events = (
        typed_website_event("session-1", "authenticated_session", {
            "metric": "authenticated_session", "value": 1,
            "session_ref": session_ref, "authenticated": True,
        }),
        typed_website_event("tool-1", "tool_use", {
            "metric": "tool_use", "value": 1, "session_ref": session_ref,
            "authenticated": True, "tool_key": "award-search", **exact,
        }),
        typed_website_event("conversion-1", "conversion", {
            "metric": "newsletter_conversion", "value": 1,
            "evidence_type": "website_conversion", **exact,
        }),
        typed_website_event("deploy-1", "deployment_changed", {
            "deployment_id": "site-production", "deployment_revision": "git-abc123",
            "environment": "production",
        }, deployment=True),
    )
    projector = WebsiteCampaignMetricProjector(database)
    orchestrator = SyncOrchestrator(campaign_metric_projector=projector)
    first = orchestrator.apply_result(
        ConnectorResult(events), connector_kind=ConnectorKind.WEBSITE,
        brand_id=brand["id"], connector_account_id=account["id"], stream="events",
    )
    replay = orchestrator.apply_result(
        ConnectorResult(events), connector_kind=ConnectorKind.WEBSITE,
        brand_id=brand["id"], connector_account_id=account["id"], stream="events",
    )

    assert first.recorded == 4
    assert first.website_sessions_recorded == 1
    assert first.website_tool_uses_recorded == 1
    assert first.website_deployments_recorded == 1
    assert first.campaign_metrics_projected == 1
    assert replay.recorded == 0
    assert replay.website_sessions_recorded == 0
    assert replay.website_tool_uses_recorded == 0
    assert replay.website_deployments_recorded == 0
    assert replay.campaign_metrics_projected == 0
    observations = store.rows("SELECT * FROM website_event_observations ORDER BY id")
    assert [row["event_type"] for row in observations] == [
        "authenticated_session", "tool_use", "conversion", "deployment_changed",
    ]
    tool = observations[1]
    assert tool["brand_id"] == brand["id"]
    assert tool["campaign_id"] == campaign["id"]
    assert tool["asset_membership_id"] == membership["id"]
    assert tool["tracked_link_id"] == link["id"]
    assert tool["cta_id"] == "subscribe"
    assert tool["attribution_source"] == "newsletter"
    assert graph.measurement(campaign["id"])["cross_channel_rollup"]["conversions"] == 1
    assert len(store.rows("SELECT * FROM performance_records")) == 1


def test_unknown_authenticated_session_rejects_whole_page_before_writes(
    tmp_path, monkeypatch,
):
    database, brand, account, _, campaign, membership, link = setup_campaign(
        tmp_path, monkeypatch,
    )
    store.set_sync_cursor(account["id"], "events", "before-page")
    exact = {
        "brand_id": brand["id"], "campaign_id": campaign["id"],
        "asset_id": membership["asset_id"], "tracked_link_id": link["id"],
        "cta_id": "subscribe", "source": "newsletter",
        "attribution_confidence": "tracked_link_exact",
    }
    valid_session = typed_website_event("session-1", "authenticated_session", {
        "metric": "authenticated_session", "value": 1,
        "session_ref": "sha256:" + "1" * 64, "authenticated": True,
    })
    unknown_tool = typed_website_event("tool-1", "tool_use", {
        "metric": "tool_use", "value": 1, "session_ref": "sha256:" + "2" * 64,
        "authenticated": True, "tool_key": "award-search", **exact,
    })
    with pytest.raises(ValueError, match="unknown authenticated session"):
        SyncOrchestrator(
            campaign_metric_projector=WebsiteCampaignMetricProjector(database),
        ).apply_result(
            ConnectorResult((valid_session, unknown_tool)),
            connector_kind=ConnectorKind.WEBSITE, brand_id=brand["id"],
            connector_account_id=account["id"], stream="events",
        )
    assert store.rows("SELECT * FROM connector_events") == []
    assert store.rows("SELECT * FROM website_event_observations") == []
    assert store.rows("SELECT * FROM performance_records") == []
    assert store.get_sync_cursor(account["id"], "events")["cursor"] == "before-page"


def test_tool_use_cannot_be_attributed_to_a_session_that_started_later(
    tmp_path, monkeypatch,
):
    database, brand, account, _, _, _, _ = setup_campaign(tmp_path, monkeypatch)
    session_ref = "sha256:" + "6" * 64
    tool = typed_website_event("tool", "tool_use", {
        "metric": "tool_use", "value": 1, "session_ref": session_ref,
        "authenticated": True, "tool_key": "award-search",
        "attribution_confidence": "unattributed",
    }, observed_at="2026-09-02T12:00:00+00:00")
    later_session = typed_website_event("session", "authenticated_session", {
        "metric": "authenticated_session", "value": 1,
        "session_ref": session_ref, "authenticated": True,
    }, observed_at="2026-09-02T12:01:00+00:00")
    with pytest.raises(ValueError, match="observed later"):
        SyncOrchestrator(
            campaign_metric_projector=WebsiteCampaignMetricProjector(database),
        ).apply_result(
            ConnectorResult((tool, later_session)), connector_kind=ConnectorKind.WEBSITE,
            brand_id=brand["id"], connector_account_id=account["id"], stream="events",
        )
    assert store.rows("SELECT * FROM connector_events") == []


@pytest.mark.parametrize("field,bad_value", [
    ("brand_id", "foreign-brand"),
    ("campaign_id", "foreign-campaign"),
    ("asset_id", "foreign-asset"),
    ("tracked_link_id", "foreign-link"),
    ("cta_id", "different-cta"),
    ("source", "different-source"),
])
def test_typed_tool_use_requires_every_exact_attribution_dimension(
    tmp_path, monkeypatch, field, bad_value,
):
    database, brand, account, _, campaign, membership, link = setup_campaign(
        tmp_path, monkeypatch,
    )
    session_ref = "sha256:" + "4" * 64
    exact = {
        "brand_id": brand["id"], "campaign_id": campaign["id"],
        "asset_id": membership["asset_id"], "tracked_link_id": link["id"],
        "cta_id": "subscribe", "source": "newsletter",
        "attribution_confidence": "tracked_link_exact",
    }
    exact[field] = bad_value
    events = (
        typed_website_event("session", "authenticated_session", {
            "metric": "authenticated_session", "value": 1,
            "session_ref": session_ref, "authenticated": True,
        }),
        typed_website_event("tool", "tool_use", {
            "metric": "tool_use", "value": 1, "session_ref": session_ref,
            "authenticated": True, "tool_key": "award-search", **exact,
        }),
    )
    with pytest.raises(ValueError, match="does not match"):
        SyncOrchestrator(
            campaign_metric_projector=WebsiteCampaignMetricProjector(database),
        ).apply_result(
            ConnectorResult(events), connector_kind=ConnectorKind.WEBSITE,
            brand_id=brand["id"], connector_account_id=account["id"], stream="events",
        )
    assert store.rows("SELECT * FROM connector_events") == []
    assert store.rows("SELECT * FROM website_event_observations") == []


def test_unattributed_tool_use_stays_unassigned_but_requires_known_session(
    tmp_path, monkeypatch,
):
    database, brand, account, _, campaign, _, _ = setup_campaign(tmp_path, monkeypatch)
    session_ref = "sha256:" + "3" * 64
    events = (
        typed_website_event("session-1", "authenticated_session", {
            "metric": "authenticated_session", "value": 1,
            "session_ref": session_ref, "authenticated": True,
        }),
        typed_website_event("tool-1", "tool_use", {
            "metric": "tool_use", "value": 1, "session_ref": session_ref,
            "authenticated": True, "tool_key": "award-search",
            "attribution_confidence": "unattributed",
        }),
    )
    outcome = SyncOrchestrator(
        campaign_metric_projector=WebsiteCampaignMetricProjector(database),
    ).apply_result(
        ConnectorResult(events), connector_kind=ConnectorKind.WEBSITE,
        brand_id=brand["id"], connector_account_id=account["id"], stream="events",
    )
    tool = store.row(
        "SELECT * FROM website_event_observations WHERE event_type='tool_use'",
    )
    assert outcome.website_tool_uses_recorded == 1
    assert tool["campaign_id"] is None
    assert tool["asset_membership_id"] is None
    assert tool["tracked_link_id"] is None
    assert CampaignGraphStore(database).measurement(campaign["id"])["timeline"] == []


def test_reused_provider_event_identity_cannot_change_tool_material(
    tmp_path, monkeypatch,
):
    database, brand, account, _, _, _, _ = setup_campaign(tmp_path, monkeypatch)
    session_ref = "sha256:" + "5" * 64
    session = typed_website_event("session", "authenticated_session", {
        "metric": "authenticated_session", "value": 1,
        "session_ref": session_ref, "authenticated": True,
    })
    first_tool = typed_website_event("tool", "tool_use", {
        "metric": "tool_use", "value": 1, "session_ref": session_ref,
        "authenticated": True, "tool_key": "award-search",
        "attribution_confidence": "unattributed",
    })
    orchestrator = SyncOrchestrator(
        campaign_metric_projector=WebsiteCampaignMetricProjector(database),
    )
    orchestrator.apply_result(
        ConnectorResult((session, first_tool)), connector_kind=ConnectorKind.WEBSITE,
        brand_id=brand["id"], connector_account_id=account["id"], stream="events",
    )
    changed_tool = typed_website_event("tool", "tool_use", {
        **first_tool.payload, "tool_key": "card-search",
    })
    with pytest.raises(ValueError, match="already bound to different material"):
        orchestrator.apply_result(
            ConnectorResult((changed_tool,)), connector_kind=ConnectorKind.WEBSITE,
            brand_id=brand["id"], connector_account_id=account["id"], stream="events",
        )
    rows = store.rows(
        "SELECT tool_key FROM website_event_observations WHERE event_type='tool_use'",
    )
    assert rows == [{"tool_key": "award-search"}]


def test_conflicting_duplicate_identity_in_page_is_rejected_before_any_write(
    tmp_path, monkeypatch,
):
    database, brand, account, graph, campaign, _, _ = setup_campaign(tmp_path, monkeypatch)
    store.set_sync_cursor(account["id"], "events", "before-page")
    session_ref = "sha256:" + "7" * 64
    session = typed_website_event("session", "authenticated_session", {
        "metric": "authenticated_session", "value": 1,
        "session_ref": session_ref, "authenticated": True,
    })
    first_tool = typed_website_event("tool", "tool_use", {
        "metric": "tool_use", "value": 1, "session_ref": session_ref,
        "authenticated": True, "tool_key": "award-search",
        "attribution_confidence": "unattributed",
    })
    conflicting_tool = typed_website_event("tool", "tool_use", {
        **first_tool.payload, "tool_key": "card-search",
    })

    with pytest.raises(ValueError, match="already bound to different material"):
        SyncOrchestrator(
            campaign_metric_projector=WebsiteCampaignMetricProjector(database),
        ).apply_result(
            ConnectorResult((session, first_tool, conflicting_tool)),
            connector_kind=ConnectorKind.WEBSITE, brand_id=brand["id"],
            connector_account_id=account["id"], stream="events",
        )

    assert store.rows("SELECT * FROM connector_events") == []
    assert store.rows("SELECT * FROM website_event_observations") == []
    assert store.rows("SELECT * FROM performance_records") == []
    assert graph.measurement(campaign["id"])["timeline"] == []
    assert store.get_sync_cursor(account["id"], "events")["cursor"] == "before-page"


def test_same_website_provider_events_replayed_across_streams_count_once(
    tmp_path, monkeypatch,
):
    database, brand, account, graph, campaign, membership, link = setup_campaign(
        tmp_path, monkeypatch,
    )
    session_ref = "sha256:" + "8" * 64
    exact = {
        "brand_id": brand["id"], "campaign_id": campaign["id"],
        "asset_id": membership["asset_id"], "tracked_link_id": link["id"],
        "cta_id": "subscribe", "source": "newsletter",
        "attribution_confidence": "tracked_link_exact",
    }
    events = (
        typed_website_event("session-cross-stream", "authenticated_session", {
            "metric": "authenticated_session", "value": 1,
            "session_ref": session_ref, "authenticated": True,
        }),
        typed_website_event("tool-cross-stream", "tool_use", {
            "metric": "tool_use", "value": 1, "session_ref": session_ref,
            "authenticated": True, "tool_key": "award-search", **exact,
        }),
        typed_website_event("conversion-cross-stream", "conversion", {
            "metric": "newsletter_conversion", "value": 1,
            "evidence_type": "website_conversion", **exact,
        }),
    )
    orchestrator = SyncOrchestrator(
        campaign_metric_projector=WebsiteCampaignMetricProjector(database),
    )
    first = orchestrator.apply_result(
        ConnectorResult(events), connector_kind=ConnectorKind.WEBSITE,
        brand_id=brand["id"], connector_account_id=account["id"], stream="metrics",
    )
    replay = orchestrator.apply_result(
        ConnectorResult(events), connector_kind=ConnectorKind.WEBSITE,
        brand_id=brand["id"], connector_account_id=account["id"], stream="events",
    )

    assert first.recorded == 3
    assert replay.recorded == 0
    assert replay.performance_recorded == 0
    assert replay.campaign_metrics_projected == 0
    assert replay.website_sessions_recorded == 0
    assert replay.website_tool_uses_recorded == 0
    assert len(store.rows("SELECT * FROM connector_events")) == 3
    assert len(store.rows("SELECT * FROM website_event_observations")) == 3
    assert len(store.rows("SELECT * FROM performance_records")) == 1
    assert graph.measurement(campaign["id"])["cross_channel_rollup"]["conversions"] == 1


def test_conflicting_website_provider_event_across_streams_is_atomic(
    tmp_path, monkeypatch,
):
    database, brand, account, graph, campaign, _, _ = setup_campaign(tmp_path, monkeypatch)
    session_ref = "sha256:" + "9" * 64
    events = (
        typed_website_event("session-cross-conflict", "authenticated_session", {
            "metric": "authenticated_session", "value": 1,
            "session_ref": session_ref, "authenticated": True,
        }),
        typed_website_event("tool-cross-conflict", "tool_use", {
            "metric": "tool_use", "value": 1, "session_ref": session_ref,
            "authenticated": True, "tool_key": "award-search",
            "attribution_confidence": "unattributed",
        }),
    )
    orchestrator = SyncOrchestrator(
        campaign_metric_projector=WebsiteCampaignMetricProjector(database),
    )
    orchestrator.apply_result(
        ConnectorResult(events), connector_kind=ConnectorKind.WEBSITE,
        brand_id=brand["id"], connector_account_id=account["id"], stream="metrics",
    )
    store.set_sync_cursor(account["id"], "events", "before-cross-stream-page")
    conflicting = typed_website_event("tool-cross-conflict", "tool_use", {
        **events[1].payload, "tool_key": "card-search",
    })

    with pytest.raises(ValueError, match="already bound to different material"):
        orchestrator.apply_result(
            ConnectorResult((conflicting,)), connector_kind=ConnectorKind.WEBSITE,
            brand_id=brand["id"], connector_account_id=account["id"], stream="events",
        )

    assert len(store.rows("SELECT * FROM connector_events")) == 2
    assert len(store.rows("SELECT * FROM website_event_observations")) == 2
    assert store.rows("SELECT * FROM performance_records") == []
    assert graph.measurement(campaign["id"])["timeline"] == []
    assert store.get_sync_cursor(account["id"], "events")["cursor"] == "before-cross-stream-page"
