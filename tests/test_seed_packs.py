import json
from pathlib import Path

import pytest

from brandman import seed_packs, store
from brandman.brand_guidelines import _topic_backlinks
from brandman.principals import is_privileged, reset_privileged_principal, set_privileged_principal


def _pack(tmp_path: Path) -> Path:
    path = tmp_path / "acme.json"
    path.write_text(json.dumps({
        "name": "acme", "claim_units": ["widgets"],
        "brands": [{
            "slug": "acme", "name": "Acme", "mission": "Sell widgets.", "voice": "Plain.",
            "compliance_rules": "Cite sources.",
            "personas": [{"name": "Buyers", "audience": "Widget buyers", "angles": ["price"]}],
            "growth_mission": {"name": "Acme growth", "goals": {"x_followers": [10, 60]}},
        }],
    }))
    return path


@pytest.fixture
def acme(tmp_path, monkeypatch):
    monkeypatch.setenv("BRANDMAN_SEED_PACK", str(_pack(tmp_path)))
    seed_packs._load.cache_clear()
    store.DATA_PATH = tmp_path / "acme.db"
    store.init_db(profile="test")
    yield
    seed_packs._load.cache_clear()


def test_custom_pack_seeds_brands_personas_and_missions(acme):
    brand = store.get_brand("acme")
    assert brand["name"] == "Acme" and store.get_brand("demo-brand") is None
    personas = store.rows("SELECT name FROM personas WHERE brand_id=?", (brand["id"],))
    assert [item["name"] for item in personas] == ["Buyers"]
    mission = store.ensure_growth_mission("acme")
    assert {goal["metric"]: (goal["baseline"], goal["target"]) for goal in mission["goals"]} == {
        "x_followers": (10, 60),
    }
    assert seed_packs.claim_units() == ("widgets",)


def test_none_pack_starts_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("BRANDMAN_SEED_PACK", "none")
    seed_packs._load.cache_clear()
    store.DATA_PATH = tmp_path / "empty.db"
    store.init_db(profile="test")
    assert store.rows("SELECT * FROM brands") == []
    seed_packs._load.cache_clear()


def test_unknown_pack_fails_closed(monkeypatch):
    monkeypatch.setenv("BRANDMAN_SEED_PACK", "does-not-exist")
    seed_packs._load.cache_clear()
    with pytest.raises(seed_packs.SeedPackError):
        seed_packs.brands()
    seed_packs._load.cache_clear()


def test_topic_backlinks_accept_current_and_legacy_forms():
    rules = {
        "topic_backlinks": [{"topic": "pricing", "urls": ["https://x.test/p"]}],
        "transfer_bonus_tool_backlinks": ["https://x.test/t"],
        "approved_tool_backlinks": ["https://x.test/a"],
    }
    assert _topic_backlinks(rules) == [
        ("pricing", ["https://x.test/p"]), ("transfer bonus", ["https://x.test/t"]),
    ]


def test_privileged_principal_is_operator_or_context_marked(monkeypatch):
    monkeypatch.setenv("BRANDMAN_OPERATOR", "owner@example.test")
    assert is_privileged("owner@example.test") and not is_privileged("someone")
    token = set_privileged_principal("someone")
    try:
        assert is_privileged("someone")
    finally:
        reset_privileged_principal(token)
    assert not is_privileged("someone")
