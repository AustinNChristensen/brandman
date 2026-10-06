import base64
from datetime import UTC, datetime
import json
import sqlite3

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from app import store
from app.credentials import CredentialStore
from app.connector_health import ConnectorHealthStore, make_connector_health_handler
from app.connectors import ConnectorResult
from app.main import app
from app.readiness import LiveReadinessService
from app.scheduler import PeriodicOrchestrator


NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)
HEADERS = {
    "Authorization": "Basic "
    + base64.b64encode(b"operator:test-only-password").decode()
}


pytestmark = pytest.mark.usefixtures("launch_window_clock")


def setup_database(tmp_path, monkeypatch):
    database = tmp_path / "readiness.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    monkeypatch.setenv("BRAND_OS_DB", str(database))
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "test-only-password")
    store.init_db()
    store.ensure_demo_brand_growth_mission()
    return database, store.get_brand("demo-brand")


def checks(report):
    return {item["id"]: item for item in report["checks"]}


def test_empty_preflight_distinguishes_code_gaps_from_missing_configuration(tmp_path, monkeypatch):
    database, _ = setup_database(tmp_path, monkeypatch)
    monkeypatch.delenv("BRAND_OS_CREDENTIAL_MASTER_KEY", raising=False)

    report = LiveReadinessService(
        database, environment=dict(__import__("os").environ), clock=lambda: NOW,
    ).inspect("demo-brand")
    result = checks(report)

    assert report["ready"] is False
    assert result["preview_auth"]["status"] == "ready"
    assert result["credential_master_key"]["status"] == "not_configured"
    assert result["beehiiv_read"]["status"] == "not_configured"
    assert result["beehiiv_read"]["missing_scopes"] == ["posts.read"]
    assert result["x_read"]["status"] == "not_configured"
    assert result["website_analytics"]["status"] == "not_configured"
    assert result["active_mission"]["status"] == "ready"
    assert result["worker_schedules"]["status"] == "not_configured"
    assert report["summary"]["code_ready_percent"] == 100.0
    assert any("BRAND_OS_CREDENTIAL_MASTER_KEY" in action
               for action in result["credential_master_key"]["actions"])


