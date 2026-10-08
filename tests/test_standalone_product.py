import base64
from datetime import UTC, datetime

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from brandman import store
from brandman.connection_product import CONNECTION_LANES, onboarding_manifest
from brandman.main import app
from brandman.provider_usage import ProviderUsageLedger


HEADERS = {
    "Authorization": "Basic "
    + base64.b64encode(b"operator:test-only-password").decode()
}


def test_onboarding_contract_is_plain_language_split_lane_and_price_neutral():
    document = onboarding_manifest(encryption_configured=False)
    assert document["encryption"]["ready"] is False
    assert document["modes"][0]["mode"] == "assisted"
    assert document["modes"][0]["credentials_stored"] is False
    assert document["modes"][1]["mode"] == "standalone"
    assert document["modes"][1]["available_now"] is False
    assert document["pricing"]["vendor_prices_bundled"] is False
    assert "unit_price" not in str(document)

    assert CONNECTION_LANES["beehiiv_read"]["scopes"] == ["posts.read"]
    assert CONNECTION_LANES["beehiiv_read"]["can_write"] is False
    assert CONNECTION_LANES["beehiiv_write"]["scopes"] == ["posts.write"]
    assert CONNECTION_LANES["beehiiv_write"]["can_read"] is False
    assert "tweet.write" not in CONNECTION_LANES["x_read"]["scopes"]
    assert "tweet.write" in CONNECTION_LANES["x_write"]["scopes"]
    assert "developer project" in document["providers"]["x"]["availability"]


