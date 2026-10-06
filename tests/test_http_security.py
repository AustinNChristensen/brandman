from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from app import store
from app.http_security import (
    PREVIEW_SESSION_COOKIE,
    create_preview_session,
    deployment_boundary_status,
    validate_preview_session,
)
from app.main import app


PASSWORD = "test-only-password"
AUTH = "Basic " + base64.b64encode(f"operator:{PASSWORD}".encode()).decode()


def _brand_payload(slug: str) -> dict:
    return {
        "slug": slug, "name": "Security test", "mission": "Test boundaries",
        "voice": "Direct", "compliance_rules": "Human review required",
        "approval_policy": "human_approval_required",
    }


def test_public_homepage_is_static_and_every_other_surface_stays_gated(monkeypatch):
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", PASSWORD)
    with TestClient(app) as client:
        home = client.get("/")
        api = client.get("/api/brands")
        posted = client.post("/", json={})

    assert home.status_code == 200 and "BrandMan" in home.text
    assert "Authorization" not in home.text and PASSWORD not in home.text
    assert home.headers["cache-control"] == "no-store, max-age=0"
    assert api.status_code == 401
    assert posted.status_code in (401, 403, 405)


def test_every_response_is_non_cacheable_and_browser_hardened(monkeypatch):
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", PASSWORD)
    with TestClient(app) as client:
        unauthorized = client.get("/health")
        authorized = client.get("/health", headers={"Authorization": AUTH})

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    for response in (unauthorized, authorized):
        assert response.headers["cache-control"] == "no-store, max-age=0"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["cross-origin-resource-policy"] == "same-origin"


def test_operator_navigation_gets_clear_login_page_and_cookie_session(monkeypatch):
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", PASSWORD)
    with TestClient(app) as client:
        redirect = client.get(
            "/docs", headers={"Accept": "text/html"}, follow_redirects=False,
        )
        login = client.get("/login")
        wrong = client.post(
            "/login", data={"password": "wrong"}, follow_redirects=False,
        )
        accepted = client.post(
            "/login", data={"password": PASSWORD}, follow_redirects=False,
        )
        dashboard = client.get("/", headers={"Accept": "text/html"})

    assert redirect.status_code == 303 and redirect.headers["location"] == "/login"
    assert login.status_code == 200
    assert "The BrandOS service is running" in login.text
    assert wrong.status_code == 401 and "not accepted" in wrong.text
    assert PASSWORD not in wrong.text
    assert accepted.status_code == 303 and accepted.headers["location"] == "/"
    assert PASSWORD not in accepted.headers["location"]
    cookie = accepted.headers["set-cookie"]
    assert PREVIEW_SESSION_COOKIE in cookie
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert dashboard.status_code == 200


def test_top_level_login_metadata_is_allowed_without_weakening_api_mutations(monkeypatch):
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", PASSWORD)
    with TestClient(app) as client:
        login = client.post(
            "/login", data={"password": PASSWORD}, follow_redirects=False,
            headers={"Sec-Fetch-Site": "cross-site", "Origin": "null"},
        )
        mutation = client.post(
            "/api/brands", json=_brand_payload("top-level-rejected"),
            headers={"Authorization": AUTH, "Sec-Fetch-Site": "none"},
        )

    assert login.status_code == 303
    assert PREVIEW_SESSION_COOKIE in login.headers["set-cookie"]
    assert mutation.status_code == 403
    assert mutation.json() == {"detail": "Cross-site browser mutations are not allowed."}


def test_preview_session_is_expiring_signed_and_password_bound():
    token = create_preview_session(PASSWORD, now=100, nonce="fixed-nonce")
    assert validate_preview_session(token, PASSWORD, now=101) is True
    assert validate_preview_session(token, "different-password", now=101) is False
    assert validate_preview_session(token + "tampered", PASSWORD, now=101) is False
    assert validate_preview_session(token, PASSWORD, now=100 + 8 * 60 * 60 + 1) is False


