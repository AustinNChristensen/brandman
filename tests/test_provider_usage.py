from datetime import UTC, datetime
import sqlite3

import pytest

from app.connectors import HttpResponse
from app.provider_usage import (
    MeteredTransport, ProviderUsageLedger, classify_request, safe_endpoint,
)
from app import store


NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)


class FakeTransport:
    def __init__(self, *, fail=False): self.fail = fail
    def request(self, method, url, **kwargs):
        if self.fail: raise RuntimeError("token=must-not-be-persisted")
        return HttpResponse(201, b'{"data":{"id":"42"}}', {"x-request-id": "request-42"})


def test_metered_transport_records_payload_free_request_and_operator_price(tmp_path):
    ledger = ProviderUsageLedger(tmp_path / "usage.db", clock=lambda: NOW)
    price = ledger.configure_price(
        version="operator-2026-09", provider="x", method="POST",
        endpoint_pattern="https://api.x.com/2/tweets", unit_name="request",
        unit_price="0.0125", currency="USD",
        effective_at="2026-09-01T00:00:00Z", actor="preview-operator",
        billable_category="post.create_with_url",
    )
    transport = MeteredTransport(
        FakeTransport(), ledger, brand_id="brand-1",
        connector_account_id="x-writer", provider="x",
    )
    response = transport.request(
        "POST", "https://api.x.com/2/tweets?access_token=never-store",
        headers={"Authorization": "Bearer never-store"},
        json_body={"text": "also never store https://example.test"},
    )
    assert response.status_code == 201
    report = ledger.report("brand-1")
    assert report["request_count"] == 1
    assert report["totals"][0]["estimated_cost"] == "0.0125"
    assert report["events"][0]["endpoint"] == "https://api.x.com/2/tweets"
    assert report["events"][0]["pricing_version_id"] == price["id"]
    assert report["events"][0]["billable_category"] == "post.create_with_url"
    assert report["events"][0]["provider_request_id"].startswith("sha256:")
    assert "request-42" not in str(report)
    assert report["events"][0]["response_resource_count"] == 1
    assert report["breakdowns"] == [{
        "provider": "x", "billable_category": "post.create_with_url",
        "currency": "USD", "unit_name": "request", "requests": 1,
        "units": "1", "estimated_cost": "0.0125",
    }]
    assert report["pricing_versions"][0]["id"] == price["id"]
    assert "never-store" not in str(report)


def test_usage_business_breakdown_never_blends_providers_or_categories(tmp_path):
    ledger = ProviderUsageLedger(tmp_path / "usage.db", clock=lambda: NOW)
    for provider, category, price in (
        ("x", "post.create_plain", "0.02"),
        ("beehiiv", "post.create", "0.03"),
    ):
        ledger.configure_price(
            version="customer-plan-v1", provider=provider, method="POST",
            endpoint_pattern=(
                "https://api.x.com/2/tweets" if provider == "x"
                else "https://api.beehiiv.com/v2/publications/*/posts"
            ),
            unit_name="request", unit_price=price, currency="USD",
            effective_at="2026-09-01T00:00:00Z", actor="pricing-operator",
            billable_category=category,
        )
    ledger.record(
        brand_id="brand-1", connector_account_id="x-write", provider="x",
        method="POST", url="https://api.x.com/2/tweets", status_code=201,
        billable_category="post.create_plain",
    )
    ledger.record(
        brand_id="brand-1", connector_account_id="bee-write", provider="beehiiv",
        method="POST", url="https://api.beehiiv.com/v2/publications/pub/posts",
        status_code=201, billable_category="post.create",
    )
    report = ledger.report("brand-1")
    assert {(row["provider"], row["billable_category"], row["estimated_cost"])
            for row in report["breakdowns"]} == {
        ("x", "post.create_plain", "0.02"),
        ("beehiiv", "post.create", "0.03"),
    }
    assert {(row["provider"], row["billable_category"], row["estimated_cost"])
            for row in report["totals"]} == {
        ("x", "post.create_plain", "0.02"),
        ("beehiiv", "post.create", "0.03"),
    }


def test_unpriced_and_failed_requests_are_truthfully_metered(tmp_path):
    ledger = ProviderUsageLedger(tmp_path / "usage.db", clock=lambda: NOW)
    transport = MeteredTransport(
        FakeTransport(fail=True), ledger, brand_id="brand-1",
        connector_account_id="beehiiv-read", provider="beehiiv",
    )
    with pytest.raises(RuntimeError):
        transport.request("GET", "https://api.beehiiv.com/v2/posts?api_key=hidden")
    report = ledger.report("brand-1")
    assert report["unpriced_request_count"] == 1
    assert report["events"][0]["status_code"] is None
    assert report["events"][0]["outcome"] == "transport_error"
    assert "hidden" not in str(report)