def test_readiness_does_not_recommend_reconnecting_quarantined_fixture_account(
    tmp_path, monkeypatch,
):
    database, brand = setup_database(tmp_path, monkeypatch)
    fixture = store.upsert_connector_account(
        brand["id"], "x", "read-no-inbox-api", "X fixture account",
        status="disconnected", scopes=[],
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO fixture_quarantine_registry
               (table_name,record_key_json,manifest_sha256,actor,reason,quarantined_at)
               VALUES ('connector_accounts',?,'sha256:test','qa','fixture',?)""",
            (json.dumps([fixture["id"]], separators=(",", ":")), NOW.isoformat()),
        )

    report = LiveReadinessService(
        database, environment=dict(__import__("os").environ), clock=lambda: NOW,
    ).inspect("demo-brand")

    for check_id in ("x_read", "x_write"):
        check = checks(report)[check_id]
        assert check["account_id"] is None
        assert fixture["id"] not in json.dumps(check)
        assert "read-no-inbox-api" not in json.dumps(check)


def test_connected_preflight_mirrors_runtime_scope_rules_and_preserves_code_gaps(tmp_path, monkeypatch):
    database, brand = setup_database(tmp_path, monkeypatch)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BRAND_OS_CREDENTIAL_MASTER_KEY", key)
    credentials = CredentialStore(database, key)
    credentials.put(
        "beehiiv", "publication-1", "DemoBrand Beehiiv", {"api_key": "bee-secret"},
        required_scopes=["posts.read", "posts.write"],
        granted_scopes=["posts.read", "posts.write"],
    )
    credentials.put(
        "x", "demobrand-read", "DemoBrand X read", {
            "access_token": "x-read-value", "refresh_token": "read-refresh-value",
            "client_id": "read-client", "expires_at": "2027-09-02T00:00:00Z",
        },
        required_scopes=["tweet.read", "users.read", "offline.access"],
        granted_scopes=["tweet.read", "users.read", "offline.access"],
    )
    credentials.put(
        "x", "demobrand-write", "DemoBrand X write", {
            "access_token": "x-write-value", "refresh_token": "write-refresh-value",
            "client_id": "write-client", "expires_at": "2027-09-02T00:00:00Z",
        },
        required_scopes=["tweet.read", "users.read", "tweet.write", "offline.access"],
        granted_scopes=["tweet.read", "users.read", "tweet.write", "offline.access"],
    )
    credentials.put(
        "website", "demo.example", "DemoBrand analytics", {"access_token": "web-value"},
        required_scopes=["analytics.read"], granted_scopes=["analytics.read"],
    )
    beehiiv = store.upsert_connector_account(
        brand["id"], "beehiiv", "publication-1", "DemoBrand Beehiiv",
        status="healthy", scopes=["posts.read", "posts.write"],
    )
    x_read = store.upsert_connector_account(
        brand["id"], "x", "demobrand-read", "DemoBrand X read",
        status="healthy", scopes=["tweet.read", "users.read", "offline.access"],
        configuration={"user_id": "42", "username": "demobrand"},
    )
    store.upsert_connector_account(
        brand["id"], "x", "demobrand-write", "DemoBrand X write",
        status="healthy",
        scopes=["tweet.read", "users.read", "tweet.write", "offline.access"],
    )
    website = store.upsert_connector_account(
        brand["id"], "website", "demo.example", "DemoBrand Website",
        status="healthy", scopes=["analytics.read"],
        configuration={"endpoint_url": "https://demo.example/api/analytics"},
    )
    scheduler = PeriodicOrchestrator(database, clock=lambda: NOW)
    scheduler.ensure_defaults()
    scheduler.tick()
    health = ConnectorHealthStore(database)

    class HealthyReadConnector:
        def sync(self, cursor):
            return ConnectorResult()

    handler = make_connector_health_handler({
        beehiiv["id"]: HealthyReadConnector(),
        x_read["id"]: HealthyReadConnector(),
        website["id"]: HealthyReadConnector(),
    }, health)
    for check in health.trigger(brand["id"], actor="chris"):
        if check["status"] == "queued":
            handler({"payload": {"health_check_id": check["id"]}})

    report = LiveReadinessService(
        database, environment=dict(__import__("os").environ), clock=lambda: NOW,
    ).inspect("demo-brand")
    result = checks(report)

    assert result["credential_master_key"]["status"] == "ready"
    assert "secret" not in json.dumps(report).lower()
    assert result["beehiiv_read"]["status"] == "ready"
    assert result["beehiiv_write"]["status"] == "ready"
    assert result["beehiiv_read"]["account_id"] == beehiiv["id"]
    assert result["x_write"]["status"] == "unhealthy"
    assert result["x_write"]["account_connected"] is True
    assert any("not_probeable" in action for action in result["x_write"]["actions"])
    assert result["x_read"]["account_connected"] is True
    assert result["x_read"]["status"] == "ready"
    assert result["website_analytics"]["account_connected"] is True
    assert result["website_analytics"]["account_id"] == website["id"]
    assert result["website_analytics"]["status"] == "ready"
    assert result["worker_schedules"]["status"] == "ready"
    assert result["worker_schedules"]["schedules_configured"] is True
    assert result["worker_schedules"]["worker_healthy"] is True
    assert result["worker_schedules"]["heartbeat_age_seconds"] == 0


def test_wrong_but_well_formed_key_is_reported_without_decryption_error_or_secret(tmp_path, monkeypatch):
    database, brand = setup_database(tmp_path, monkeypatch)
    original_key = Fernet.generate_key().decode()
    CredentialStore(database, original_key).put(
        "x", "demobrand", "DemoBrand X", {"access_token": "never-return-this"},
        required_scopes=["tweet.write"], granted_scopes=["tweet.write"],
    )
    store.upsert_connector_account(
        brand["id"], "x", "demobrand", "DemoBrand X",
        status="healthy", scopes=["tweet.write"],
    )
    environment = {
        "BRAND_OS_PREVIEW_PASSWORD": "configured",
        "BRAND_OS_CREDENTIAL_MASTER_KEY": Fernet.generate_key().decode(),
    }

    report = LiveReadinessService(database, environment=environment, clock=lambda: NOW).inspect(
        "demo-brand"
    )
    result = checks(report)
    serialized = json.dumps(report)

    assert result["credential_master_key"]["status"] == "unhealthy"
    assert result["x_write"]["account_connected"] is False
    assert "never-return-this" not in serialized
    assert "encrypted_payload" not in serialized
    assert "InvalidToken" not in serialized


def test_rest_preflight_is_authenticated_and_mcp_helper_is_read_only(tmp_path, monkeypatch):
    database, _ = setup_database(tmp_path, monkeypatch)
    monkeypatch.delenv("BRAND_OS_CREDENTIAL_MASTER_KEY", raising=False)
    with TestClient(app) as client:
        assert client.get("/api/brands/demo-brand/readiness").status_code == 401
        response = client.get("/api/brands/demo-brand/readiness", headers=HEADERS)
        missing = client.get("/api/brands/nope/readiness", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["brand"]["slug"] == "demo-brand"
    assert missing.status_code == 404
    before = store.rows("SELECT id FROM connector_accounts")
    from app.mcp_server import get_live_readiness
    mcp_report = get_live_readiness("demo-brand")
    assert mcp_report["brand"]["slug"] == "demo-brand"
    assert store.rows("SELECT id FROM connector_accounts") == before


def test_missing_scope_actions_name_exact_scope(tmp_path, monkeypatch):
    database, brand = setup_database(tmp_path, monkeypatch)
    key = Fernet.generate_key().decode()
    environment = {
        "BRAND_OS_PREVIEW_PASSWORD": "configured",
        "BRAND_OS_CREDENTIAL_MASTER_KEY": key,
    }
    CredentialStore(database, key).put(
        "x", "demobrand", "DemoBrand X", {"access_token": "hidden"},
        required_scopes=["tweet.read", "users.read", "tweet.write", "offline.access"],
        granted_scopes=["tweet.read", "users.read", "offline.access"],
    )
    store.upsert_connector_account(
        brand["id"], "x", "demobrand", "DemoBrand X",
        status="healthy", scopes=["tweet.read", "users.read", "offline.access"],
    )

    report = LiveReadinessService(database, environment=environment, clock=lambda: NOW).inspect(
        "demo-brand"
    )
    write = checks(report)["x_write"]
    assert write["missing_scopes"] == ["tweet.write"]
    assert any("tweet.write" in action for action in write["actions"])


def test_native_x_api_never_claims_connected_without_refresh_material(tmp_path, monkeypatch):
    database, brand = setup_database(tmp_path, monkeypatch)
    key = Fernet.generate_key().decode()
    environment = {
        "BRAND_OS_PREVIEW_PASSWORD": "configured",
        "BRAND_OS_CREDENTIAL_MASTER_KEY": key,
    }
    scopes = ["tweet.read", "users.read", "offline.access"]
    CredentialStore(database, key).put(
        "x", "demobrand-read", "DemoBrand X read",
        {"access_token": "expiring-without-refresh"},
        required_scopes=scopes, granted_scopes=scopes,
    )
    store.upsert_connector_account(
        brand["id"], "x", "demobrand-read", "DemoBrand X read",
        status="healthy", scopes=scopes,
        configuration={"user_id": "42", "username": "demobrand"},
    )

    result = checks(LiveReadinessService(
        database, environment=environment, clock=lambda: NOW,
    ).inspect("demo-brand"))["x_read"]

    assert result["execution_mode"] == "api"
    assert result["account_connected"] is False
    assert result["status"] == "unhealthy"
    assert any("refresh_token" in action for action in result["actions"])


def test_unknown_brand_raises_key_error(tmp_path, monkeypatch):
    database, _ = setup_database(tmp_path, monkeypatch)
    with pytest.raises(KeyError, match="unknown brand"):
        LiveReadinessService(database, clock=lambda: NOW).inspect("unknown")


def test_schedule_rows_without_a_real_tick_are_configured_but_not_healthy(tmp_path, monkeypatch):
    database, _ = setup_database(tmp_path, monkeypatch)
    scheduler = PeriodicOrchestrator(database, clock=lambda: NOW)
    scheduler.ensure_defaults()

    report = LiveReadinessService(database, clock=lambda: NOW).inspect("demo-brand")
    worker = checks(report)["worker_schedules"]

    assert worker["configured"] is True
    assert worker["schedules_configured"] is True
    assert worker["healthy"] is False
    assert worker["worker_healthy"] is False
    assert worker["status"] == "unhealthy"
    assert worker["heartbeat_at"] is None
    assert any("no heartbeat" in action for action in worker["actions"])


def test_stale_tick_does_not_claim_worker_health(tmp_path, monkeypatch):
    database, _ = setup_database(tmp_path, monkeypatch)
    scheduler = PeriodicOrchestrator(database, clock=lambda: NOW)
    scheduler.ensure_defaults()
    scheduler.tick()
    later = datetime(2026, 9, 2, 15, tzinfo=UTC)

    report = LiveReadinessService(database, clock=lambda: later).inspect("demo-brand")
    worker = checks(report)["worker_schedules"]

    assert worker["schedules_configured"] is True
    assert worker["worker_healthy"] is False
    assert worker["heartbeat_age_seconds"] == 10_800
    assert worker["heartbeat_freshness_threshold_seconds"] == 7_200
    assert worker["status"] == "unhealthy"
    assert any("7200-second" in action for action in worker["actions"])
