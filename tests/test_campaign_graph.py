import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import store
from app.campaign_graph import CampaignGraphError, CampaignGraphStore
from app.campaign_templates import CampaignTemplateStore
from app.dispatch import GovernedDispatcher, SQLiteDispatchStore
from app.distribution_package import DistributionPackageStore
from app.editorial import EditorialStore
from app.main import app


def setup_graph(tmp_path, monkeypatch):
    database = tmp_path / "campaign-graph.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    store.init_db()
    brand = store.get_brand("demo-brand")
    source = store.insert("sources", {
        "brand_id": brand["id"], "title": "Official source", "url": "https://issuer.test",
        "source_type": "official", "body_summary": "Terms", "lifecycle_state": "published",
        "scheduled_for": None, "external_source_id": "official-1",
    })
    DistributionPackageStore(
        database, EditorialStore(database), GovernedDispatcher(SQLiteDispatchStore(database)),
    )
    return database, brand, source, CampaignGraphStore(database)


def insert_asset(database, brand_id, asset_type, channel):
    asset_id = f"{asset_type}-{channel}"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO campaign_assets VALUES (?,?,?,?,?,?,?,?)",
            (asset_id, brand_id, asset_type, channel, "draft", "{}", store.now(), store.now()),
        )
    return asset_id


def test_empty_campaign_supports_attach_reorder_anchor_relationship_flight_and_detach(
    tmp_path, monkeypatch,
):
    database, brand, _, graph = setup_graph(tmp_path, monkeypatch)
    campaign = graph.create_campaign(brand["id"], "Lego campaign", "Test the graph", actor="Chris")
    assert campaign["memberships"] == []
    web_id = insert_asset(database, brand["id"], "web", "web")
    x_id = insert_asset(database, brand["id"], "x_thread", "x")
    anchor = graph.attach(campaign["id"], asset_type="web", asset_id=web_id, channel="web",
                          role="anchor", actor="Chris", reason="Primary guide")
    with pytest.raises(CampaignGraphError, match="already has a primary"):
        graph.attach(campaign["id"], asset_type="x_thread", asset_id=x_id, channel="x",
                     role="anchor", actor="Chris", reason="Competing anchor")
    touchpoint = graph.attach(
        campaign["id"], asset_type="x_thread", asset_id=x_id, channel="x",
        role="touchpoint", actor="Chris", reason="Social explanation", sequence=2,
    )
    relation = graph.add_relationship(
        campaign["id"], anchor["id"], touchpoint["id"], relationship_type="drives",
        actor="Chris", reason="Declare the reader path",
    )
    changed = graph.set_anchor(touchpoint["id"], actor="Chris", reason="Thread leads this flight")
    assert changed["primary_anchor"]["id"] == touchpoint["id"]
    assert changed["relationships"][0]["from_membership_id"] == touchpoint["id"]
    assert changed["relationships"][0]["to_membership_id"] == anchor["id"]
    flight = graph.add_flight(
        campaign["id"], "launch", "2026-09-02T12:00:00Z", "2026-09-04T12:00:00Z",
        actor="Chris", reason="Bound measurement",
    )
    assert flight["name"] == "launch"
    reordered = graph.reorder(
        campaign["id"], [touchpoint["id"], anchor["id"]], actor="Chris", reason="Lead first",
    )
    assert [item["id"] for item in reordered["memberships"] if item["active"]] == [touchpoint["id"], anchor["id"]]
    detached = graph.detach(touchpoint["id"], actor="Chris", reason="Remove this flight")
    assert detached["active"] == 0
    final = graph.get(campaign["id"])
    assert final["primary_anchor"] is None
    assert relation["id"] not in {item["id"] for item in final["relationships"]}
    assert {item["action"] for item in graph.audit(campaign["id"])} >= {
        "created", "attached", "relationship_added", "anchor_changed", "flight_added",
        "reordered", "detached",
    }


