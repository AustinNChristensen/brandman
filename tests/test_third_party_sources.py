from __future__ import annotations

from cryptography.fernet import Fernet
import pytest

from app import store
from app.connectors import ConnectorError, HttpResponse, UrllibTransport
from app.service_runtime import build_service_runtime
from app.third_party_sources import (
    CONTENT_POLICY, ThirdPartySourceError, ThirdPartySourceService,
    validate_public_feed_url,
)


def setup_source_service(tmp_path):
    database = tmp_path / "sources.db"
    store.DATA_PATH = database
    store.init_db()
    brand = store.get_brand("demo-brand")
    service = ThirdPartySourceService(
        database, clock=lambda: "2026-09-02T12:00:00+00:00",
    )
    return database, brand, service


def test_onboarding_validates_public_feed_and_enrolls_default_schedule(tmp_path):
    _, brand, service = setup_source_service(tmp_path)
    source = service.onboard(
        brand["id"], publisher_name="Independent points feed",
        feed_url="https://news.example.test/feed/",
        homepage_url="https://news.example.test/",
        actor="Chris", reason="Monitor relevant public deal reporting",
    )
    assert source["connector_status"] == "connected"
    assert source["content_policy"] == CONTENT_POLICY
    assert source["polling_interval_seconds"] == 1800
    assert source["schedule"]["enabled"] is True
    assert source["schedule"]["action_type"] == "connector_sync"
    assert source["audit"][0]["actor"] == "Chris"
    account = store.row("SELECT * FROM connector_accounts WHERE id=?", (source["connector_account_id"],))
    assert account["connector_type"] == "rss"
    assert account["account_key"] == "https://news.example.test/feed/"

    # Repeated onboarding is idempotent and does not create duplicate accounts
    # or mutate the original governed audit decision.
    repeated = service.onboard(
        brand["id"], publisher_name="Independent points feed",
        feed_url="https://news.example.test/feed/", actor="Chris", reason="Repeat",
    )
    assert repeated["connector_account_id"] == source["connector_account_id"]
    assert len(repeated["audit"]) == 1


def test_onboarding_adopts_legacy_rss_account_without_losing_identity(tmp_path):
    _, brand, service = setup_source_service(tmp_path)
    legacy = store.upsert_connector_account(
        brand["id"], "rss", "https://legacy.example.test/feed/", "Legacy feed",
        status="disconnected", capabilities=["content.read"],
    )
    governed = service.onboard(
        brand["id"], publisher_name="Governed legacy feed",
        feed_url="https://legacy.example.test/feed/",
        actor="Chris", reason="Bring an existing feed under source governance",
    )
    assert governed["connector_account_id"] == legacy["id"]
    assert governed["connector_status"] == "connected"
    assert governed["schedule"]["enabled"] is True


@pytest.mark.parametrize("url", [
    "http://example.test/feed", "https://localhost/feed", "https://127.0.0.1/feed",
    "https://user:secret@example.test/feed", "https://example.test/feed?token=secret",
])
def test_feed_validation_rejects_nonpublic_or_credential_bearing_urls(url):
    with pytest.raises(ValueError):
        validate_public_feed_url(url)


def test_disable_is_audited_stops_schedule_and_cancels_waiting_sync(tmp_path):
    _, brand, service = setup_source_service(tmp_path)
    source = service.onboard(
        brand["id"], publisher_name="Deal news",
        feed_url="https://deals.example.test/rss.xml",
        actor="Chris", reason="Add a relevant source",
    )
    account_id = source["connector_account_id"]
    job = store.enqueue_job(
        "connector.sync", "source-cycle-1",
        {"brand_id": brand["id"], "connector_account_id": account_id, "stream": "content"},
        brand_id=brand["id"], connector_account_id=account_id,
    )
    disabled = service.set_enabled(
        account_id, False, actor="Chris", reason="Editorial quality review",
    )
    assert disabled["enabled"] is False
    assert disabled["connector_status"] == "disconnected"
    assert disabled["schedule"]["enabled"] is False
    assert store.row("SELECT status FROM durable_jobs WHERE id=?", (job["id"],))["status"] == "cancelled"
    assert disabled["audit"][-1]["details"]["queued_jobs_cancelled"] == 1
    assert service.list(brand["id"], include_disabled=False) == []
    with pytest.raises(ThirdPartySourceError, match="already disabled"):
        service.set_enabled(account_id, False, actor="Chris", reason="Repeat")

    enabled = service.set_enabled(
        account_id, True, actor="Chris", reason="Review passed",
    )
    assert enabled["enabled"] is True
    assert [event["action"] for event in enabled["audit"]] == [
        "onboarded", "disabled", "enabled",
    ]


