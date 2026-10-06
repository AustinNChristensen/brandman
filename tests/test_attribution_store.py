import sqlite3

import pytest

from app.attribution_store import (
    AttributionStore,
    KpiEvidenceRequired,
    TrackedLinkConflict,
)


@pytest.fixture
def store(tmp_path):
    return AttributionStore(tmp_path / "attribution.db", clock=lambda: "2026-09-02T12:00:00+00:00")


def link_args(**overrides):
    values = {
        "brand_id": "brand-1", "brand_slug": "demo-brand",
        "campaign_id": "campaign-1", "artifact_id": "post-1",
        "cta_id": "subscribe", "source": "X", "medium": "Social",
        "destination": "HTTPS://demo.example/join?b=2&utm_campaign=old&a=1#fragment",
        "actor": "Chris",
    }
    values.update(overrides)
    return values


def test_tracked_link_is_canonical_persistent_and_idempotent(store):
    first = store.create_tracked_link(**link_args())
    second = store.create_tracked_link(**link_args())
    assert first["id"] == second["id"]
    assert first["destination"] == "https://demo.example/join?a=1&b=2"
    assert first["tracked_url"].endswith(
        "utm_source=x&utm_medium=social&utm_campaign=campaign-1&utm_content=post-1&utm_cta=subscribe&utm_brand=demo-brand"
    )
    assert store.get_tracked_link(first["id"])["campaign_id"] == "campaign-1"
    assert store.list_tracked_links("brand-1", lifecycle="active") == [first]
    assert [event["action"] for event in store.list_link_audit(first["id"])] == ["created"]


def test_link_conflicts_and_lifecycle_are_audited(store):
    link = store.create_tracked_link(**link_args(idempotency_key="stable-key"))
    with pytest.raises(TrackedLinkConflict):
        store.create_tracked_link(**link_args(destination="https://demo.example/other", idempotency_key="stable-key"))
    deprecated = store.set_link_lifecycle(link["id"], "deprecated", actor="Chris", detail="campaign ended")
    assert deprecated["lifecycle"] == "deprecated"
    audit = store.list_link_audit(link["id"])
    assert [(row["action"], row["actor"]) for row in audit] == [("created", "Chris"), ("deprecated", "Chris")]


def test_inbound_attribution_requires_a_canonical_match_for_direct(store):
    link = store.create_tracked_link(**link_args())
    direct = store.resolve_inbound_attribution(link["tracked_url"])
    assert direct["confidence"] == "direct"
    assert direct["tracked_link"]["id"] == link["id"]
    assisted = store.resolve_inbound_attribution("https://demo.example/?utm_campaign=unknown")
    assert assisted["confidence"] == "assisted"
    assert assisted["tracked_link"] is None
    assert store.resolve_inbound_attribution("https://demo.example/")["confidence"] == "unattributed"

    # The brand slug in canonical links prevents cross-brand identity collisions.
    store.create_tracked_link(**link_args(brand_id="brand-2", brand_slug="other-brand"))
    no_brand = link["tracked_url"].replace("&utm_brand=demo-brand", "")
    assert store.resolve_inbound_attribution(no_brand)["confidence"] == "assisted"


def test_connector_kpi_evidence_is_required_before_canonical_promotion(store):
    with pytest.raises(KpiEvidenceRequired, match="connector_account_id"):
        store.record_kpi_evidence(
            mission_id="mission-1", metric="x_followers", value=10,
            observed_at="2026-09-02T11:00:00Z", source="x",
        )
    assert store.get_canonical_kpi("mission-1", "x_followers") is None
    evidence = store.record_kpi_evidence(
        mission_id="mission-1", metric="x_followers", value=10,
        observed_at="2026-09-02T11:00:00Z", source="x",
        connector_account_id="x-account", connector_event_id="event-55",
        dimensions={"handle": "demobrand"},
    )
    assert store.get_canonical_kpi("mission-1", "x_followers") is None
    current = store.promote_kpi_evidence(evidence["id"], actor="mission-sync")
    assert current["value"] == 10
    assert current["connector_event_id"] == "event-55"
    assert current["evidence_record_id"] == evidence["id"]


def test_manual_kpi_requires_explicit_human_verification_and_is_idempotent(store):
    with pytest.raises(KpiEvidenceRequired, match="verifier"):
        store.record_kpi_evidence(
            mission_id="mission-1", metric="active_subscribers", value=14,
            observed_at="2026-09-02T11:00:00Z", source="manual", human_manual=True,
        )
    evidence = store.record_kpi_evidence(
        mission_id="mission-1", metric="active_subscribers", value=14,
        observed_at="2026-09-02T11:00:00Z", source="manual", human_manual=True,
        human_verified_by="Chris", human_verification_note="Checked Beehiiv dashboard",
        idempotency_key="manual-subscriber-count-2026-09-02",
    )
    duplicate = store.record_kpi_evidence(
        mission_id="mission-1", metric="active_subscribers", value=14,
        observed_at="2026-09-02T11:00:00Z", source="manual", human_manual=True,
        human_verified_by="Chris", human_verification_note="Checked Beehiiv dashboard",
        idempotency_key="manual-subscriber-count-2026-09-02",
    )
    assert duplicate["id"] == evidence["id"]
    current = store.promote_kpi_evidence(evidence["id"], actor="Chris")
    assert current["evidence_type"] == "human_manual"
    assert current["human_verified_by"] == "Chris"

    stale = store.record_kpi_evidence(
        mission_id="mission-1", metric="active_subscribers", value=13,
        observed_at="2026-09-01T11:00:00Z", source="manual", human_manual=True,
        human_verified_by="Chris",
    )
    with pytest.raises(KpiEvidenceRequired, match="stale"):
        store.promote_kpi_evidence(stale["id"], actor="Chris")
    assert len(store.list_kpi_audit("mission-1", "active_subscribers")) == 1


def test_schema_initialization_is_additive_and_repeatable(store):
    store.init_schema()
    store.init_schema()
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("SELECT count(*) FROM attribution_schema_migrations").fetchone()[0] == 1
