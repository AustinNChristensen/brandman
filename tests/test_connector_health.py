import base64
import json
from pathlib import Path
import threading

from fastapi.testclient import TestClient
import pytest

from brandman import store
from brandman.connector_health import (
    ConnectorHealthStore, HEALTH_CHECK_JOB_TYPE, make_connector_health_handler,
)
from brandman.connectors import ConnectorError, ConnectorKind, ConnectorResult
from brandman.main import app


HEADERS = {"Authorization": "Basic " + base64.b64encode(
    b"operator:test-only-password"
).decode()}


def setup_health(tmp_path):
    database = tmp_path / "health.db"
    store.DATA_PATH = database
    store.init_db()
    brand = store.get_brand("demo-brand")
    account = store.upsert_connector_account(
        brand["id"], "rss", "https://feed.example.test/rss", "Feed",
        status="connected", capabilities=["content.read"],
    )
    return database, brand, account, ConnectorHealthStore(database)


def job_for(check):
    return {"payload": {"health_check_id": check["id"]}}


def test_read_probe_is_queued_then_durably_audited_healthy(tmp_path):
    _, brand, account, health = setup_health(tmp_path)
    calls = []

    class Connector:
        def sync(self, cursor):
            calls.append(cursor)
            return ConnectorResult()

    check = health.trigger(brand["id"], actor="chris")[0]
    assert check["status"] == "queued"
    assert check["provider_responded"] is False
    assert calls == []
    completed = make_connector_health_handler({account["id"]: Connector()}, health)(
        job_for(check)
    )

    assert completed["status"] == "healthy"
    assert completed["provider_responded"] is True
    assert completed["response_status"] is None  # connector abstraction only proves successful response
    assert calls == [None]
    assert [item["action"] for item in completed["audit"]] == [
        "queued", "started", "completed",
    ]
    metadata = store.row("SELECT * FROM connector_accounts WHERE id=?", (account["id"],))
    assert metadata["status"] == "healthy"
    assert metadata["health_checked_at"] == completed["completed_at"]


def test_provider_error_is_redacted_and_only_response_can_claim_unhealthy(tmp_path):
    database, brand, account, health = setup_health(tmp_path)

    class Connector:
        def sync(self, cursor):
            raise ConnectorError(ConnectorKind.RSS, "feed auth token-do-not-store", 401)

    check = health.trigger(brand["id"], actor="chris")[0]
    completed = make_connector_health_handler({account["id"]: Connector()}, health)(job_for(check))
    persisted = database.read_bytes()

    assert completed["status"] == "unhealthy"
    assert completed["provider_responded"] is True
    assert completed["response_status"] == 401
    assert "token-do-not-store" not in completed["error_code"]
    assert b"token-do-not-store" not in persisted
    assert store.row("SELECT status FROM connector_accounts WHERE id=?", (account["id"],))["status"] == "degraded"


def test_timeout_is_bounded_and_does_not_make_a_provider_health_claim(tmp_path):
    _, brand, account, health = setup_health(tmp_path)
    release = threading.Event()

    class Connector:
        def sync(self, cursor):
            release.wait(10)

    check = health.trigger(brand["id"], actor="chris", timeout_seconds=1)[0]
    completed = make_connector_health_handler({account["id"]: Connector()}, health)(job_for(check))
    release.set()

    assert completed["status"] == "timed_out"
    assert completed["provider_responded"] is False
    assert completed["error_code"] == "probe.timeout"
    assert store.row("SELECT status FROM connector_accounts WHERE id=?", (account["id"],))["status"] == "connected"


def test_write_only_x_is_audited_not_probeable_and_never_enqueued(tmp_path):
    _, brand, _, health = setup_health(tmp_path)
    x_write = store.upsert_connector_account(
        brand["id"], "x", "demobrand-write", "X write",
        status="healthy", scopes=["tweet.write"],
    )
    checks = health.trigger(
        brand["id"], actor="chris", connector_account_id=x_write["id"],
    )

    assert checks[0]["status"] == "not_probeable"
    assert checks[0]["provider_responded"] is False
    assert checks[0]["error_code"] == "scope.read_write_not_separated"
    assert store.rows(
        "SELECT * FROM durable_jobs WHERE job_type=? AND connector_account_id=?",
        (HEALTH_CHECK_JOB_TYPE, x_write["id"]),
    ) == []


def test_unregistered_connector_fails_without_changing_account_health(tmp_path):
    _, brand, account, health = setup_health(tmp_path)
    check = health.trigger(brand["id"], actor="chris")[0]
    completed = make_connector_health_handler({}, health)(job_for(check))
    assert completed["status"] == "not_configured"
    assert completed["provider_responded"] is False
    assert store.row("SELECT status FROM connector_accounts WHERE id=?", (account["id"],))["status"] == "connected"


def test_store_rejects_provider_health_claim_without_response(tmp_path):
    _, brand, _, health = setup_health(tmp_path)
    check = health.trigger(brand["id"], actor="chris")[0]
    health.start(check["id"], actor="worker")
    with pytest.raises(ValueError, match="without a response"):
        health.complete(
            check["id"], status="healthy", actor="worker", provider_responded=False,
        )


def test_rest_trigger_is_authenticated_and_mcp_surface_will_be_status_only(tmp_path, monkeypatch):
    database, brand, account, _ = setup_health(tmp_path)
    monkeypatch.setattr(store, "DATA_PATH", database)
    monkeypatch.setenv("BRANDMAN_PREVIEW_PASSWORD", "test-only-password")
    with TestClient(app) as client:
        denied = client.post(f"/api/brands/demo-brand/connector-health-checks")
        triggered = client.post(
            "/api/brands/demo-brand/connector-health-checks",
            json={"connector_account_id": account["id"], "timeout_seconds": 5},
            headers=HEADERS,
        )
        listed = client.get(
            "/api/brands/demo-brand/connector-health-checks", headers=HEADERS,
        )
    assert denied.status_code == 401
    assert triggered.status_code == 202
    assert triggered.json()[0]["requested_by"] == "chris"
    assert listed.json()[0]["id"] == triggered.json()[0]["id"]
    assert "credential" not in json.dumps(listed.json()).lower()
    from brandman.mcp_server import list_connector_health_checks
    mcp_status = list_connector_health_checks("demo-brand")
    assert mcp_status[0]["id"] == triggered.json()[0]["id"]