def test_onboarded_feed_runs_through_rss_projection_without_real_network(tmp_path):
    database, brand, service = setup_source_service(tmp_path)
    source = service.onboard(
        brand["id"], publisher_name="Public points reporting",
        feed_url="https://publisher.example.test/feed/",
        actor="Chris", reason="Evaluate stories for original coverage",
        polling_interval_seconds=900,
    )
    calls = []

    class LocalTransport:
        def request(self, method, url, **kwargs):
            calls.append((method, url))
            return HttpResponse(200, b"""<rss><channel><item>
              <guid>story-1</guid><title>Example transfer update</title>
              <link>https://publisher.example.test/story-1</link>
              <description>Summary supplied by the syndicated feed.</description>
            </item></channel></rss>""")

    runtime = build_service_runtime(
        "third-party-source-test", database, Fernet.generate_key().decode(),
        lambda _account: LocalTransport(),
    )
    assert source["connector_account_id"] in runtime.runtime.connectors
    runtime.tick(max_decisions=10)
    result = runtime.run_until_idle(max_jobs=10)
    assert calls == [
        ("GET", "https://publisher.example.test/feed/"),
        ("GET", "https://publisher.example.test/story-1"),
    ]
    ingested = store.row(
        "SELECT id FROM sources WHERE brand_id=? AND url=?",
        (brand["id"], "https://publisher.example.test/story-1"),
    )
    snapshot = store.row(
        "SELECT status FROM source_canonical_revalidations WHERE brand_id=? AND source_id=?",
        (brand["id"], ingested["id"]),
    )
    assert snapshot["status"] == "verified"
    assert any(job["job_type"] == "connector.sync" and job["status"] == "completed"
               for job in result.jobs)
    assert store.rows("SELECT id FROM editorial_candidates WHERE brand_id=?", (brand["id"],))
    # The generic item remains candidate/intelligence backlog until it clears
    # the mission-fit threshold or is selected explicitly.
    assert not store.rows("SELECT id FROM campaigns WHERE brand_id=?", (brand["id"],))
    assert not store.rows("SELECT id FROM posts WHERE status='draft'")
    assert store.row("SELECT promotion_state FROM source_intelligence_records")["promotion_state"] == "backlog"


def test_worker_rechecks_disabled_account_before_any_network_call(tmp_path):
    database, brand, service = setup_source_service(tmp_path)
    source = service.onboard(
        brand["id"], publisher_name="Paused source",
        feed_url="https://paused.example.test/feed/",
        actor="Chris", reason="Initial source review",
    )
    calls = []

    class MustNotRunTransport:
        def request(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("disabled source attempted a network request")

    runtime = build_service_runtime(
        "disabled-source-test", database, Fernet.generate_key().decode(),
        lambda _account: MustNotRunTransport(),
    )
    service.set_enabled(
        source["connector_account_id"], False,
        actor="Chris", reason="Pause before the next polling cycle",
    )
    store.enqueue_job(
        "connector.sync", "manually-enqueued-after-disable",
        {"brand_id": brand["id"], "connector_account_id": source["connector_account_id"],
         "stream": "content"},
        brand_id=brand["id"], connector_account_id=source["connector_account_id"],
    )
    result = runtime.run_once(as_of="9999-12-31T00:00:00+00:00")
    assert result["status"] == "completed"
    assert result["result"]["reason"] == "connector_account_disabled"
    assert calls == []


class _TransportResponse:
    def __init__(self, status=200, body=b"ok", headers=()):
        self.status = status
        self._body = body
        self._headers = list(headers)

    def read(self, _limit):
        return self._body

    def getheaders(self):
        return self._headers


class _Connection:
    def __init__(self, response, calls, target):
        self.response = response
        self.calls = calls
        self.target = target

    def request(self, method, path, body=None, headers=None):
        self.calls.append((self.target, method, path, body, headers))

    def getresponse(self):
        return self.response

    def close(self):
        pass


def test_rss_transport_pins_the_validated_public_address_against_dns_rebinding():
    calls = []
    resolved = []

    def resolver(host, port):
        resolved.append((host, port))
        return ["93.184.216.34"]

    def factory(host, address, port, timeout):
        assert (host, address, port, timeout) == (
            "feed.example.test", "93.184.216.34", 443, 2,
        )
        return _Connection(_TransportResponse(), calls, address)

    response = UrllibTransport(
        timeout_seconds=2, resolver=resolver, connection_factory=factory,
    ).request("GET", "https://feed.example.test/rss")

    assert response.status_code == 200
    assert resolved == [("feed.example.test", 443)]
    assert calls[0][0] == "93.184.216.34"


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.8", "169.254.169.254", "224.0.0.1",
    "0.0.0.0", "192.0.2.1", "::1", "fc00::1",
])
def test_rss_transport_rejects_every_non_global_resolved_address(address):
    connected = []
    transport = UrllibTransport(
        resolver=lambda _host, _port: [address],
        connection_factory=lambda *args: connected.append(args),
    )
    with pytest.raises(ConnectorError, match="non-public target blocked"):
        transport.request("GET", "https://private.nip.io/feed")
    assert connected == []


def test_rss_transport_revalidates_redirect_and_blocks_private_destination():
    connections = []
    responses = [_TransportResponse(
        302, headers=[("Location", "https://internal.example.test/feed")],
    )]

    def resolver(host, _port):
        return {
            "public.example.test": ["93.184.216.34"],
            "internal.example.test": ["10.0.0.9"],
        }[host]

    def factory(host, address, port, timeout):
        connections.append((host, address))
        return _Connection(responses.pop(0), [], address)

    with pytest.raises(ConnectorError, match="non-public target blocked"):
        UrllibTransport(
            resolver=resolver, connection_factory=factory,
        ).request("GET", "https://public.example.test/feed")
    assert connections == [("public.example.test", "93.184.216.34")]


def test_rss_transport_bounds_redirects_and_drops_auth_across_origins():
    calls = []
    responses = [
        _TransportResponse(302, headers=[("Location", "https://two.example.test/feed")]),
        _TransportResponse(302, headers=[("Location", "https://three.example.test/feed")]),
    ]

    def factory(host, address, port, timeout):
        return _Connection(responses.pop(0), calls, address)

    transport = UrllibTransport(
        max_redirects=1,
        resolver=lambda _host, _port: ["93.184.216.34"],
        connection_factory=factory,
    )
    with pytest.raises(ConnectorError, match="redirect policy"):
        transport.request(
            "GET", "https://one.example.test/feed",
            headers={"Authorization": "Bearer should-not-cross-origin"},
        )
    assert "Authorization" in calls[0][4]
    assert "Authorization" not in calls[1][4]
