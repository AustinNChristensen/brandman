from __future__ import annotations

import json

from brandman import store


def _seed_source(brand_id: str, index: int, *, state: str = "published") -> dict:
    return store.insert(
        "sources",
        {
            "brand_id": brand_id,
            "title": f"Source {index:02d}",
            "url": f"https://example.test/{index}",
            "source_type": "rss",
            "body_summary": "Current editorial intelligence",
            "lifecycle_state": state,
            "scheduled_for": None,
            "external_source_id": f"source-{index}",
        },
    )


def _seed_campaign(brand_id: str, index: int, *, source_id: str | None = None,
                   status: str = "draft") -> dict:
    return store.insert(
        "campaigns",
        {
            "brand_id": brand_id,
            "source_id": source_id,
            "name": f"Campaign {index:02d}",
            "objective": "Serve the current mission",
            "status": status,
        },
    )


def _quarantine(table: str, record_id: str) -> None:
    with store.connection() as connection:
        connection.execute(
            """INSERT INTO fixture_quarantine_registry
               (table_name,record_key_json,manifest_sha256,actor,reason,quarantined_at)
               VALUES (?,?,?,?,?,?)""",
            (table, json.dumps([record_id], separators=(",", ":")), "a" * 64,
             "test-auditor", "fixture evidence", store.now()),
        )


def test_context_is_active_recent_and_excludes_terminal_and_quarantined_records() -> None:
    store.init_db(profile="test")
    brand = store.get_brand("demo-brand")
    assert brand is not None

    for index in range(30):
        _seed_source(brand["id"], index)
        _seed_campaign(brand["id"], index)
    archived_source = _seed_source(brand["id"], 90, state="archived")
    archived_campaign = _seed_campaign(brand["id"], 90, status="archived")
    quarantined_source = _seed_source(brand["id"], 91)
    quarantined_campaign = _seed_campaign(brand["id"], 91)
    _quarantine("sources", quarantined_source["id"])
    _quarantine("campaigns", quarantined_campaign["id"])

    context = store.brand_context("demo-brand")
    assert context is not None
    source_ids = {item["id"] for item in context["sources"]}
    campaign_ids = {item["id"] for item in context["campaigns"]}
    assert len(source_ids) == len(campaign_ids) == 25
    assert archived_source["id"] not in source_ids
    assert quarantined_source["id"] not in source_ids
    assert archived_campaign["id"] not in campaign_ids
    assert quarantined_campaign["id"] not in campaign_ids
    assert context["context_window"] == {
        "scope": "active_recent",
        "sources": {"returned": 25, "eligible": 30, "limit": 25},
        "campaigns": {"returned": 25, "eligible": 30, "limit": 25},
        "excluded_states": [
            "abandoned", "archived", "cancelled", "deleted", "rejected", "stale",
        ],
        "quarantined_records_excluded": True,
        "history_surfaces": {
            "sources_and_posts": "/api/brands/demo-brand/calendar",
            "campaigns": "/api/brands/demo-brand/campaign-graphs",
        },
    }


def test_context_prioritizes_sources_linked_to_active_campaigns() -> None:
    store.init_db(profile="test")
    brand = store.get_brand("demo-brand")
    assert brand is not None
    linked = _seed_source(brand["id"], 0)
    _seed_campaign(brand["id"], 0, source_id=linked["id"])
    for index in range(1, 31):
        _seed_source(brand["id"], index)

    context = store.brand_context("demo-brand")
    assert context is not None
    assert context["sources"][0]["id"] == linked["id"]
    assert linked["id"] in {item["id"] for item in context["sources"]}


def test_context_filter_does_not_delete_history() -> None:
    store.init_db(profile="test")
    brand = store.get_brand("demo-brand")
    assert brand is not None
    archived_source = _seed_source(brand["id"], 99, state="archived")
    archived_campaign = _seed_campaign(
        brand["id"], 99, source_id=archived_source["id"], status="archived",
    )

    context = store.brand_context("demo-brand")
    assert context is not None
    assert archived_source["id"] not in {item["id"] for item in context["sources"]}
    assert archived_campaign["id"] not in {item["id"] for item in context["campaigns"]}
    assert store.row("SELECT id FROM sources WHERE id=?", (archived_source["id"],))
    assert store.row("SELECT id FROM campaigns WHERE id=?", (archived_campaign["id"],))
