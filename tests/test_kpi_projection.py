from pathlib import Path

from brandman import store
from brandman.attribution_store import AttributionStore
from brandman.connectors import ConnectorEvent, ConnectorKind, ConnectorResult, EventKind
from brandman.kpi_projection import MissionKpiProjector
from brandman.sync import SyncOrchestrator


import pytest

pytestmark = pytest.mark.usefixtures("launch_window_clock")


def setup_loop(tmp_path: Path, kind: ConnectorKind = ConnectorKind.X, *, status="healthy"):
    database = tmp_path / "projection.db"
    store.DATA_PATH = database
    store.init_db()
    brand = store.get_brand("demo-brand")
    mission = store.ensure_demo_brand_growth_mission()
    account = store.upsert_connector_account(
        brand["id"], kind.value, "primary", "Primary", status=status
    )
    attribution = AttributionStore(database, clock=lambda: "2026-09-02T13:00:00+00:00")
    projector = MissionKpiProjector(attribution)
    orchestrator = SyncOrchestrator(kpi_projector=projector)
    return brand, mission, account, attribution, projector, orchestrator


def metric_event(
    kind: ConnectorKind,
    *,
    key: str,
    observed_at: str,
    metric: str,
    value,
    evidence_type: str,
    event_kind: EventKind = EventKind.METRIC_OBSERVED,
):
    return ConnectorEvent(
        kind,
        event_kind,
        key,
        observed_at,
        key,
        {"metric": metric, "value": value, "evidence_type": evidence_type},
    )


def apply(orchestrator, brand, account, event):
    return orchestrator.apply_result(
        ConnectorResult((event,)),
        connector_kind=event.connector,
        brand_id=brand["id"],
        connector_account_id=account["id"],
        stream="metrics",
    )


def test_x_follower_evidence_is_promoted_once_and_links_canonical_event(tmp_path):
    brand, mission, account, attribution, _, orchestrator = setup_loop(tmp_path)
    event = metric_event(
        ConnectorKind.X,
        key="x:profile:11",
        observed_at="2026-09-02T12:00:00+00:00",
        metric="x_followers",
        value=11,
        evidence_type="x_profile_metrics",
    )

    first = apply(orchestrator, brand, account, event)
    second = apply(orchestrator, brand, account, event)

    assert first.kpis_projected == 1
    assert second.kpis_projected == 0
    stored = store.row("SELECT * FROM connector_events")
    canonical = attribution.get_canonical_kpi(mission["id"], "x_followers")
    assert canonical["value"] == 11
    assert canonical["connector_account_id"] == account["id"]
    assert canonical["connector_event_id"] == stored["id"]
    assert canonical["dimensions"]["provider_external_id"] == "x:profile:11"
    assert len(attribution.list_kpi_evidence(mission["id"], "x_followers")) == 1
    assert len(attribution.list_kpi_audit(mission["id"], "x_followers")) == 1
    snapshot = store.row("SELECT * FROM kpi_snapshots WHERE metric='x_followers'")
    assert snapshot["value"] == 11


def test_stale_and_regressive_x_observations_are_evidence_but_do_not_overwrite(tmp_path):
    brand, mission, account, attribution, _, orchestrator = setup_loop(tmp_path)
    current = metric_event(
        ConnectorKind.X, key="current", observed_at="2026-09-02T12:00:00Z",
        metric="x_followers", value=12, evidence_type="x_profile_metrics",
    )
    stale = metric_event(
        ConnectorKind.X, key="stale", observed_at="2026-09-02T11:00:00Z",
        metric="x_followers", value=20, evidence_type="x_profile_metrics",
    )
    regressive = metric_event(
        ConnectorKind.X, key="regressive", observed_at="2026-09-02T13:00:00Z",
        metric="x_followers", value=10, evidence_type="x_profile_metrics",
    )
    same_time_conflict = metric_event(
        ConnectorKind.X, key="conflict", observed_at="2026-09-02T12:00:00Z",
        metric="x_followers", value=13, evidence_type="x_profile_metrics",
    )

    assert apply(orchestrator, brand, account, current).kpis_projected == 1
    assert apply(orchestrator, brand, account, stale).kpis_projected == 0
    assert apply(orchestrator, brand, account, regressive).kpis_projected == 0
    assert apply(orchestrator, brand, account, same_time_conflict).kpis_projected == 0
    canonical = attribution.get_canonical_kpi(mission["id"], "x_followers")
    assert canonical["value"] == 12
    # Blocked observations do not masquerade as verified canonical evidence.
    assert len(attribution.list_kpi_evidence(mission["id"], "x_followers")) == 1
    assert len(attribution.list_kpi_audit(mission["id"], "x_followers")) == 1