def test_all_templates_instantiate_draft_graphs_idempotently(tmp_path, monkeypatch):
    database, brand, source, graph = setup_graph(tmp_path, monkeypatch)
    templates = CampaignTemplateStore(database)
    answers = {"goal": "Explain", "audience": "Readers", "source": "Official source",
               "cta": "Read", "flight": "Launch", "success": "Clicks above baseline"}
    for template in templates.list():
        result = templates.instantiate(
            template["template_key"], "demo-brand", answers,
            name=template["name"], objective="Draft a governed package", source_id=source["id"],
            idempotency_key=f"instance:{template['template_key']}", actor="Chris",
        )
        replay = templates.instantiate(
            template["template_key"], "demo-brand", answers,
            name=template["name"], objective="Draft a governed package", source_id=source["id"],
            idempotency_key=f"instance:{template['template_key']}", actor="Chris",
        )
        assert replay["id"] == result["id"]
        assert result["status"] == "draft"
        assert len([item for item in result["memberships"] if item["role"] == "anchor"]) == 1
        assert any(item["role"] == "supporting" and item["asset_id"] == source["id"]
                   for item in result["memberships"])
    assert len(graph.list(brand["id"])) == 5


def test_template_idempotency_is_tenant_scoped_and_payload_bound(tmp_path, monkeypatch):
    database, points_brand, points_source, graph = setup_graph(tmp_path, monkeypatch)
    demo_other_brand = store.get_brand("demo-personal")
    demo_other_source = store.insert("sources", {
        "brand_id": demo_other_brand["id"], "title": "Demo source",
        "url": "https://demo-personal.test", "source_type": "official", "body_summary": "Notes",
        "lifecycle_state": "published", "scheduled_for": None,
        "external_source_id": "demo-official-1",
    })
    templates = CampaignTemplateStore(database)
    answers = {"goal": "Explain", "audience": "Readers", "source": "Official source",
               "cta": "Read", "flight": "Launch", "success": "Clicks above baseline"}
    points = templates.instantiate(
        "newsletter-led", "demo-brand", answers, name="Demo campaign",
        objective="Serve demo readers", source_id=points_source["id"],
        idempotency_key="shared", actor="Chris",
    )
    demo_other = templates.instantiate(
        "newsletter-led", "demo-personal", answers, name="Demo campaign",
        objective="Serve demo readers", source_id=demo_other_source["id"],
        idempotency_key="shared", actor="Chris",
    )
    assert points["id"] != demo_other["id"]
    assert points["brand_id"] == points_brand["id"]
    assert demo_other["brand_id"] == demo_other_brand["id"]

    changed_answers = {**answers, "goal": "Sell something else"}
    with pytest.raises(CampaignGraphError, match="already bound to a different request"):
        templates.instantiate(
            "newsletter-led", "demo-brand", changed_answers, name="Demo campaign",
            objective="Serve demo readers", source_id=points_source["id"],
            idempotency_key="shared", actor="Chris",
        )
    with pytest.raises(CampaignGraphError, match="already bound to a different request"):
        templates.instantiate(
            "x-thread-explainer", "demo-brand", answers, name="Demo campaign",
            objective="Serve demo readers", source_id=points_source["id"],
            idempotency_key="shared", actor="Chris",
        )
    assert len(graph.list(points_brand["id"])) == 1
    assert len(graph.list(demo_other_brand["id"])) == 1


