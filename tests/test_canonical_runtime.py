from __future__ import annotations

from cryptography.fernet import Fernet
import base64
from fastapi.testclient import TestClient

from brandman import store
from brandman.canonical_revalidation import CanonicalSourceRevalidationStore
from brandman.canonical_runtime import (
    CANONICAL_REVALIDATE_JOB_TYPE, enqueue_canonical_revalidation,
)
from brandman.connectors import ConnectorError, ConnectorKind, HttpResponse
from brandman.service_runtime import build_service_runtime
from brandman.sync import enqueue_sync_job
from brandman.worker_cli import build_secretless_assisted_runtime
from brandman.main import app


class RouteTransport:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.routes[url]
        if isinstance(result, BaseException):
            raise result
        return result


def _database(tmp_path):
    database = tmp_path / "canonical-runtime.db"
    store.DATA_PATH = database
    store.init_db()
    brand = store.get_brand("demo-brand")
    return database, brand


def test_secretless_rss_runtime_fetches_each_canonical_page_and_persists_after_source(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "now", lambda: "2026-09-02T12:30:00+00:00")
    database, brand = _database(tmp_path)
    feed_url = "https://feed.test/rss"
    account = store.upsert_connector_account(
        brand["id"], "rss", feed_url, "Public feed", status="healthy",
        scopes=[], capabilities=["content.read"],
    )
    feed = b"""<rss><channel>
      <item><guid>one</guid><title>Old 40% offer</title><link>https://one.test/offer</link>
        <description>Ends Sep 30</description><pubDate>Tue, 02 Sep 2026 12:00:00 GMT</pubDate></item>
      <item><guid>two</guid><title>Second offer</title><link>https://two.test/offer</link>
        <description>20% through Sep 27</description><pubDate>Tue, 02 Sep 2026 11:00:00 GMT</pubDate></item>
    </channel></rss>"""
    transport = RouteTransport({
        feed_url: HttpResponse(200, feed),
        "https://one.test/offer": ConnectorError(ConnectorKind.RSS, "DNS resolution"),
        "https://two.test/offer": HttpResponse(
            200, b"<html><title>Second offer</title><meta name='description' content='20% through Sep 27'></html>",
        ),
    })
    runtime, _, _ = build_secretless_assisted_runtime(
        "canonical-worker", database, transport_factory=lambda _account: transport,
    )
    enqueue_sync_job(
        brand_id=brand["id"], connector_account_id=account["id"], stream="content",
        idempotency_key="rss-cycle-1",
    )

    completed = runtime.run_once(as_of="2026-09-02T13:00:00+00:00")

    assert completed["status"] == "completed"
    sources = store.rows("SELECT * FROM sources WHERE brand_id=? ORDER BY title", (brand["id"],))
    assert [source["title"] for source in sources] == ["Old 40% offer", "Second offer"]
    evidence = CanonicalSourceRevalidationStore(database).list(brand["id"])
    assert len(evidence) == 2
    assert {item["status"] for item in evidence} == {"unavailable", "verified"}
    assert all(item["source_id"] in {source["id"] for source in sources} for item in evidence)
    unavailable = next(item for item in evidence if item["status"] == "unavailable")
    assert unavailable["rationale"] == [
        "canonical page was unavailable during bounded HTTPS retrieval"
    ]
    assert "DNS" not in str(unavailable)
    assert all(item["semantic_fact_check"] is False for item in evidence)


def test_unchanged_rss_replay_does_not_duplicate_canonical_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "now", lambda: "2026-09-02T12:30:00+00:00")
    database, brand = _database(tmp_path)
    feed_url = "https://feed.test/rss"
    account = store.upsert_connector_account(
        brand["id"], "rss", feed_url, "Public feed", status="healthy",
        scopes=[], capabilities=["content.read"],
    )
    feed = b"""<rss><channel><item><guid>one</guid><title>Offer</title>
      <link>https://one.test/offer</link><description>30%</description>
      <pubDate>Tue, 02 Sep 2026 12:00:00 GMT</pubDate></item></channel></rss>"""
    transport = RouteTransport({
        feed_url: HttpResponse(200, feed),
        "https://one.test/offer": HttpResponse(200, b"<html><title>Offer 30%</title></html>"),
    })
    runtime, _, _ = build_secretless_assisted_runtime(
        "canonical-worker", database, transport_factory=lambda _account: transport,
    )
    for cycle in ("one", "two"):
        enqueue_sync_job(
            brand_id=brand["id"], connector_account_id=account["id"], stream="content",
            idempotency_key=f"rss-cycle-{cycle}",
        )
        assert runtime.run_once(as_of="2026-09-02T13:00:00+00:00")["status"] == "completed"

    assert len(CanonicalSourceRevalidationStore(database).list(brand["id"])) == 1