def test_beehiiv_and_website_subscriber_snapshots_use_one_canonical_metric(tmp_path):
    brand, mission, account, attribution, _, orchestrator = setup_loop(
        tmp_path, ConnectorKind.BEEHIIV
    )
    beehiiv = metric_event(
        ConnectorKind.BEEHIIV, key="beehiiv:stats:14",
        observed_at="2026-09-02T10:00:00Z", metric="active_subscribers", value=14,
        evidence_type="beehiiv_publication_stats", event_kind=EventKind.SUBSCRIBER_CHANGED,
    )
    assert apply(orchestrator, brand, account, beehiiv).kpis_projected == 1

    website_account = store.upsert_connector_account(
        brand["id"], "website", "primary", "Website", status="connected"
    )
    website = metric_event(
        ConnectorKind.WEBSITE, key="website:subscriber:15",
        observed_at="2026-09-02T11:00:00Z", metric="newsletter_subscribers", value=15,
        evidence_type="website_newsletter_subscribers",
    )
    assert apply(orchestrator, brand, website_account, website).kpis_projected == 1
    canonical = attribution.get_canonical_kpi(
        mission["id"], "active_beehiiv_subscribers"
    )
    assert canonical["value"] == 15
    assert canonical["source"] == "website"
    assert canonical["connector_account_id"] == website_account["id"]


def test_unverified_or_unrecognized_connector_data_cannot_change_kpis(tmp_path):
    brand, mission, account, attribution, projector, orchestrator = setup_loop(
        tmp_path, status="disconnected"
    )
    event = metric_event(
        ConnectorKind.X, key="x:unverified", observed_at="2026-09-02T12:00:00Z",
        metric="x_followers", value=99, evidence_type="x_profile_metrics",
    )
    assert apply(orchestrator, brand, account, event).kpis_projected == 0
    assert attribution.get_canonical_kpi(mission["id"], "x_followers") is None

    # A persisted website analytics row is not subscriber evidence unless its
    # metric and evidence type are both on the allow-list.
    website_account = store.upsert_connector_account(
        brand["id"], "website", "primary", "Website", status="healthy"
    )
    unknown = metric_event(
        ConnectorKind.WEBSITE, key="website:clicks", observed_at="2026-09-02T12:00:00Z",
        metric="clicks", value=1000, evidence_type="website_subscriber_count",
    )
    outcome = apply(orchestrator, brand, website_account, unknown)
    assert outcome.kpis_projected == 0
    assert attribution.get_canonical_kpi(
        mission["id"], "active_beehiiv_subscribers"
    ) is None


def test_invalid_counts_and_out_of_window_observations_are_not_promoted(tmp_path):
    brand, mission, account, attribution, _, orchestrator = setup_loop(tmp_path)
    for key, value in (("fraction", 12.5), ("negative", -1), ("boolean", True)):
        event = metric_event(
            ConnectorKind.X, key=key, observed_at="2026-09-02T12:00:00Z",
            metric="x_followers", value=value, evidence_type="x_profile_metrics",
        )
        assert apply(orchestrator, brand, account, event).kpis_projected == 0
    old = metric_event(
        ConnectorKind.X, key="pre-mission", observed_at="2026-08-31T12:00:00Z",
        metric="x_followers", value=50, evidence_type="x_profile_metrics",
    )
    assert apply(orchestrator, brand, account, old).kpis_projected == 0
    assert attribution.get_canonical_kpi(mission["id"], "x_followers") is None