def test_legacy_template_instance_migrates_and_binds_first_complete_replay(tmp_path, monkeypatch):
    database, brand, source, _ = setup_graph(tmp_path, monkeypatch)
    templates = CampaignTemplateStore(database)
    answers = {"goal": "Explain", "audience": "Readers", "source": "Official source",
               "cta": "Read", "flight": "Launch", "success": "Clicks above baseline"}
    created = templates.instantiate(
        "newsletter-led", "demo-brand", answers, name="Legacy campaign",
        objective="Keep this campaign", source_id=source["id"],
        idempotency_key="legacy-key", actor="Chris",
    )
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT * FROM campaign_template_instances WHERE campaign_id=?", (created["id"],),
        ).fetchone()
        connection.execute("ALTER TABLE campaign_template_instances RENAME TO scoped_instances")
        connection.execute(
            """CREATE TABLE campaign_template_instances (
              idempotency_key TEXT PRIMARY KEY,brand_id TEXT NOT NULL,
              template_key TEXT NOT NULL,template_version INTEGER NOT NULL,
              campaign_id TEXT NOT NULL UNIQUE,created_by TEXT NOT NULL,created_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO campaign_template_instances VALUES (?,?,?,?,?,?,?)",
            (row[1], row[0], row[3], row[4], row[7], row[8], row[9]),
        )
        connection.execute("DROP TABLE scoped_instances")

    migrated = CampaignTemplateStore(database).instantiate(
        "newsletter-led", "demo-brand", answers, name="Legacy campaign",
        objective="Keep this campaign", source_id=source["id"],
        idempotency_key="legacy-key", actor="Chris",
    )
    assert migrated["id"] == created["id"]
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(campaign_template_instances)"
        )}
        binding = connection.execute(
            """SELECT request_fingerprint,core_fingerprint FROM campaign_template_instances
               WHERE brand_id=? AND idempotency_key='legacy-key'""", (brand["id"],),
        ).fetchone()
    assert {"operation", "request_fingerprint", "core_fingerprint"} <= columns
    assert binding[0] and binding[1]

    with pytest.raises(CampaignGraphError, match="already bound to a different request"):
        CampaignTemplateStore(database).instantiate(
            "newsletter-led", "demo-brand", {**answers, "cta": "Different"},
            name="Legacy campaign", objective="Keep this campaign", source_id=source["id"],
            idempotency_key="legacy-key", actor="Chris",
        )


def test_channel_native_metrics_do_not_invent_cross_channel_reach_or_ctr(tmp_path, monkeypatch):
    database, brand, _, graph = setup_graph(tmp_path, monkeypatch)
    campaign = graph.create_campaign(brand["id"], "Measurement", "Normalize safely", actor="Chris")
    specifications = [
        ("newsletter", "newsletter", {"delivered": 100, "opens": 50, "clicks": 10}),
        ("email", "email", {"delivered": 80, "opens": 32, "clicks": 8}),
        ("web", "web", {"pageviews": 200, "sessions": 150, "clicks": 15}),
        ("youtube", "youtube", {"views": 300, "watch_time_seconds": 9000, "clicks": 12}),
        ("x_post", "x", {"impressions": 500, "engagements": 40, "clicks": 5}),
    ]
    for index, (asset_type, channel, metrics) in enumerate(specifications):
        asset_id = insert_asset(database, brand["id"], asset_type, channel)
        member = graph.attach(
            campaign["id"], asset_type=asset_type, asset_id=asset_id, channel=channel,
            role="anchor" if index == 0 else "touchpoint", actor="Chris", reason="Measure",
            attribution_primary=index == 0,
        )
        first = graph.record_metric(
            member["id"], observed_at="2026-09-03T12:00:00Z", native_metrics=metrics,
            conversions=1, revenue_cents=100, attribution_confidence="modeled",
            idempotency_key=f"metric:{channel}",
        )
        assert graph.record_metric(
            member["id"], observed_at="2026-09-03T12:00:00Z", native_metrics=metrics,
            conversions=1, revenue_cents=100, attribution_confidence="modeled",
            idempotency_key=f"metric:{channel}",
        )["id"] == first["id"]

    report = graph.measurement(campaign["id"])
    assert report["cross_channel_rollup"]["clicks"] == 50
    assert report["cross_channel_rollup"]["cross_channel_ctr"] is None
    assert "newsletter" in report["cross_channel_rollup"]["reach_not_summed"]
    assert "youtube" in report["cross_channel_rollup"]["reach_not_summed"]
    assert report["channels"]["email"]["rates"]["open_rate"]["denominator_metric"] == "delivered"
    assert report["channels"]["x"]["rates"]["engagement_rate"]["value"] == 0.08
    assert report["conversion_confidence"] == "modeled"


def test_generic_graph_rest_acceptance_and_template_instantiation(tmp_path, monkeypatch):
    database = tmp_path / "campaign-graph-api.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "test-password")
    headers = {"Authorization": "Basic b3BlcmF0b3I6dGVzdC1wYXNzd29yZA=="}
    with TestClient(app, headers=headers) as client:
        brand = store.get_brand("demo-brand")
        source = store.insert("sources", {
            "brand_id": brand["id"], "title": "Official", "url": "https://issuer.test",
            "source_type": "official", "body_summary": "Terms", "lifecycle_state": "published",
            "scheduled_for": None, "external_source_id": "rest-source",
        })
        empty = client.post("/api/brands/demo-brand/campaigns", json={
            "name": "Empty planning", "objective": "Choose assets later",
        })
        assert empty.status_code == 201
        assert empty.json()["memberships"] == []
        answers = {"goal": "Explain", "audience": "Readers", "source": "Official",
                   "cta": "Read", "flight": "Launch", "success": "Clicks"}
        instantiated = client.post(
            "/api/brands/demo-brand/campaign-templates/youtube-launch/instantiate",
            json={"answers": answers, "name": "Video launch", "objective": "Launch safely",
                  "source_id": source["id"], "idempotency_key": "youtube-launch-1"},
        )
        assert instantiated.status_code == 201
        graph = instantiated.json()
        assert graph["primary_anchor"]["channel"] == "youtube"
        members = [item for item in graph["memberships"] if item["active"]]
        touchpoint = next(item for item in members if item["role"] == "touchpoint" and item["channel"] == "x")
        changed = client.post(
            f"/api/campaign-memberships/{touchpoint['id']}/anchor",
            json={"reason": "Lead with this channel for the first flight"},
        )
        assert changed.status_code == 200
        assert changed.json()["primary_anchor"]["id"] == touchpoint["id"]
        reordered_ids = [item["id"] for item in reversed(members)]
        assert client.post(
            f"/api/campaigns/{graph['id']}/memberships/reorder",
            json={"membership_ids": reordered_ids, "reason": "Match the delivery sequence"},
        ).status_code == 200
        supporting = next(item for item in members if item["role"] == "supporting")
        assert client.post(f"/api/campaigns/{graph['id']}/relationships", json={
            "from_membership_id": supporting["id"], "to_membership_id": touchpoint["id"],
            "relationship_type": "informs", "reason": "Preserve grounding lineage",
        }).status_code == 201
        assert client.post(f"/api/campaigns/{graph['id']}/flights", json={
            "name": "launch", "starts_at": "2026-09-02T12:00:00Z",
            "ends_at": "2026-09-04T12:00:00Z", "reason": "Bound attribution",
        }).status_code == 201
        assert client.post(f"/api/campaign-memberships/{touchpoint['id']}/metrics", json={
            "observed_at": "2026-09-03T12:00:00Z", "native_metrics": {"impressions": 100,
            "clicks": 5}, "idempotency_key": "api-metric-1",
        }).status_code == 201
        assert client.get("/api/brands/demo-brand/campaign-graphs").status_code == 200
        assert client.get(f"/api/campaigns/{graph['id']}/measurement").json()[
            "cross_channel_rollup"
        ]["cross_channel_ctr"] is None
        assert client.get(f"/api/campaigns/{graph['id']}/graph-audit").json()[-1][
            "action"
        ] == "flight_added"
        assert client.post(
            f"/api/campaign-memberships/{touchpoint['id']}/detach",
            json={"reason": "Remove the completed touchpoint"},
        ).json()["active"] == 0
        from app.mcp_server import get_campaign_graph, get_campaign_measurement, list_campaign_graphs, mcp
        assert get_campaign_graph(graph["id"])["id"] == graph["id"]
        assert get_campaign_measurement(graph["id"])["campaign_id"] == graph["id"]
        assert any(item["id"] == graph["id"] for item in list_campaign_graphs("demo-brand"))
        assert "instantiate_campaign_template" in set(mcp._tool_manager._tools)
