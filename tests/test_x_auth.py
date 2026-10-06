from datetime import UTC, datetime
import json

import pytest
from cryptography.fernet import Fernet

from app.connectors import ConnectorError, HttpResponse
from app.credentials import CredentialStore
from app.x_auth import XOAuthRefreshError, XOAuthTokenSupplier


NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)
SCOPES = ["tweet.read", "users.read", "tweet.write", "offline.access"]


class Transport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def credentials(tmp_path, payload):
    key = Fernet.generate_key().decode()
    repository = CredentialStore(tmp_path / "oauth.db", key)
    repository.put(
        "x", "writer", "X writer", payload,
        required_scopes=SCOPES, granted_scopes=SCOPES,
    )
    return repository


def test_future_access_token_does_not_call_refresh_endpoint(tmp_path):
    repository = credentials(tmp_path, {
        "access_token": "current-access", "refresh_token": "current-refresh",
        "client_id": "client", "expires_at": "2026-09-02T14:00:00Z",
    })
    transport = Transport(HttpResponse(500, b"{}"))
    supplier = XOAuthTokenSupplier(repository, "writer", transport, clock=lambda: NOW)
    assert supplier() == "Bearer current-access"
    assert transport.calls == []


def test_expiring_token_refreshes_and_rotates_encrypted_credentials(tmp_path):
    repository = credentials(tmp_path, {
        "access_token": "old-access", "refresh_token": "old-refresh",
        "client_id": "client-id", "client_secret": "client-secret",
        "expires_at": "2026-09-02T12:01:00Z",
    })
    transport = Transport(HttpResponse(200, json.dumps({
        "access_token": "new-access", "refresh_token": "new-refresh",
        "expires_in": 7200,
    }).encode()))
    supplier = XOAuthTokenSupplier(repository, "writer", transport, clock=lambda: NOW)

    assert supplier() == "Bearer new-access"
    method, url, kwargs = transport.calls[0]
    assert method == "POST"
    assert url.endswith("/oauth2/token")
    assert kwargs["form_body"] == {
        "grant_type": "refresh_token", "refresh_token": "old-refresh",
        "client_id": "client-id",
    }
    assert kwargs["headers"]["Authorization"].startswith("Basic ")
    rotated = repository.secret("x", "writer").reveal()
    assert rotated["access_token"] == "new-access"
    assert rotated["refresh_token"] == "new-refresh"
    assert rotated["expires_at"] == "2026-09-02T14:00:00+00:00"
    assert repository.get("x", "writer").credential_revision == 2
    audit = repository.audit("x", "writer")
    assert audit[-1].actor == "system:x-oauth-refresh"


def test_refresh_failure_is_safe_and_does_not_rotate(tmp_path):
    repository = credentials(tmp_path, {
        "access_token": "old-access", "refresh_token": "sensitive-refresh",
        "client_id": "client", "expires_at": "2026-09-02T11:00:00Z",
    })
    transport = Transport(HttpResponse(401, b'{"error":"nope"}'))
    supplier = XOAuthTokenSupplier(repository, "writer", transport, clock=lambda: NOW)
    with pytest.raises(ConnectorError) as caught:
        supplier()
    assert caught.value.status_code == 401
    assert "sensitive-refresh" not in str(caught.value)
    assert repository.get("x", "writer").credential_revision == 1


def test_expired_token_without_refresh_material_fails_closed(tmp_path):
    repository = credentials(tmp_path, {
        "access_token": "old-access", "expires_at": "2026-09-02T11:00:00Z",
    })
    supplier = XOAuthTokenSupplier(
        repository, "writer", Transport(HttpResponse(200, b"{}")), clock=lambda: NOW,
    )
    with pytest.raises(XOAuthRefreshError, match="missing refresh_token"):
        supplier()


def test_even_unexpired_native_token_requires_durable_refresh_material(tmp_path):
    repository = credentials(tmp_path, {
        "access_token": "current-access", "expires_at": "2026-09-02T14:00:00Z",
    })
    supplier = XOAuthTokenSupplier(
        repository, "writer", Transport(HttpResponse(200, b"{}")), clock=lambda: NOW,
    )
    with pytest.raises(XOAuthRefreshError, match="missing refresh_token"):
        supplier()