def test_cross_site_browser_mutation_is_rejected_before_state_change(monkeypatch):
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", PASSWORD)
    with TestClient(app) as client:
        before = len(store.rows("SELECT * FROM brands"))
        response = client.post(
            "/api/brands", json=_brand_payload("cross-site-rejected"),
            headers={"Authorization": AUTH, "Origin": "https://attacker.example",
                     "Sec-Fetch-Site": "cross-site"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "Cross-site browser mutations are not allowed."}
    assert len(store.rows("SELECT * FROM brands")) == before


def test_same_origin_browser_and_explicit_non_browser_mutations_remain_supported(monkeypatch):
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", PASSWORD)
    with TestClient(app) as client:
        browser = client.post(
            "/api/brands", json=_brand_payload("same-origin-allowed"),
            headers={"Authorization": AUTH, "Origin": "http://testserver",
                     "Sec-Fetch-Site": "same-origin"},
        )
        explicit_client = client.post(
            "/api/brands", json=_brand_payload("explicit-client-allowed"),
            headers={"Authorization": AUTH},
        )

    assert browser.status_code == 201
    assert explicit_client.status_code == 201


def test_host_allowlist_and_remote_https_fail_closed(monkeypatch):
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "a-strong-remote-preview-password")
    with TestClient(app) as client:
        rejected_host = client.get(
            "/health", headers={"Authorization": AUTH, "Host": "evil.example"},
        )
    assert rejected_host.status_code == 421

    monkeypatch.setenv("BRAND_OS_ALLOWED_HOSTS", "brand.example")
    remote_auth = "Basic " + base64.b64encode(
        b"operator:a-strong-remote-preview-password"
    ).decode()
    with TestClient(app, base_url="http://brand.example") as client:
        cleartext = client.get("/health", headers={"Authorization": remote_auth})
    with TestClient(app, base_url="https://brand.example") as client:
        tls = client.get("/health", headers={"Authorization": remote_auth})

    assert cleartext.status_code == 400
    assert tls.status_code == 200
    assert tls.headers["strict-transport-security"].startswith("max-age=31536000")


def test_remote_client_cannot_spoof_loopback_host_to_bypass_transport_policy(monkeypatch):
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", PASSWORD)
    with TestClient(
        app, base_url="http://localhost", client=("198.51.100.42", 50000),
    ) as client:
        response = client.get("/health", headers={"Authorization": AUTH})

    assert response.status_code == 421
    assert "Loopback" in response.json()["detail"]


def test_remote_or_malformed_security_configuration_is_inert(monkeypatch):
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "short")
    monkeypatch.setenv("BRAND_OS_ALLOWED_HOSTS", "brand.example")
    short_auth = "Basic " + base64.b64encode(b"operator:short").decode()
    with TestClient(app, base_url="https://brand.example") as client:
        weak = client.get("/health", headers={"Authorization": short_auth})
    assert weak.status_code == 503
    assert "short" not in weak.text

    monkeypatch.setenv("BRAND_OS_ALLOWED_HOSTS", "*")
    with TestClient(app) as client:
        wildcard = client.get("/health", headers={"Authorization": short_auth})
    assert wildcard.status_code == 503
    assert "wildcards" in wildcard.json()["detail"]


def test_secret_free_deployment_readiness_distinguishes_local_and_remote_boundaries():
    local = deployment_boundary_status({"BRAND_OS_PREVIEW_PASSWORD": PASSWORD})
    unsafe_remote = deployment_boundary_status({
        "BRAND_OS_PREVIEW_PASSWORD": "configured",
        "BRAND_OS_ALLOWED_HOSTS": "brand.example",
    })
    safe_remote = deployment_boundary_status({
        "BRAND_OS_PREVIEW_PASSWORD": "a-strong-remote-preview-password",
        "BRAND_OS_ALLOWED_HOSTS": "brand.example",
        "BRAND_OS_REQUIRE_HTTPS": "true",
    })

    assert local["healthy"] is True and local["local_only"] is True
    assert unsafe_remote["healthy"] is False
    assert unsafe_remote["remote_password_ready"] is False
    assert any("HTTPS" in action for action in unsafe_remote["actions"])
    assert safe_remote["healthy"] is True and safe_remote["local_only"] is False
    assert "a-strong-remote-preview-password" not in repr(safe_remote)
