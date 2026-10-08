from brandman import store
from brandman.brand_settings import BrandSettingsError, BrandSettingsStore
from brandman.provider_usage import ProviderUsageLedger
from brandman.main import app
from fastapi.testclient import TestClient
import base64
import pytest


def test_brand_settings_are_audited_and_scoped(tmp_path, monkeypatch):
    database = tmp_path / "settings.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    store.init_db(profile="test")
    points = store.get_brand("demo-brand")
    other = store.create_brand({
        "slug": "other", "name": "Other", "mission": "Other mission", "voice": "Other voice",
        "compliance_rules": "Other rules", "approval_policy": "human_approval_required",
    })
    settings = BrandSettingsStore(database, clock=lambda: "2026-09-03T12:00:00+00:00")
    updated = settings.update(points["id"], {"voice": "Direct and useful"}, actor="chris", reason="Use reviewed voice guidance.")
    assert updated["voice"] == "Direct and useful"
    assert store.get_brand("other")["voice"] == other["voice"]
    audit = settings.audit(points["id"])
    assert audit[0]["actor"] == "chris"
    assert audit[0]["before"]["voice"] != audit[0]["after"]["voice"]


def test_brand_settings_reject_unknown_blank_and_unsupported_values(tmp_path, monkeypatch):
    database = tmp_path / "settings-errors.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    store.init_db(profile="test")
    brand = store.get_brand("demo-brand")
    settings = BrandSettingsStore(database)
    with pytest.raises(BrandSettingsError, match="unsupported brand settings"):
        settings.update(brand["id"], {"slug": "takeover"}, actor="chris", reason="Bad change")
    with pytest.raises(BrandSettingsError, match="cannot be blank"):
        settings.update(brand["id"], {"mission": ""}, actor="chris", reason="Bad change")
    with pytest.raises(BrandSettingsError, match="unsupported approval policy"):
        settings.update(brand["id"], {"approval_policy": "auto_publish"}, actor="chris", reason="Bad change")


def test_rate_cards_list_only_the_selected_brand(tmp_path, monkeypatch):
    database = tmp_path / "rate-cards.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    store.init_db(profile="test")
    points = store.get_brand("demo-brand")
    other = store.create_brand({
        "slug": "other", "name": "Other", "mission": "Other mission", "voice": "Other voice",
        "compliance_rules": "Other rules", "approval_policy": "human_approval_required",
    })
    ledger = ProviderUsageLedger(database)
    for brand_id, version in ((points["id"], "demo-v1"), (other["id"], "other-v1")):
        ledger.configure_price(
            brand_id=brand_id, version=version, provider="x", method="POST",
            endpoint_pattern="https://api.x.com/2/tweets", unit_name="request", unit_price="0.01",
            currency="USD", effective_at="2026-09-03T00:00:00Z", actor="chris",
        )
    assert [item["version"] for item in ledger.list_prices(points["id"])] == ["demo-v1"]


def test_rate_card_exact_replay_is_idempotent_and_conflict_is_explicit(tmp_path, monkeypatch):
    database = tmp_path / "rate-card-replay.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    store.init_db(profile="test")
    brand = store.get_brand("demo-brand")
    ledger = ProviderUsageLedger(database)
    values = {
        "brand_id": brand["id"], "version": "demo-v1", "provider": "x",
        "method": "POST", "endpoint_pattern": "https://api.x.com/2/tweets",
        "unit_name": "request", "unit_price": "0.01", "currency": "USD",
        "effective_at": "2026-09-03T00:00:00Z", "actor": "chris",
    }
    created = ledger.configure_price(**values)
    replayed = ledger.configure_price(**values)
    assert replayed["id"] == created["id"]
    assert len(ledger.list_prices(brand["id"])) == 1
    with pytest.raises(ValueError, match="new version"):
        ledger.configure_price(**{**values, "unit_price": "0.02"})
    assert len(ledger.list_prices(brand["id"])) == 1


def test_settings_api_uses_authenticated_actor_and_rejects_cross_brand_schedule(tmp_path, monkeypatch):
    database = tmp_path / "settings-api.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "settings-test")
    auth = {"Authorization": "Basic " + base64.b64encode(b"operator:settings-test").decode()}
    with TestClient(app, headers=auth) as client:
        assert client.patch("/api/brands/demo-brand/settings", json={
            "voice": "Spoofed", "reason": "Spoof actor attempt", "actor": "agent",
        }).status_code == 422
        updated = client.patch("/api/brands/demo-brand/settings", json={
            "voice": "Direct, skeptical, and useful", "reason": "Adopt the reviewed house voice.",
        })
        assert updated.status_code == 200
        settings = client.get("/api/brands/demo-brand/settings").json()
        assert settings["brand"]["voice"] == "Direct, skeptical, and useful"
        assert settings["audit"][-1]["actor"] == "chris"
        assert client.put(
            "/api/brands/demo-brand/orchestration/schedules/not-this-brand",
            json={"enabled": False},
        ).status_code == 404


def test_settings_api_orchestration_status_is_brand_scoped(tmp_path, monkeypatch):
    database = tmp_path / "settings-status.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "settings-status-test")
    auth = {"Authorization": "Basic " + base64.b64encode(b"operator:settings-status-test").decode()}
    with TestClient(app, headers=auth) as client:
        points = store.get_brand("demo-brand")
        other = store.create_brand({
            "slug": "other-status", "name": "Other", "mission": "Other mission",
            "voice": "Other voice", "compliance_rules": "Other rules",
            "approval_policy": "human_approval_required",
        })
        other_account = store.upsert_connector_account(
            other["id"], "rss", "https://other.test/feed", "Other feed",
            status="healthy", scopes=[], capabilities=["content.read"],
        )
        scheduler = __import__("brandman.main", fromlist=["periodic_orchestrator"]).periodic_orchestrator
        scheduler.ensure_schedule(
            "other-only", name="Other only", action_type="connector_sync",
            interval_seconds=900,
            payload={"brand_id": other["id"], "connector_account_id": other_account["id"],
                     "stream": "content"},
            brand_id=other["id"], connector_account_id=other_account["id"],
            next_run_at="2020-01-01T00:00:00+00:00",
        )
        settings = client.get("/api/brands/demo-brand/settings").json()
        assert settings["orchestration"]["schedules"]["total"] == 0
        assert settings["orchestration"]["pending_decisions"] == 0
        assert settings["orchestration"]["latest_tick"] is None
        refused = client.put(
            "/api/brands/demo-brand/orchestration/schedules/other-only",
            json={"enabled": False},
        )
        assert refused.status_code == 404
        assert scheduler.list_schedules(brand_id=other["id"])[0]["enabled"] is True
        assert points["id"] != other["id"]
