from pathlib import Path

import pytest

from brandman import store
from brandman.beehiiv_metrics import BeehiivCampaignMetricProjector
from brandman.beehiiv_assisted_sync import BeehiivAssistedSyncError, ingest_beehiiv_measurements
from brandman.campaign_graph import CampaignGraphStore
from brandman.connectors import ConnectorEvent, ConnectorKind, ConnectorResult, EventKind
from brandman.editorial import EditorialStore
from brandman.sync import SyncOrchestrator


pytestmark = pytest.mark.usefixtures("launch_window_clock")


def test_authenticated_beehiiv_stats_project_once_to_exported_campaign_issue(tmp_path: Path):
    database = tmp_path / "beehiiv-metrics.db"
    store.DATA_PATH = database
    store.init_db()
    brand = store.get_brand("demo-brand")
    account = store.upsert_connector_account(
        brand["id"], "beehiiv", "pub_1", "DemoBrand Beehiiv", status="healthy",
    )
    editorial = EditorialStore(database)
    issue = editorial.create_issue(
        brand["id"], {
            "subject": "Measured", "preview_text": "Measured issue",
            "final_title": "Measured issue", "sections": [{"body": "Body"}],
        }, created_by="writer",
    )
    with editorial._connect() as connection:
        connection.execute(
            """UPDATE newsletter_issues SET lifecycle='exported',
               beehiiv_external_id='post_123' WHERE id=?""", (issue["id"],),
        )
    graph = CampaignGraphStore(database)
    campaign = graph.create_campaign(
        brand["id"], "Measured issue", "Learn from newsletter outcomes", actor="tester",
    )
    membership = graph.attach(
        campaign["id"], asset_type="newsletter_issue", asset_id=issue["id"],
        channel="newsletter", role="anchor", attribution_primary=True,
        actor="tester", reason="Bind exported newsletter to its campaign",
    )
    event = ConnectorEvent(
        connector=ConnectorKind.BEEHIIV, kind=EventKind.METRIC_OBSERVED,
        dedup_key="beehiiv:post-stats:post_123:snapshot-1",
        occurred_at="2026-09-02T12:00:00+00:00", external_id="post_123",
        payload={
            "evidence_type": "beehiiv_post_stats", "provider_post_id": "post_123",
            "native_metrics": {
                "delivered": 100, "opens": 45, "clicks": 8,
                "unsubscribes": 1, "web_views": 20, "web_clicks": 4,
                "upgrades": 2,
            },
            "impressions": 100, "clicks": 12, "engagements": 45,
            "conversions": 2,
        },
    )
    orchestrator = SyncOrchestrator(
        campaign_metric_projector=BeehiivCampaignMetricProjector(database),
    )
    first = orchestrator.apply_result(
        ConnectorResult((event,)), connector_kind=ConnectorKind.BEEHIIV,
        brand_id=brand["id"], connector_account_id=account["id"], stream="content",
    )
    replay = orchestrator.apply_result(
        ConnectorResult((event,)), connector_kind=ConnectorKind.BEEHIIV,
        brand_id=brand["id"], connector_account_id=account["id"], stream="content",
    )

    assert first.performance_recorded == 1
    assert first.campaign_metrics_projected == 1
    assert replay.performance_recorded == 0
    assert replay.campaign_metrics_projected == 0
    measurement = graph.measurement(campaign["id"])
    observation = measurement["assets"][membership["id"]]["observations"][0]
    assert observation["membership_id"] == membership["id"]
    assert observation["native_metrics"] == {
        "delivered": 100, "opens": 45, "clicks": 8, "unsubscribes": 1,
    }
    assert observation["conversions"] == 2
    assert observation["attribution_confidence"] == "authenticated_provider_reported"


def test_assisted_measurement_ingest_is_aggregate_replay_safe_and_drops_pii(tmp_path: Path):
    database = tmp_path / "assisted-beehiiv.db"
    store.DATA_PATH = database
    store.init_db()
    brand = store.get_brand("demo-brand")
    mission = store.ensure_demo_brand_growth_mission()
    store.upsert_connector_account(
        brand["id"], "beehiiv", "pub_demo", "DemoBrand Beehiiv",
        status="connected", configuration={"delivery_mode": "mcp_assisted"},
    )
    payload = {
        "database": database, "brand_id": brand["id"],
        "posts": [{
            "id": "post_other", "subscriber_email": "never-store@example.test",
            "stats": {"email": {"delivered": 10, "unique_opens": 4}},
        }],
        "publication_stats": {"active_subscriptions": 18},
        "observed_at": "2026-09-02T12:00:00Z",
    }
    first = ingest_beehiiv_measurements(**payload)
    replay = ingest_beehiiv_measurements(**payload)

    assert first["recorded"] == 2
    assert first["kpis_projected"] == 1
    assert replay["recorded"] == 0
    assert replay["kpis_projected"] == 0
    assert first["privacy"] == "aggregate_only_no_subscriber_records"
    events = store.rows("SELECT payload FROM connector_events ORDER BY event_type")
    assert "never-store@example.test" not in repr(events)
    assert store.row(
        """SELECT value FROM kpi_snapshots
           WHERE mission_id=? AND metric='active_beehiiv_subscribers'
           ORDER BY observed_at DESC LIMIT 1""", (mission["id"],),
    )["value"] == 18

    before = len(store.rows("SELECT id FROM connector_events"))
    with pytest.raises(BeehiivAssistedSyncError, match="canonical post_ provider ID"):
        ingest_beehiiv_measurements(
            database, brand_id=brand["id"],
            posts=[{"id": "person@example.test", "stats": {"email": {"delivered": 1}}}],
            publication_stats=None, observed_at="2026-09-02T13:00:00Z",
        )
    assert len(store.rows("SELECT id FROM connector_events")) == before
