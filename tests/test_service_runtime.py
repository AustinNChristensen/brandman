from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from brandman import store
from brandman.experiments import (
    EXPERIMENT_WINDOW_COLLECT_JOB_TYPE, EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE,
)
from brandman.connectors import HttpResponse
from brandman.campaign_graph import CampaignGraphStore
from brandman.credentials import CredentialConfigurationError, CredentialStore
from brandman.editorial import EditorialStore, IssueLifecycle
from brandman.beehiiv_runtime import (
    BEEHIIV_NEWSLETTER_EXPORT_JOB,
    enqueue_newsletter_export,
)
from brandman.service_runtime import (
    ServiceRuntimeConfigurationError,
    build_service_runtime,
)
from brandman.sync import enqueue_sync_job
from brandman.connector_health import HEALTH_CHECK_JOB_TYPE
from brandman.x_delivery import enqueue_x_delivery


pytestmark = pytest.mark.usefixtures("launch_window_clock")


class RecordingTransport:
    def __init__(self, connector_type: str, calls: list[dict]):
        self.connector_type = connector_type
        self.calls = calls

    def request(self, method, url, **kwargs):
        self.calls.append({"connector": self.connector_type, "method": method, "url": url, **kwargs})
        if self.connector_type == "beehiiv":
            if method == "POST":
                return HttpResponse(201, json.dumps({"data": {
                    "id": "beehiiv-draft-1", "status": "draft",
                    "preview_url": "https://beehiiv.test/drafts/1",
                }}).encode())
            return HttpResponse(200, json.dumps({"data": [], "pagination": {"page": 1, "total_pages": 1}}).encode())
        if self.connector_type == "x":
            return HttpResponse(201, json.dumps({"data": {"id": "123456789"}}).encode())
        return HttpResponse(
            200,
            b"<rss><channel><item><guid>story-1</guid><title>Story</title></item></channel></rss>",
        )


def configured_database(tmp_path):
    database = tmp_path / "service.db"
    store.DATA_PATH = database
    store.init_db()
    brand = store.get_brand("demo-brand")
    key = Fernet.generate_key().decode()
    credentials = CredentialStore(database, key)
    beehiiv = store.upsert_connector_account(
        brand["id"], "beehiiv", "pub_demo", "Demo Brand Beehiiv",
        status="healthy", scopes=["posts.read", "posts.write"],
        capabilities=["posts.read", "drafts.write"],
    )
    x_account = store.upsert_connector_account(
        brand["id"], "x", "demobrand", "Demo Brand X",
        status="healthy",
        scopes=["tweet.read", "users.read", "tweet.write", "offline.access"],
        capabilities=["posts.write"],
    )
    rss = store.upsert_connector_account(
        brand["id"], "rss", "https://example.test/feed.xml", "Industry feed",
        status="healthy", scopes=[], capabilities=["content.read"],
    )
    credentials.put(
        "beehiiv", "pub_demo", "Beehiiv", {"api_key": "beehiiv-secret"},
        required_scopes=["posts.read", "posts.write"],
        granted_scopes=["posts.read", "posts.write"],
    )
    credentials.put(
        "x", "demobrand", "X", {
            "access_token": "x-secret", "refresh_token": "x-refresh",
            "client_id": "x-client", "expires_at": "2027-09-02T00:00:00Z",
        },
        required_scopes=["tweet.read", "users.read", "tweet.write", "offline.access"],
        granted_scopes=["tweet.read", "users.read", "tweet.write", "offline.access"],
    )
    return database, key, brand, beehiiv, x_account, rss


def test_factory_requires_master_key_before_mapping_or_network(tmp_path):
    database = tmp_path / "service.db"
    calls = []
    with pytest.raises(CredentialConfigurationError, match="not configured"):
        build_service_runtime(
            "worker", database, None,
            lambda account: RecordingTransport(account["connector_type"], calls),
        )
    assert calls == []


def test_maps_reads_and_write_separately_without_eager_secret_or_network_access(tmp_path):
    database, key, _, beehiiv, x_account, rss = configured_database(tmp_path)
    calls = []
    service = build_service_runtime(
        "worker", database, key,
        lambda account: RecordingTransport(account["connector_type"], calls),
    )
    assert calls == []
    assert set(service.runtime.connectors) == {beehiiv["id"], rss["id"]}
    assert x_account["id"] not in service.runtime.connectors
    assert service.configuration.x_write_connector_account_id == x_account["id"]
    assert service.configuration.beehiiv_write_connector_account_id == beehiiv["id"]
    assert "x" in service.dispatcher.publishers
    assert service.runtime.orchestrator.kpi_projector is not None
    assert service.runtime.orchestrator.source_campaign_operator is not None
    assert {EXPERIMENT_WINDOW_COLLECT_JOB_TYPE, EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE} <= set(
        service.runtime.worker.handlers
    )
    safe = repr(service) + repr(service.configuration.as_dict())
    assert "x-secret" not in safe
    assert "beehiiv-secret" not in safe


