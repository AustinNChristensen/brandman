import base64
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

os.environ["BRAND_OS_PREVIEW_PASSWORD"] = "test-only-password"

from app.main import app
from app import store


HEADERS = {
    "Authorization": "Basic " + base64.b64encode(b"operator:test-only-password").decode()
}


@pytest.fixture(autouse=True)
def isolated_connection_database(tmp_path, monkeypatch):
    database = tmp_path / "connection-api.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    monkeypatch.setenv("BRAND_OS_DB", str(database))


def test_connection_writes_fail_closed_without_master_key(monkeypatch):
    monkeypatch.delenv("BRAND_OS_CREDENTIAL_MASTER_KEY", raising=False)
    with TestClient(app, headers=HEADERS) as client:
        response = client.put(
            "/api/connections/x/no-key",
            json={"display_name": "No key", "credentials": {"access_token": "must-not-leak"}},
        )
    assert response.status_code == 503
    assert "BRAND_OS_CREDENTIAL_MASTER_KEY" in response.json()["detail"]
    assert "must-not-leak" not in response.text


def test_connect_update_and_list_are_redacted(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BRAND_OS_CREDENTIAL_MASTER_KEY", key)
    token = "x-secret-access-381174"
    refresh = "x-secret-refresh-921274"
    account = f"demo-brand-api-test-{uuid4()}"
    with TestClient(app, headers=HEADERS) as client:
        connected = client.put(
            f"/api/connections/x/{account}",
            json={
                "display_name": "Demo Brand X",
                "credentials": {"access_token": token, "refresh_token": refresh},
                "required_scopes": ["tweet.read", "users.read", "tweet.write"],
                "granted_scopes": ["tweet.read", "users.read"],
            },
        )
        assert connected.status_code == 200
        metadata = connected.json()
        assert metadata["credential_revision"] == 1
        assert metadata["scope_status"] == "insufficient"
        assert metadata["missing_scopes"] == ["tweet.write"]
        assert metadata["has_credentials"] is True

        listed = client.get("/api/connections?provider=x")
        fetched = client.get(f"/api/connections/x/{account}")
        for response in (connected, listed, fetched):
            assert token not in response.text
            assert refresh not in response.text
            assert "encrypted_payload" not in response.text
            assert "access_token" not in response.text

        updated = client.put(
            f"/api/connections/x/{account}",
            json={
                "display_name": "Demo Brand X",
                "credentials": {"access_token": "replacement-secret"},
                "required_scopes": ["tweet.read", "users.read", "tweet.write"],
                "granted_scopes": ["tweet.read", "users.read", "tweet.write"],
            },
        ).json()
        assert updated["credential_revision"] == 2
        assert updated["scope_status"] == "least_privilege"

    database = Path(os.environ["BRAND_OS_DB"])
    with sqlite3.connect(database) as connection:
        encrypted = connection.execute(
            "SELECT encrypted_payload FROM connector_credentials WHERE provider='x' AND account_id=?",
            (account,),
        ).fetchone()[0]
    assert token.encode() not in encrypted
    assert refresh.encode() not in encrypted
    assert b"replacement-secret" not in encrypted


def test_health_reconnect_disconnect_and_authenticated_audit(monkeypatch):
    monkeypatch.setenv("BRAND_OS_CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())
    account = f"demo-brand-beehiiv-api-test-{uuid4()}"
    with TestClient(app, headers=HEADERS) as client:
        client.put(
            f"/api/connections/beehiiv/{account}",
            json={"display_name": "Demo Brand Beehiiv", "credentials": {"api_key": "hidden"}},
        )
        health = client.patch(
            f"/api/connections/beehiiv/{account}/health",
            json={"status": "reconnect_required", "error_code": "oauth.token_expired"},
        ).json()
        assert health["reconnect_required"] is True
        assert health["last_error_code"] == "oauth.token_expired"
        reconnect = client.get(
            f"/api/connections/beehiiv/{account}/reconnect-status"
        ).json()
        assert reconnect["reconnect_required"] is True
        assert reconnect["has_credentials"] is True

        spoof = client.post(
            f"/api/connections/beehiiv/{account}/disconnect", json={"actor": "mallory"}
        )
        assert spoof.status_code == 200
        assert spoof.json()["status"] == "disconnected"
        assert spoof.json()["has_credentials"] is False
        assert client.get(
            f"/api/connections/beehiiv/{account}/reconnect-status"
        ).json()["reconnect_required"] is True

    with sqlite3.connect(os.environ["BRAND_OS_DB"]) as connection:
        actors = [row[0] for row in connection.execute(
            "SELECT actor FROM credential_audit WHERE provider='beehiiv' AND account_id=? ORDER BY sequence",
            (account,),
        )]
    assert actors == ["preview-operator", "preview-operator", "preview-operator"]


def test_connection_management_is_not_exposed_to_mcp():
    from app.mcp_server import mcp

    tools = set(mcp._tool_manager._tools)
    assert not any("credential" in name or name.startswith("connect_account") for name in tools)


def test_dashboard_x_writer_contract_is_separate_least_privilege_and_redacted(monkeypatch):
    monkeypatch.setenv("BRAND_OS_CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())
    account = f"demo-brand-x-writer-{uuid4()}"
    exact_scopes = ["tweet.read", "tweet.write", "users.read", "offline.access"]
    secret = "writer-secret-never-return"
    with TestClient(app, headers=HEADERS) as client:
        connector = client.post("/api/brands/demo-brand/connectors", json={
            "connector_type": "x", "account_key": account,
            "display_name": "Demo Brand X writer", "status": "needs_attention",
            "scopes": exact_scopes, "capabilities": ["posts.write"],
            "configuration": {},
        })
        assert connector.status_code == 201
        assert connector.json()["scopes"] == exact_scopes

        connected = client.put(f"/api/connections/x/{account}", json={
            "display_name": "Demo Brand X writer",
            "credentials": {
                "access_token": secret, "refresh_token": "writer-refresh",
                "client_id": "writer-client", "expires_at": "2027-09-02T00:00:00Z",
            },
            "required_scopes": exact_scopes,
            "granted_scopes": exact_scopes,
        })
        assert connected.status_code == 200
        assert connected.json()["scope_status"] == "least_privilege"
        assert connected.json()["missing_scopes"] == []
        assert connected.json()["excessive_scopes"] == []
        assert secret not in connected.text
        assert "access_token" not in connected.text

        health = client.post("/api/brands/demo-brand/connector-health-checks", json={
            "connector_account_id": connector.json()["id"], "timeout_seconds": 20,
        })
        assert health.status_code == 202
        assert health.json()[0]["status"] == "not_probeable"
        assert health.json()[0]["error_code"] == "scope.read_write_not_separated"
        assert store.rows(
            "SELECT id FROM durable_jobs WHERE connector_account_id=? AND job_type='connector.health_check'",
            (connector.json()["id"],),
        ) == []

        disconnected = client.post(f"/api/connections/x/{account}/disconnect")
        assert disconnected.status_code == 200
        assert disconnected.json()["has_credentials"] is False
        assert secret not in disconnected.text


def test_browser_assisted_delivery_requires_no_api_credential(monkeypatch):
    monkeypatch.delenv("BRAND_OS_CREDENTIAL_MASTER_KEY", raising=False)
    account = f"demo-brand-assisted-x-{uuid4()}"
    with TestClient(app, headers=HEADERS) as client:
        connected = client.post("/api/brands/demo-brand/connectors", json={
            "connector_type": "x", "account_key": account,
            "display_name": "Demo Brand assisted X delivery", "status": "connected",
            "scopes": [], "capabilities": ["browser.assisted", "x_write"],
            "configuration": {
                "delivery_mode": "browser_assisted", "connection_role": "x_write",
            },
        })
        assert connected.status_code == 201
        payload = connected.json()
        assert payload["status"] == "connected"
        assert payload["scopes"] == []
        assert payload["configuration"] == {
            "delivery_mode": "browser_assisted", "connection_role": "x_write",
        }
        assert "credential" not in connected.text.lower()
        readiness = client.get("/api/brands/demo-brand/readiness")
        assert readiness.status_code == 200
        report = readiness.json()
        required = [check for check in report["checks"] if check.get("required_for_live", True)]
        assert all(check["id"] not in {"x_read", "x_write"} for check in required)
        assert next(
            check for check in report["checks"] if check["id"] == "credential_master_key"
        )["required_for_live"] is False
