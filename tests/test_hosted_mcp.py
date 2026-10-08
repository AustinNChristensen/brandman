from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from brandman.main import app


PASSWORD = "migration-test-password"
RESOURCE = "https://usebrandman.com/mcp"
REDIRECT_URI = "http://127.0.0.1:43117/callback"


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", PASSWORD)
    monkeypatch.setenv("BRAND_OS_ALLOWED_HOSTS", "usebrandman.com")
    monkeypatch.setenv("BRAND_OS_REQUIRE_HTTPS", "true")
    return TestClient(app, base_url="https://usebrandman.com")


def test_hosted_mcp_discovery_matches_live_contract(monkeypatch):
    with _client(monkeypatch) as client:
        protected = client.get("/.well-known/oauth-protected-resource/mcp")
        authorization = client.get("/.well-known/oauth-authorization-server")
        unauthorized = client.get("/mcp")

    assert protected.json() == {
        "resource": RESOURCE,
        "authorization_servers": ["https://usebrandman.com"],
        "scopes_supported": ["brandman:mcp"],
        "bearer_methods_supported": ["header"],
    }
    assert authorization.json()["token_endpoint"] == "https://usebrandman.com/oauth/token"
    assert authorization.json()["code_challenge_methods_supported"] == ["S256"]
    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == (
        'Bearer resource_metadata="https://usebrandman.com/.well-known/oauth-protected-resource/mcp"'
    )


def test_hosted_mcp_pkce_exchange_and_revocation(monkeypatch):
    verifier = "a-long-enough-pkce-verifier-value-for-the-test"
    with _client(monkeypatch) as client:
        registration = client.post("/oauth/register", json={"redirect_uris": [REDIRECT_URI]})
        client_id = registration.json()["client_id"]
        params = {
            "response_type": "code", "client_id": client_id,
            "redirect_uri": REDIRECT_URI, "state": "opaque-state",
            "resource": RESOURCE, "scope": "brandman:mcp",
            "code_challenge": _challenge(verifier), "code_challenge_method": "S256",
        }
        consent = client.post("/oauth/authorize", params=params, data={"password": PASSWORD}, follow_redirects=False)
        query = parse_qs(urlparse(consent.headers["location"]).query)
        code = query["code"][0]
        exchange = client.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code,
            "code_verifier": verifier, "client_id": client_id,
            "redirect_uri": REDIRECT_URI, "resource": RESOURCE,
        })
        payload = exchange.json()
        duplicate = client.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code,
            "code_verifier": verifier, "client_id": client_id,
            "redirect_uri": REDIRECT_URI, "resource": RESOURCE,
        })
        revoked = client.post("/oauth/revoke", data={"token": payload["access_token"]})
        denied = client.get("/mcp", headers={"Authorization": f"Bearer {payload['access_token']}"})

    assert registration.status_code == 201
    assert consent.status_code == 303
    assert query["state"] == ["opaque-state"]
    assert exchange.status_code == 200
    assert payload["scope"] == "brandman:mcp"
    assert "refresh_token" in payload and "access_token" in payload
    assert duplicate.status_code == 400 and duplicate.json() == {"error": "invalid_grant"}
    assert revoked.status_code == 200
    assert denied.status_code == 401


def _authorize_code(client, verifier, redirect_uri=REDIRECT_URI):
    registration = client.post("/oauth/register", json={"redirect_uris": [redirect_uri]})
    client_id = registration.json()["client_id"]
    params = {
        "response_type": "code", "client_id": client_id,
        "redirect_uri": redirect_uri, "state": "s",
        "resource": RESOURCE, "scope": "brandman:mcp",
        "code_challenge": _challenge(verifier), "code_challenge_method": "S256",
    }
    consent = client.post("/oauth/authorize", params=params, data={"password": PASSWORD}, follow_redirects=False)
    code = parse_qs(urlparse(consent.headers["location"]).query)["code"][0]
    return client_id, code