def test_beehiiv_secret_is_revealed_only_when_request_is_executed(tmp_path):
    database, key, brand, beehiiv, _, _ = configured_database(tmp_path)
    calls = []
    service = build_service_runtime(
        "worker", database, key,
        lambda account: RecordingTransport(account["connector_type"], calls),
    )
    reveals = []
    original_secret = service.credential_store.secret

    def tracked_secret(provider, account_id):
        reveals.append((provider, account_id))
        return original_secret(provider, account_id)

    service.credential_store.secret = tracked_secret
    enqueue_sync_job(
        brand_id=brand["id"], connector_account_id=beehiiv["id"],
        stream="posts", idempotency_key="beehiiv-cycle-1",
    )
    assert reveals == []
    result = service.run_once()
    assert result["status"] == "completed"
    assert reveals == [("beehiiv", "pub_demo")]
    assert calls[0]["headers"]["Authorization"] == "Bearer beehiiv-secret"
    assert "beehiiv-secret" not in repr(result)
    assert "beehiiv-secret" not in repr(service)


def test_periodic_native_beehiiv_sync_projects_subscribers_and_issue_metrics(tmp_path, monkeypatch):
    # The seeded mission window ends 2026-10-01, so pin the connector clock inside
    # it. Otherwise KPI projection is skipped as outside_mission_window.
    monkeypatch.setattr(
        "brandman.connectors.BeehiivConnector._now", lambda self: "2026-09-15T12:00:00+00:00",
    )
    database, key, brand, beehiiv, _, _ = configured_database(tmp_path)
    store.ensure_growth_mission("demo-brand")
    scopes = ["posts.read", "posts.write", "publications.read"]
    store.upsert_connector_account(
        brand["id"], "beehiiv", "pub_demo", "Demo Brand Beehiiv",
        status="healthy", scopes=scopes,
        capabilities=["posts.read", "drafts.write", "publication.stats.read"],
    )
    CredentialStore(database, key).put(
        "beehiiv", "pub_demo", "Beehiiv", {"api_key": "beehiiv-secret"},
        required_scopes=scopes, granted_scopes=scopes,
    )
    editorial = EditorialStore(database)
    issue = editorial.create_issue(
        brand["id"], {
            "subject": "Measured", "preview_text": "Measured issue",
            "final_title": "Measured issue", "sections": [{"body": "Body"}],
        }, created_by="writer",
    )
    with editorial._connect() as connection:
        connection.execute(
            """UPDATE newsletter_issues SET lifecycle='exported',
               beehiiv_external_id='post_123' WHERE id=?""", (issue["id"],),
        )
    graph = CampaignGraphStore(database)
    campaign = graph.create_campaign(
        brand["id"], "Measured issue", "Learn from newsletter results", actor="tester",
    )
    membership = graph.attach(
        campaign["id"], asset_type="newsletter_issue", asset_id=issue["id"],
        channel="newsletter", role="anchor", attribution_primary=True,
        actor="tester", reason="Bind exported issue",
    )

    class MetricsTransport:
        def request(self, method, url, **kwargs):
            if url.endswith("/publications/pub_demo"):
                return HttpResponse(200, json.dumps({"data": {"stats": {
                    "active_subscriptions": 18, "active_free_subscriptions": 18,
                    "active_premium_subscriptions": 0,
                }}}).encode())
            return HttpResponse(200, json.dumps({
                "data": [{
                    "id": "post_123", "title": "Measured issue", "status": "confirmed",
                    "stats": {"email": {
                        "delivered": 100, "unique_opens": 45,
                        "unique_clicks": 8, "unsubscribes": 1,
                    }},
                }], "pagination": {"page": 1, "total_pages": 1},
            }).encode())

    service = build_service_runtime(
        "beehiiv-metrics-worker", database, key,
        lambda account: MetricsTransport() if account["id"] == beehiiv["id"]
        else RecordingTransport(account["connector_type"], []),
    )
    enqueue_sync_job(
        brand_id=brand["id"], connector_account_id=beehiiv["id"],
        stream="content", idempotency_key="scheduled-beehiiv-cycle-1",
    )
    completed = service.run_until_idle(max_jobs=1).jobs[0]

    assert completed["status"] == "completed"
    assert completed["result"]["kpis_projected"] == 1
    assert completed["result"]["campaign_metrics_projected"] == 1
    assert graph.measurement(campaign["id"])["assets"][membership["id"]][
        "native_totals"
    ] == {"delivered": 100, "opens": 45, "clicks": 8, "unsubscribes": 1}