def test_manual_source_revalidation_is_durable_idempotent_and_metadata_only(tmp_path):
    database, brand = _database(tmp_path)
    source = store.insert("sources", {
        "brand_id": brand["id"], "title": "Official offer",
        "url": "https://issuer.test/offer", "source_type": "manual",
        "body_summary": "30% through Sep 27", "lifecycle_state": "published",
        "scheduled_for": None, "external_source_id": "official-offer",
    })
    transport = RouteTransport({
        source["url"]: HttpResponse(
            200, b"<html><title>Official 30% offer</title><meta name='description' content='Ends Sep 27'></html>",
        ),
    })
    runtime, _, _ = build_secretless_assisted_runtime(
        "canonical-worker", database, transport_factory=lambda _account: transport,
    )
    first = enqueue_canonical_revalidation(
        brand_id=brand["id"], source_id=source["id"],
        idempotency_key="official-offer-r1", actor="Chris",
    )
    replay = enqueue_canonical_revalidation(
        brand_id=brand["id"], source_id=source["id"],
        idempotency_key="official-offer-r1", actor="Chris",
    )
    assert replay["id"] == first["id"]

    completed = runtime.run_once(as_of="2099-09-02T13:00:00+00:00")
    assert completed["status"] == "completed"
    assert completed["result"]["semantic_fact_check"] is False
    assert completed["result"]["evidence_scope"] == "canonical_metadata_only"
    assert runtime.run_once(as_of="2099-09-02T13:00:00+00:00") is None
    assert len(CanonicalSourceRevalidationStore(database).list(brand["id"])) == 1


def test_native_runtime_also_registers_canonical_fetching_and_durable_handler(tmp_path):
    database, brand = _database(tmp_path)
    feed_url = "https://feed.test/rss"
    rss = store.upsert_connector_account(
        brand["id"], "rss", feed_url, "Public feed", status="healthy",
        scopes=[], capabilities=["content.read"],
    )
    transport = RouteTransport({})
    service = build_service_runtime(
        "native-worker", database, Fernet.generate_key().decode(),
        lambda _account: transport,
    )

    assert service.runtime.connectors[rss["id"]].canonical_revalidator is not None
    assert CANONICAL_REVALIDATE_JOB_TYPE in service.runtime.worker.handlers


def test_rest_enqueues_manual_revalidation_and_read_surface_labels_metadata_only(
    tmp_path, monkeypatch,
):
    _, brand = _database(tmp_path)
    source = store.insert("sources", {
        "brand_id": brand["id"], "title": "Issuer terms",
        "url": "https://issuer.test/terms", "source_type": "manual",
        "body_summary": "Terms", "lifecycle_state": "published",
        "scheduled_for": None, "external_source_id": "issuer-terms",
    })
    monkeypatch.setenv("BRANDMAN_PREVIEW_PASSWORD", "canonical-test")
    token = base64.b64encode(b"operator:canonical-test").decode()
    with TestClient(app, headers={"Authorization": f"Basic {token}"}) as client:
        queued = client.post(
            f"/api/brands/demo-brand/sources/{source['id']}/canonical-revalidations",
            json={"idempotency_key": "issuer-terms-r1"},
        )
        assert queued.status_code == 202
        assert queued.json()["job_type"] == CANONICAL_REVALIDATE_JOB_TYPE
        response = client.get(
            "/api/brands/demo-brand/canonical-revalidations",
            params={"source_id": source["id"]},
        )
        assert response.status_code == 200
        assert response.json() == []