def test_untrusted_provider_request_id_is_hashed_not_persisted(tmp_path):
    class CanaryTransport:
        def request(self, method, url, **kwargs):
            return HttpResponse(
                200, b'{"data":[]}',
                {"x-request-id": "request token=provider-secret-canary"},
            )
    ledger = ProviderUsageLedger(tmp_path / "usage.db", clock=lambda: NOW)
    MeteredTransport(
        CanaryTransport(), ledger, brand_id="brand-1",
        connector_account_id="x-read", provider="x",
    ).request("GET", "https://api.x.com/2/tweets")
    report = ledger.report("brand-1")
    request_id = report["events"][0]["provider_request_id"]
    assert request_id.startswith("sha256:")
    assert "provider-secret-canary" not in repr(report)


def test_safe_endpoint_removes_userinfo_query_and_fragment():
    assert safe_endpoint("https://user:pass@example.com/path?q=secret#fragment") == "https://example.com/path"


def test_x_billable_classification_distinguishes_owned_general_user_and_create_shape():
    assert classify_request("x", "GET", "https://api.x.com/2/users/42/tweets", owned_user_id="42") == "post.read_owned"
    assert classify_request("x", "GET", "https://api.x.com/2/users/77/tweets", owned_user_id="42") == "post.read_general"
    assert classify_request("x", "GET", "https://api.x.com/2/users/42", owned_user_id="42") == "user.read"
    assert classify_request("x", "GET", "https://api.x.com/2/tweets/search/recent") == "post.read_general"
    assert classify_request("x", "POST", "https://api.x.com/2/tweets", {"text": "plain"}) == "post.create_plain"
    assert classify_request("x", "POST", "https://api.x.com/2/tweets", {"text": "see https://example.test"}) == "post.create_with_url"


def test_legacy_rate_card_unique_constraint_is_safely_upgraded(tmp_path):
    database = tmp_path / "legacy-usage.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE provider_pricing_versions (
              id TEXT PRIMARY KEY, version TEXT NOT NULL, provider TEXT NOT NULL,
              method TEXT NOT NULL, endpoint_pattern TEXT NOT NULL,
              unit_name TEXT NOT NULL, unit_price TEXT NOT NULL, currency TEXT NOT NULL,
              effective_at TEXT NOT NULL, configured_by TEXT NOT NULL, created_at TEXT NOT NULL,
              UNIQUE(version,provider,method,endpoint_pattern)
            )"""
        )
        connection.execute(
            """INSERT INTO provider_pricing_versions VALUES
               ('old','v1','x','POST','https://api.x.com/2/tweets','request',
                '0.01','USD','2026-01-01T00:00:00+00:00','operator',
                '2026-01-01T00:00:00+00:00')"""
        )

    ledger = ProviderUsageLedger(database, clock=lambda: NOW)
    ledger.configure_price(
        version="v2", provider="x", method="POST",
        endpoint_pattern="https://api.x.com/2/tweets",
        billable_category="post.create_plain", unit_name="request",
        unit_price="0.01", currency="USD",
        effective_at="2026-09-01T00:00:00Z", actor="operator",
    )
    ledger.configure_price(
        version="v2", provider="x", method="POST",
        endpoint_pattern="https://api.x.com/2/tweets",
        billable_category="post.create_with_url", unit_name="request",
        unit_price="0.02", currency="USD",
        effective_at="2026-09-01T00:00:00Z", actor="operator",
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_pricing_versions"
        ).fetchone()[0] == 3
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='provider_pricing_versions'"
        ).fetchone()[0]
    assert "billable_category" in sql


def test_rest_and_mcp_usage_reports_are_read_only(tmp_path, monkeypatch):
    import base64
    from fastapi.testclient import TestClient
    from app.main import app
    from app.mcp_server import get_provider_api_usage

    database = tmp_path / "api-usage.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "test-only-password")
    store.init_db()
    brand = store.get_brand("demo-brand")
    ledger = ProviderUsageLedger(database, clock=lambda: NOW)
    ledger.record(
        brand_id=brand["id"], connector_account_id="x-read", provider="x",
        method="GET", url="https://api.x.com/2/users/1", status_code=200,
    )
    headers = {"Authorization": "Basic " + base64.b64encode(b"operator:test-only-password").decode()}
    with TestClient(app, headers=headers) as client:
        response = client.get("/api/brands/demo-brand/provider-usage")
        assert response.status_code == 200
        assert response.json()["request_count"] == 1
    assert get_provider_api_usage("demo-brand")["request_count"] == 1