def test_bounded_runtime_executes_approved_x_delivery(tmp_path):
    database, key, brand, _, x_account, _ = configured_database(tmp_path)
    calls = []
    service = build_service_runtime(
        "worker", database, key,
        lambda account: RecordingTransport(account["connector_type"], calls),
        retry_base_seconds=0,
    )
    item = service.dispatcher.create(
        "x", {"body": "Approved post"}, brand_id=brand["id"]
    )
    service.dispatcher.submit_for_approval(item.id, actor="agent")
    service.dispatcher.approve(item.id, revision=1, approver="chris")
    queued = service.dispatcher.queue(item.id, actor="chris")
    enqueue_x_delivery(queued, x_account["id"])

    run = service.run_until_idle(max_jobs=1)
    assert run.processed == 1
    assert run.reached_limit is True
    published = service.dispatcher.store.get(item.id)
    assert published.external_id == "123456789"
    assert [call["connector"] for call in calls] == ["x"]
    assert "x-secret" not in repr(run.as_dict())


def test_bounded_runtime_exports_only_exact_approved_newsletter_draft(tmp_path):
    database, key, brand, beehiiv, _, _ = configured_database(tmp_path)
    editorial = EditorialStore(database)
    issue = editorial.create_issue(brand["id"], {
        "editorial_thesis": "Make one useful decision.",
        "target_reader": "Readers",
        "intended_outcome": "Choose a card strategy",
        "working_title": "A useful decision", "final_title": "A useful decision",
        "subject": "A useful decision", "preview_text": "The useful math",
        "sections": [{"heading": "Decision", "body": "Details"}],
        "cta": {"label": "Read more", "url": "https://demo.example"},
        "seo": {"title": "A useful decision", "description": "Guidance"},
        "content_basis": {"kind": "original_analysis", "statement": "DemoBrand card-strategy analysis."},
        "claims": [], "source_provenance": [],
    }, created_by="agent")
    editorial.transition(issue["id"], IssueLifecycle.OUTLINE)
    editorial.transition(issue["id"], IssueLifecycle.DRAFT)
    editorial.record_fact_check(
        issue["id"], expected_revision=1, reviewer="chris"
    )
    editorial.approve_issue(issue["id"], approver="chris", expected_revision=1)
    job = enqueue_newsletter_export(
        editorial, issue["id"], connector_account_id=beehiiv["id"]
    )
    calls = []
    service = build_service_runtime(
        "worker", database, key,
        lambda account: RecordingTransport(account["connector_type"], calls),
    )

    completed = service.run_until_idle(max_jobs=1).jobs[0]

    assert completed["id"] == job["id"]
    assert completed["status"] == "completed"
    assert completed["result"]["external_id"] == "beehiiv-draft-1"
    assert editorial.get_issue(issue["id"])["lifecycle"] == "exported"
    create = next(call for call in calls if call["connector"] == "beehiiv")
    assert create["json_body"]["status"] == "draft"
    assert not ({"publish", "send_at", "schedule_at"} & set(create["json_body"]))
    assert BEEHIIV_NEWSLETTER_EXPORT_JOB in service.runtime.worker.handlers


def test_unhealthy_or_under_scoped_accounts_are_not_mapped(tmp_path):
    database, key, _, beehiiv, x_account, _ = configured_database(tmp_path)
    brand = store.get_brand("demo-brand")
    store.upsert_connector_account(
        brand["id"], "beehiiv", "pub_demo", "Beehiiv",
        status="unhealthy", scopes=["posts.read"], capabilities=["posts.read"],
    )
    store.upsert_connector_account(
        brand["id"], "x", "demobrand", "X",
        status="healthy", scopes=["tweet.read"], capabilities=["posts.read"],
    )
    calls = []
    service = build_service_runtime(
        "worker", database, key,
        lambda account: RecordingTransport(account["connector_type"], calls),
    )
    assert beehiiv["id"] not in service.runtime.connectors
    assert x_account["id"] in service.configuration.skipped_connector_account_ids
    assert "x" not in service.dispatcher.publishers
    assert "x.dispatch" not in service.runtime.worker.handlers
    assert calls == []