def test_connection_onboarding_preflights_actual_encryption_configuration(
    tmp_path, monkeypatch,
):
    database = tmp_path / "onboarding.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    monkeypatch.setenv("BRANDMAN_DB", str(database))
    monkeypatch.setenv("BRANDMAN_DATABASE_PROFILE", "test")
    monkeypatch.setenv("BRANDMAN_PREVIEW_PASSWORD", "test-only-password")
    monkeypatch.setenv("BRANDMAN_CREDENTIAL_MASTER_KEY", "not-a-fernet-key")
    store.init_db(profile="test")

    with TestClient(app, headers=HEADERS) as client:
        malformed = client.get("/api/brands/demo-brand/connection-onboarding")
        assert malformed.status_code == 200
        assert malformed.json()["encryption"]["ready"] is False
        monkeypatch.setenv("BRANDMAN_CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())
        ready = client.get("/api/brands/demo-brand/connection-onboarding")
        assert ready.json()["encryption"]["ready"] is True
        assert "credential" not in ready.text.lower() or "credentials_stored" in ready.text


def test_customer_rate_cards_are_tenant_scoped_and_never_guess_prices(tmp_path):
    ledger = ProviderUsageLedger(
        tmp_path / "pricing.db",
        clock=lambda: datetime(2026, 9, 2, 12, tzinfo=UTC),
    )
    ledger.configure_price(
        brand_id="brand-a", version="agreement-v1", provider="beehiiv",
        method="GET", endpoint_pattern="https://api.beehiiv.com/v2/*",
        billable_category="post.read", unit_name="request", unit_price="0.04",
        currency="USD", effective_at="2026-09-01T00:00:00Z", actor="operator-a",
    )
    for brand in ("brand-a", "brand-b"):
        ledger.record(
            brand_id=brand, connector_account_id=f"{brand}-read", provider="beehiiv",
            method="GET", url="https://api.beehiiv.com/v2/publications/p/posts",
            status_code=200, billable_category="post.read",
        )
    report_a = ledger.report("brand-a")
    report_b = ledger.report("brand-b")
    assert report_a["events"][0]["estimated_cost"] == "0.04"
    assert report_a["pricing_versions"][0]["brand_id"] == "brand-a"
    assert report_b["events"][0]["estimated_cost"] is None
    assert report_b["pricing_versions"] == []
    assert report_b["unpriced_request_count"] == 1


def test_rate_card_rejects_cross_provider_or_insecure_endpoint_patterns(tmp_path):
    ledger = ProviderUsageLedger(tmp_path / "pricing.db")
    common = {
        "brand_id": "brand-a", "version": "agreement-v1", "provider": "x",
        "method": "GET", "billable_category": "post.read_general",
        "unit_name": "resource", "unit_price": "0.01", "currency": "USD",
        "effective_at": "2026-09-01T00:00:00Z", "actor": "operator-a",
    }
    import pytest
    with pytest.raises(ValueError, match="host does not match"):
        ledger.configure_price(
            endpoint_pattern="https://api.beehiiv.com/v2/*", **common,
        )
    with pytest.raises(ValueError, match="absolute HTTPS"):
        ledger.configure_price(
            endpoint_pattern="http://api.x.com/2/*", **common,
        )


def test_rate_card_rest_uses_authenticated_brand_identity(tmp_path, monkeypatch):
    database = tmp_path / "rate-card-api.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    monkeypatch.setenv("BRANDMAN_DB", str(database))
    monkeypatch.setenv("BRANDMAN_DATABASE_PROFILE", "test")
    monkeypatch.setenv("BRANDMAN_PREVIEW_PASSWORD", "test-only-password")
    store.init_db(profile="test")
    brand = store.get_brand("demo-brand")
    with TestClient(app, headers=HEADERS) as client:
        response = client.post("/api/brands/demo-brand/provider-rate-cards", json={
            "version": "customer-contract-v1", "operation": "x_plain_post",
            "unit_price": "0.017", "currency": "usd",
            "effective_at": "2026-09-01T00:00:00Z",
        })
        assert response.status_code == 201
        body = response.json()
        assert body["brand_id"] == brand["id"]
        assert body["configured_by"] == "chris"
        assert body["currency"] == "USD"
        report = client.get("/api/brands/demo-brand/provider-usage").json()
        assert report["pricing_versions"][0]["version"] == "customer-contract-v1"


def test_brand_connection_surface_filters_metadata_and_rejects_shared_lane_ids(
    tmp_path, monkeypatch,
):
    database = tmp_path / "connection-tenants.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    monkeypatch.setenv("BRANDMAN_DB", str(database))
    monkeypatch.setenv("BRANDMAN_DATABASE_PROFILE", "test")
    monkeypatch.setenv("BRANDMAN_PREVIEW_PASSWORD", "test-only-password")
    monkeypatch.setenv("BRANDMAN_CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())
    store.init_db(profile="test")
    other = store.create_brand({
        "slug": "other-brand", "name": "Other", "mission": "test",
        "voice": "test", "compliance_rules": "test",
        "approval_policy": "human_approval_required",
    })
    points = store.get_brand("demo-brand")
    store.upsert_connector_account(
        points["id"], connector_type="beehiiv", account_key="demo-read",
        display_name="Demo read", status="needs_attention", scopes=["posts.read"],
        capabilities=["posts.read", "metrics.read"],
        configuration={"delivery_mode": "api", "connection_role": "beehiiv_read"},
    )
    store.upsert_connector_account(
        other["id"], connector_type="beehiiv", account_key="other-read",
        display_name="Other read", status="needs_attention", scopes=["posts.read"],
        capabilities=["posts.read"],
        configuration={"delivery_mode": "api", "connection_role": "beehiiv_read"},
    )
    store.upsert_connector_account(
        other["id"], connector_type="beehiiv", account_key="shared-id",
        display_name="Other shared", status="needs_attention", scopes=["posts.read"],
        capabilities=["posts.read"], configuration={"delivery_mode": "api"},
    )
    store.upsert_connector_account(
        points["id"], connector_type="beehiiv", account_key="shared-id",
        display_name="Demo shared", status="needs_attention", scopes=["posts.read"],
        capabilities=["posts.read"], configuration={"delivery_mode": "api"},
    )
    with TestClient(app, headers=HEADERS) as client:
        connected = client.put(
            "/api/brands/demo-brand/connections/beehiiv/demo-read",
            json={
                "display_name": "Demo read", "credentials": {"api_key": "secret-a"},
                "required_scopes": ["posts.read"], "granted_scopes": ["posts.read"],
            },
        )
        assert connected.status_code == 200
        assert [row["account_id"] for row in client.get(
            "/api/brands/demo-brand/connections"
        ).json()] == ["demo-read"]
        overbroad = client.put(
            "/api/brands/demo-brand/connections/beehiiv/demo-read",
            json={
                "display_name": "Demo read", "credentials": {"api_key": "secret-wide"},
                "required_scopes": ["posts.read", "posts.write"],
                "granted_scopes": ["posts.read", "posts.write"],
            },
        )
        assert overbroad.status_code == 422
        extra_field = client.put(
            "/api/brands/demo-brand/connections/beehiiv/demo-read",
            json={
                "display_name": "Demo read",
                "credentials": {"api_key": "secret-a", "refresh_token": "not-allowed"},
                "required_scopes": ["posts.read"], "granted_scopes": ["posts.read"],
            },
        )
        assert extra_field.status_code == 422
        # A caller cannot first register a broad lane and use the exact-scope
        # check to bless it: the lane itself is compared to the product contract.
        store.upsert_connector_account(
            points["id"], connector_type="beehiiv", account_key="tampered-read",
            display_name="Tampered", status="needs_attention",
            scopes=["posts.read", "posts.write"], capabilities=["posts.read"],
            configuration={"delivery_mode": "api", "connection_role": "beehiiv_read"},
        )
        tampered = client.put(
            "/api/brands/demo-brand/connections/beehiiv/tampered-read",
            json={
                "display_name": "Tampered", "credentials": {"api_key": "secret-d"},
                "required_scopes": ["posts.read", "posts.write"],
                "granted_scopes": ["posts.read", "posts.write"],
            },
        )
        assert tampered.status_code == 422
        missing_owner = client.put(
            "/api/brands/demo-brand/connections/beehiiv/other-read",
            json={"display_name": "wrong", "credentials": {"api_key": "secret-b"}},
        )
        assert missing_owner.status_code == 409
        collision = client.put(
            "/api/brands/demo-brand/connections/beehiiv/shared-id",
            json={"display_name": "shared", "credentials": {"api_key": "secret-c"}},
        )
        assert collision.status_code == 409
    assert "secret-a" not in connected.text