def _code_form(client_id, code, verifier):
    return {
        "grant_type": "authorization_code", "code": code,
        "code_verifier": verifier, "client_id": client_id,
        "redirect_uri": REDIRECT_URI, "resource": RESOURCE,
    }


def test_concurrent_code_redemption_yields_exactly_one_token(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    verifier = "a-long-enough-pkce-verifier-value-for-the-test"
    with _client(monkeypatch) as client:
        client_id, code = _authorize_code(client, verifier)
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(
                lambda _: client.post("/oauth/token", data=_code_form(client_id, code, verifier)),
                range(8),
            ))

    assert sorted(r.status_code for r in responses).count(200) == 1
    assert all(r.json() == {"error": "invalid_grant"} for r in responses if r.status_code != 200)


def test_refresh_token_rotates_once_and_replay_fails(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    verifier = "a-long-enough-pkce-verifier-value-for-the-test"
    with _client(monkeypatch) as client:
        client_id, code = _authorize_code(client, verifier)
        first = client.post("/oauth/token", data=_code_form(client_id, code, verifier)).json()
        form = {
            "grant_type": "refresh_token", "refresh_token": first["refresh_token"],
            "client_id": client_id, "resource": RESOURCE,
        }
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: client.post("/oauth/token", data=form), range(8)))
        replay = client.post("/oauth/token", data=form)

    assert sorted(r.status_code for r in responses).count(200) == 1
    assert replay.status_code == 400 and replay.json() == {"error": "invalid_grant"}


def test_wrong_verifier_and_wrong_redirect_do_not_redeem_the_code(monkeypatch):
    verifier = "a-long-enough-pkce-verifier-value-for-the-test"
    with _client(monkeypatch) as client:
        client_id, code = _authorize_code(client, verifier)
        wrong_verifier = client.post("/oauth/token", data=_code_form(client_id, code, "x" * 50))
        wrong_redirect = client.post("/oauth/token", data={**_code_form(client_id, code, verifier), "redirect_uri": "http://127.0.0.1:1/other"})
        good = client.post("/oauth/token", data=_code_form(client_id, code, verifier))

    assert wrong_verifier.status_code == 400
    assert wrong_redirect.status_code == 400
    assert good.status_code == 200


def test_consent_screen_describes_write_capable_access(monkeypatch):
    verifier = "a-long-enough-pkce-verifier-value-for-the-test"
    with _client(monkeypatch) as client:
        client_id = client.post("/oauth/register", json={"redirect_uris": [REDIRECT_URI]}).json()["client_id"]
        page = client.get("/oauth/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI,
            "state": "s", "resource": RESOURCE, "scope": "brandman:mcp",
            "code_challenge": _challenge(verifier), "code_challenge_method": "S256",
        })

    assert page.status_code == 200
    assert "read-only" not in page.text
    assert "create or edit" in page.text


def test_authorize_password_attempts_are_rate_limited(monkeypatch):
    from brandman import hosted_mcp

    hosted_mcp._auth_failures.clear()
    verifier = "a-long-enough-pkce-verifier-value-for-the-test"
    with _client(monkeypatch) as client:
        client_id = client.post("/oauth/register", json={"redirect_uris": [REDIRECT_URI]}).json()["client_id"]
        params = {
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI,
            "state": "s", "resource": RESOURCE, "scope": "brandman:mcp",
            "code_challenge": _challenge(verifier), "code_challenge_method": "S256",
        }
        statuses = [
            client.post("/oauth/authorize", params=params, data={"password": "wrong"}, follow_redirects=False).status_code
            for _ in range(hosted_mcp.AUTH_FAILURE_LIMIT + 1)
        ]
        locked_out = client.post("/oauth/authorize", params=params, data={"password": PASSWORD}, follow_redirects=False)
    hosted_mcp._auth_failures.clear()

    assert statuses[:-1] == [200] * hosted_mcp.AUTH_FAILURE_LIMIT
    assert statuses[-1] == 429
    assert locked_out.status_code == 429