def test_rss_connector_is_built_from_persisted_url_and_runs_bounded(tmp_path):
    database, key, brand, _, _, rss = configured_database(tmp_path)
    calls = []
    service = build_service_runtime(
        "worker", database, key,
        lambda account: RecordingTransport(account["connector_type"], calls),
    )
    enqueue_sync_job(
        brand_id=brand["id"], connector_account_id=rss["id"],
        stream="content", idempotency_key="rss-cycle-1",
    )
    run = service.run_until_idle(max_jobs=1)
    assert run.processed == 1
    assert run.jobs[0]["result"]["editorial_candidates_projected"] == 1
    # A title-only generic feed item is retained as intelligence, but does not
    # clear the mission-fit threshold for campaign/post promotion.
    assert store.row("SELECT status FROM campaigns WHERE brand_id=?", (brand["id"],)) is None
    assert store.row("SELECT status FROM posts") is None
    assert store.row("SELECT promotion_state FROM source_intelligence_records")["promotion_state"] == "backlog"
    assert calls[0]["url"] == "https://example.test/feed.xml"


def test_multiple_writable_x_accounts_fail_closed(tmp_path):
    database, key, brand, _, _, _ = configured_database(tmp_path)
    store.upsert_connector_account(
        brand["id"], "x", "second", "Second X",
        status="healthy",
        scopes=["tweet.read", "users.read", "tweet.write", "offline.access"],
        capabilities=["posts.write"],
    )
    CredentialStore(database, key).put(
        "x", "second", "Second X", {
            "access_token": "second-secret", "refresh_token": "second-refresh",
            "client_id": "second-client", "expires_at": "2027-09-02T00:00:00Z",
        },
        required_scopes=["tweet.read", "users.read", "tweet.write", "offline.access"],
        granted_scopes=["tweet.read", "users.read", "tweet.write", "offline.access"],
    )
    with pytest.raises(ServiceRuntimeConfigurationError, match="account-scoped") as error:
        build_service_runtime(
            "worker", database, key,
            lambda account: RecordingTransport(account["connector_type"], []),
        )
    assert "second-secret" not in str(error.value)


def test_production_composes_separate_x_read_for_metrics_and_engagement(tmp_path):
    database, key, brand, _, x_write, _ = configured_database(tmp_path)
    store.ensure_growth_mission("demo-brand")
    x_read = store.upsert_connector_account(
        brand["id"], "x", "demobrand-read", "DemoBrand X read",
        status="healthy", scopes=["tweet.read", "users.read", "offline.access"],
        configuration={
            "user_id": "42", "username": "demobrand",
            "include_profile_metrics": False,
            "include_tweet_metrics": True,
            "include_mentions": True,
            "include_replies": False,
        },
    )
    CredentialStore(database, key).put(
        "x", "demobrand-read", "DemoBrand X read",
            {
                "access_token": "x-read-only-value", "refresh_token": "x-read-refresh",
                "client_id": "x-read-client", "expires_at": "2027-09-02T00:00:00Z",
            },
        required_scopes=["tweet.read", "users.read", "offline.access"],
        granted_scopes=["tweet.read", "users.read", "offline.access"],
    )
    calls = []

    class XReadTransport:
        responses = [
            HttpResponse(200, json.dumps({"data": [{
                "id": "post-1", "author_id": "42",
                "public_metrics": {"impression_count": 30, "like_count": 2},
            }]}).encode()),
            HttpResponse(200, json.dumps({
                "data": [{
                    "id": "mention-1", "text": "What do you think?", "author_id": "77",
                    "created_at": "2026-09-02T12:00:00Z",
                    "conversation_id": "mention-1", "public_metrics": {},
                }],
                "includes": {"users": [{"id": "77", "username": "reader"}]},
            }).encode()),
        ]

        def request(self, method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            return self.responses.pop(0)

    transport = XReadTransport()
    service = build_service_runtime(
        "x-read-worker", database, key,
        lambda account: transport if account["id"] == x_read["id"]
        else RecordingTransport(account["connector_type"], calls),
    )

    assert calls == []
    assert x_read["id"] in service.runtime.connectors
    assert service.configuration.x_write_connector_account_id == x_write["id"]
    enqueue_sync_job(
        brand_id=brand["id"], connector_account_id=x_read["id"],
        stream="content", idempotency_key="x-read-cycle",
    )
    run = service.run_until_idle(max_jobs=2)

    assert run.completed == 2
    assert all(call["method"] == "GET" for call in calls)
    assert len(store.rows("SELECT * FROM performance_records WHERE channel='x'")) == 1
    opportunity = store.row("SELECT * FROM x_engagement_opportunities")
    assert opportunity["external_post_id"] == "mention-1"
    assert opportunity["state"] == "new"
    assert "x-read-only-value" not in repr(run.as_dict())


def test_production_composes_website_analytics_and_promotes_evidence(tmp_path):
    database, key, brand, _, _, _ = configured_database(tmp_path)
    mission = store.ensure_growth_mission("demo-brand")
    website = store.upsert_connector_account(
        brand["id"], "website", "demobrand-analytics", "DemoBrand analytics",
        status="healthy", scopes=["analytics.read"],
        configuration={"endpoint_url": "https://demo.test/analytics"},
    )
    CredentialStore(database, key).put(
        "website", "demobrand-analytics", "DemoBrand analytics",
        {"access_token": "website-read-only-value"},
        required_scopes=["analytics.read"], granted_scopes=["analytics.read"],
    )
    calls = []

    class WebsiteTransport:
        def request(self, method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            return HttpResponse(200, json.dumps({"data": [{
                "id": "subscriber-snapshot-1",
                "observed_at": "2026-09-02T12:00:00Z",
                "metric": "active_beehiiv_subscribers", "value": 18,
                "evidence_type": "website_subscriber_count",
            }]}).encode())

    transport = WebsiteTransport()
    service = build_service_runtime(
        "website-worker", database, key,
        lambda account: transport if account["id"] == website["id"]
        else RecordingTransport(account["connector_type"], calls),
    )
    assert calls == []
    assert website["id"] in service.runtime.connectors
    enqueue_sync_job(
        brand_id=brand["id"], connector_account_id=website["id"],
        stream="metrics", idempotency_key="website-cycle",
    )
    completed = service.run_once()

    assert completed["status"] == "completed"
    assert completed["result"]["performance_recorded"] == 1
    assert completed["result"]["kpis_projected"] == 1
    current = store.mission_progress(mission["id"])
    subscriber_goal = next(
        goal for goal in current["goals"]
        if goal["metric"] == "active_beehiiv_subscribers"
    )
    assert subscriber_goal["current"] == 18
    assert calls[0]["method"] == "GET"
    assert "website-read-only-value" not in repr(completed)


def test_x_writer_prerequisite_read_scopes_never_register_it_as_reader(tmp_path):
    database, key, brand, _, original_writer, _ = configured_database(tmp_path)
    store.upsert_connector_account(
        brand["id"], "x", original_writer["account_key"], "Original writer",
        status="unhealthy",
        scopes=["tweet.read", "users.read", "tweet.write", "offline.access"],
    )
    combined = store.upsert_connector_account(
        brand["id"], "x", "combined", "Combined X",
        status="healthy",
        scopes=["tweet.read", "users.read", "tweet.write", "offline.access"],
        configuration={"user_id": "42", "username": "demobrand"},
    )
    CredentialStore(database, key).put(
        "x", "combined", "Combined X", {
            "access_token": "combined-value", "refresh_token": "combined-refresh",
            "client_id": "combined-client", "expires_at": "2027-09-02T00:00:00Z",
        },
        required_scopes=["tweet.read", "users.read", "tweet.write", "offline.access"],
        granted_scopes=["tweet.read", "users.read", "tweet.write", "offline.access"],
    )
    service = build_service_runtime(
        "separation-worker", database, key,
        lambda account: RecordingTransport(account["connector_type"], []),
    )
    assert combined["id"] not in service.runtime.connectors
    assert service.configuration.x_write_connector_account_id == combined["id"]


def test_connector_configuration_rejects_secret_fields(tmp_path):
    database, _, brand, _, _, _ = configured_database(tmp_path)
    with pytest.raises(ValueError, match="secret connector configuration"):
        store.upsert_connector_account(
            brand["id"], "x", "unsafe", "Unsafe", status="healthy",
            scopes=["tweet.read", "users.read", "offline.access"],
            configuration={
                "user_id": "42", "username": "demobrand",
                "access_token": "must-not-be-stored",
            },
        )
    serialized = database.read_bytes()
    assert b"must-not-be-stored" not in serialized


def test_service_runtime_executes_queued_read_only_health_probe(tmp_path):
    database, key, brand, _, _, rss = configured_database(tmp_path)
    calls = []
    service = build_service_runtime(
        "health-worker", database, key,
        lambda account: RecordingTransport(account["connector_type"], calls),
    )
    check = service.health_store.trigger(
        brand["id"], actor="chris", connector_account_id=rss["id"],
    )[0]
    completed_job = service.run_once()

    assert completed_job["job_type"] == HEALTH_CHECK_JOB_TYPE
    assert completed_job["status"] == "completed"
    completed = service.health_store.get(check["id"])
    assert completed["status"] == "healthy"
    assert completed["provider_responded"] is True
    assert [call["method"] for call in calls] == ["GET"]
