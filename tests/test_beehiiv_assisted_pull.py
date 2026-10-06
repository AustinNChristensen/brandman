from datetime import UTC, datetime, timedelta
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from app import store
import app.beehiiv_assisted_pull as assisted_pull_module
from app.beehiiv_assisted_pull import (
    ASSISTED_BEEHIIV_PULL_JOB_TYPE, BeehiivAssistedPullError,
    BeehiivAssistedPullStore, make_assisted_beehiiv_pull_handler,
)
from app.campaign_graph import CampaignGraphStore
from app.execution_agents import ExecutionAgentRegistry
from app.execution_handoff import ExecutionHandoffStore
from app.editorial import EditorialStore
from app.dispatch import GovernedDispatcher, SQLiteDispatchStore
from app.jobs import JobWorker
from app.scheduler import ASSISTED_BEEHIIV_PULL, PeriodicOrchestrator
from app.main import app, beehiiv_assisted_pull_store


NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)


def aggregate_post(post_id="post_aggregate", *, delivered=100, opens=45, clicks=8):
    return {
        "id": post_id, "title": "The weekly points briefing", "status": "published",
        "published_at": NOW.isoformat(),
        "editor_url": f"https://app.beehiiv.com/posts/{post_id}",
        "stats": {"email": {
            "delivered": delivered, "unique_opens": opens,
            "unique_clicks": clicks, "unsubscribes": 1,
        }},
    }


def setup_assisted(tmp_path, monkeypatch, *, clock=lambda: NOW):
    database = tmp_path / "assisted-pull.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    store.init_db()
    brand = store.get_brand("demo-brand")
    account = store.upsert_connector_account(
        brand["id"], "beehiiv", "pub_points", "DemoBrand Beehiiv",
        status="connected", capabilities=["browser.assisted", "beehiiv_read"],
        configuration={"delivery_mode": "browser_assisted", "connection_role": "beehiiv_read"},
    )
    agents = ExecutionAgentRegistry(database, clock=clock)
    agents.configure(brand["id"], "points-browser", "browser")
    agents.heartbeat(brand["id"], "points-browser")
    return database, brand, account


def test_periodic_scheduler_durably_requests_one_stable_assisted_pull(tmp_path, monkeypatch):
    database, brand, account = setup_assisted(tmp_path, monkeypatch)
    scheduler = PeriodicOrchestrator(database, clock=lambda: NOW)
    schedules = scheduler.ensure_defaults()
    assisted = [row for row in schedules if row["action_type"] == ASSISTED_BEEHIIV_PULL]
    assert len(assisted) == 1
    assert assisted[0]["interval_seconds"] == 21_600
    assert assisted[0]["payload"] == {
        "brand_id": brand["id"], "connector_account_id": account["id"],
    }
    first_tick = scheduler.tick(as_of=NOW.isoformat()).as_dict()
    replay_tick = scheduler.tick(as_of=NOW.isoformat()).as_dict()
    assert first_tick["jobs_enqueued"] >= 1
    assert replay_tick["jobs_enqueued"] == 0
    jobs = store.rows(
        "SELECT * FROM durable_jobs WHERE job_type=?", (ASSISTED_BEEHIIV_PULL_JOB_TYPE,),
    )
    assert len(jobs) == 1

    worker = JobWorker("safe-assisted-worker")
    worker.register(
        ASSISTED_BEEHIIV_PULL_JOB_TYPE, make_assisted_beehiiv_pull_handler(database),
    )
    completed_job = worker.run_once(as_of=jobs[0]["run_after"])
    assert completed_job["status"] == "completed"
    tasks = BeehiivAssistedPullStore(database, clock=lambda: NOW).list(brand["id"])
    assert len(tasks) == 1
    assert tasks[0]["status"] == "pending"
    assert tasks[0]["read_only"] is True
    assert tasks[0]["forbidden_actions"] == [
        "subscriber_data", "draft", "schedule", "send", "publish",
    ]


def test_claim_heartbeat_and_aggregate_receipt_are_idempotent_and_pii_free(
    tmp_path, monkeypatch,
):
    database, brand, account = setup_assisted(tmp_path, monkeypatch)
    tasks = BeehiivAssistedPullStore(database, clock=lambda: NOW)
    task = tasks.ensure(
        brand_id=brand["id"], connector_account_id=account["id"],
        scheduled_for=NOW.isoformat(),
    )
    assert tasks.ensure(
        brand_id=brand["id"], connector_account_id=account["id"],
        scheduled_for=NOW.isoformat(),
    )["id"] == task["id"]
    claimed = tasks.claim(task["id"], actor="points-browser", lease_seconds=120)
    assert "claim_token" in claimed
    renewed = tasks.heartbeat(
        task["id"], actor="points-browser", claim_token=claimed["claim_token"],
        lease_seconds=300,
    )
    assert renewed["claim_expires_at"] == (NOW + timedelta(seconds=300)).isoformat()
    payload = {
        "actor": "points-browser", "claim_token": claimed["claim_token"],
        "observed_at": NOW.isoformat(),
        "posts": [{
            "id": "post_abc-123", "title": "The weekly points briefing",
            "status": "published", "editor_url": "https://app.beehiiv.com/posts/post_abc-123",
            "subtitle": "Safe summary", "subscriber_email": "never-store@example.com",
            "stats": {"email": {"delivered": 100, "unique_opens": 45,
                                  "unique_clicks": 8, "unsubscribes": 1}},
        }],
        "publication_stats": {
            "active_subscriptions": 2500, "active_free_subscriptions": 2400,
            "active_premium_subscriptions": 100,
            "subscribers": [{"email": "never-store@example.com"}],
        },
    }
    completed = tasks.submit_receipt(task["id"], **payload)
    replay = tasks.submit_receipt(task["id"], **payload)
    assert completed == replay
    assert completed["status"] == "completed"
    assert completed["posts_received"] == 1
    assert completed["metadata_synced"] == 1
    assert completed["measurements_recorded"] == 2
    assert completed["post_measurements_received"] == 1
    assert completed["requires_post_measurement"] is True
    assert "claim_token_hash" not in completed
    serialized = "\n".join(
        str(row) for table in ("connector_events", "sources", "performance_records")
        for row in store.rows(f"SELECT * FROM {table}")
    )
    assert "never-store@example.com" not in serialized
    assert len(store.rows("SELECT * FROM connector_events")) == 2
    assert [event["action"] for event in tasks.audit(task["id"])] == [
        "requested", "claimed", "lease_renewed", "aggregate_receipt_recorded",
    ]


def test_malformed_or_pii_shaped_provider_data_cannot_partially_complete(
    tmp_path, monkeypatch,
):
    database, brand, account = setup_assisted(tmp_path, monkeypatch)
    tasks = BeehiivAssistedPullStore(database, clock=lambda: NOW)
    task = tasks.ensure(
        brand_id=brand["id"], connector_account_id=account["id"],
        scheduled_for=NOW.isoformat(),
    )
    claim = tasks.claim(task["id"], actor="points-browser")
    with pytest.raises(ValueError, match="canonical post_"):
        tasks.submit_receipt(
            task["id"], actor="points-browser", claim_token=claim["claim_token"],
            observed_at=NOW.isoformat(), posts=[{
                "id": "victim@example.com", "title": "Bad", "status": "published",
                "stats": {"email": {"delivered": -1, "unique_clicks": 5}},
            }], publication_stats=None,
        )
    assert tasks.get(task["id"])["status"] == "claimed"
    assert store.rows("SELECT * FROM connector_events") == []
    assert store.rows("SELECT * FROM sources") == []


def test_publication_total_cannot_complete_required_post_measurement_pull(
    tmp_path, monkeypatch,
):
    database, brand, account = setup_assisted(tmp_path, monkeypatch)
    tasks = BeehiivAssistedPullStore(database, clock=lambda: NOW)
    task = tasks.ensure(
        brand_id=brand["id"], connector_account_id=account["id"],
        scheduled_for=NOW.isoformat(),
    )
    claim = tasks.claim(task["id"], actor="points-browser")
    with pytest.raises(ValueError, match="per-post aggregate measurement"):
        tasks.submit_receipt(
            task["id"], actor="points-browser", claim_token=claim["claim_token"],
            observed_at=NOW.isoformat(), posts=[],
            publication_stats={"active_subscriptions": 123},
        )
    assert tasks.get(task["id"])["status"] == "claimed"
    assert store.rows("SELECT * FROM connector_events") == []
    assert store.rows("SELECT * FROM sources") == []
    assert store.rows("SELECT * FROM performance_records") == []


def test_structured_pull_records_metadata_and_exact_campaign_performance(
    tmp_path, monkeypatch,
):
    database, brand, account = setup_assisted(tmp_path, monkeypatch)
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
               approved_revision=current_revision,approved_by='Chris',approved_at=?,
               beehiiv_external_id='post_campaign' WHERE id=?""",
            (NOW.isoformat(), issue["id"]),
        )
        connection.execute(
            """INSERT INTO newsletter_export_receipts
               (id,issue_id,revision,connector,idempotency_key,payload_fingerprint,
                external_id,preview_url,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            ("receipt-campaign", issue["id"], 1, "beehiiv", "export-campaign",
             "sha256:" + "1" * 64, "post_campaign",
             "https://app.beehiiv.com/posts/post_campaign", NOW.isoformat()),
        )
    graph = CampaignGraphStore(database)
    campaign = graph.create_campaign(
        brand["id"], "Measured issue", "Learn from newsletter outcomes", actor="tester",
    )
    membership = graph.attach(
        campaign["id"], asset_type="newsletter_issue", asset_id=issue["id"],
        channel="newsletter", role="anchor", attribution_primary=True,
        actor="tester", reason="Bind exported newsletter to campaign",
    )
    tasks = BeehiivAssistedPullStore(database, clock=lambda: NOW)
    task = tasks.ensure(
        brand_id=brand["id"], connector_account_id=account["id"],
        scheduled_for=NOW.isoformat(),
    )
    claim = tasks.claim(task["id"], actor="points-browser")
    completed = tasks.submit_receipt(
        task["id"], actor="points-browser", claim_token=claim["claim_token"],
        observed_at=NOW.isoformat(), posts=[
            aggregate_post("post_campaign", delivered=200, opens=100, clicks=25),
            aggregate_post("post_metadata", delivered=50, opens=20, clicks=4),
        ], publication_stats={"active_subscriptions": 2500},
    )

    assert completed["status"] == "completed"
    assert completed["posts_received"] == 2
    assert completed["post_measurements_received"] == 2
    assert completed["metadata_synced"] == 1
    assert completed["measurements_recorded"] == 3
    assert completed["campaign_metrics_recorded"] == 1
    assert completed["measured_campaign_ids"] == [campaign["id"]]
    assert store.row(
        "SELECT external_source_id FROM sources WHERE external_source_id='post_metadata'",
    ) == {"external_source_id": "post_metadata"}
    observation = graph.measurement(campaign["id"])["assets"][membership["id"]][
        "observations"
    ][0]
    assert observation["native_metrics"] == {
        "delivered": 200, "opens": 100, "clicks": 25, "unsubscribes": 1,
    }
    audit = tasks.audit(task["id"])
    assert audit[-1]["detail"]["measured_campaign_ids"] == [campaign["id"]]


def test_expired_leases_retry_then_fail_closed_at_bound(tmp_path, monkeypatch):
    now = [NOW]
    database, brand, account = setup_assisted(tmp_path, monkeypatch, clock=lambda: now[0])
    tasks = BeehiivAssistedPullStore(database, clock=lambda: now[0])
    task = tasks.ensure(
        brand_id=brand["id"], connector_account_id=account["id"],
        scheduled_for=NOW.isoformat(), max_attempts=2,
    )
    tasks.claim(task["id"], actor="points-browser", lease_seconds=60)
    now[0] += timedelta(seconds=61)
    ExecutionAgentRegistry(database, clock=lambda: now[0]).heartbeat(
        brand["id"], "points-browser",
    )
    assert tasks.get(task["id"])["status"] == "pending"
    tasks.claim(task["id"], actor="points-browser", lease_seconds=60)
    now[0] += timedelta(seconds=61)
    assert tasks.get(task["id"])["status"] == "failed"
    with pytest.raises(BeehiivAssistedPullError, match="not available"):
        tasks.claim(task["id"], actor="points-browser")
    assert [event["action"] for event in tasks.audit(task["id"])] == [
        "requested", "claimed", "lease_expired", "claimed", "lease_exhausted",
    ]




def test_provider_kill_switch_invalidates_claim_and_prevents_every_projection(
    tmp_path, monkeypatch,
):
    database, brand, account = setup_assisted(tmp_path, monkeypatch)
    tasks = BeehiivAssistedPullStore(database, clock=lambda: NOW)
    task = tasks.ensure(
        brand_id=brand["id"], connector_account_id=account["id"],
        scheduled_for=NOW.isoformat(),
    )
    claim = tasks.claim(task["id"], actor="points-browser")
    controls = ExecutionHandoffStore(
        database, EditorialStore(database),
        GovernedDispatcher(SQLiteDispatchStore(database)), clock=lambda: NOW,
    )
    controls.set_control(brand["id"], "beehiiv", enabled=False, actor="chris")

    with pytest.raises(BeehiivAssistedPullError, match="valid active claim"):
        tasks.submit_receipt(
            task["id"], actor="points-browser", claim_token=claim["claim_token"],
            observed_at=NOW.isoformat(), posts=[{
                "id": "post_blocked", "title": "Must not persist", "status": "published",
                "stats": {"email": {"delivered": 10, "unique_clicks": 1}},
            }], publication_stats={"active_subscriptions": 999},
        )
    blocked = tasks.get(task["id"])
    assert blocked["status"] == "failed"
    assert blocked["last_failure_code"] == "provider_disabled"
    assert store.rows("SELECT * FROM sources") == []
    assert store.rows("SELECT * FROM connector_events") == []
    assert store.rows("SELECT * FROM performance_records") == []
    assert tasks.audit(task["id"])[-1]["action"] == "invalidated_by_provider_control"


def test_submit_and_concurrent_kill_switch_are_serialized_in_one_writer_transaction(
    tmp_path, monkeypatch,
):
    database, brand, account = setup_assisted(tmp_path, monkeypatch)
    tasks = BeehiivAssistedPullStore(database, clock=lambda: NOW)
    task = tasks.ensure(
        brand_id=brand["id"], connector_account_id=account["id"],
        scheduled_for=NOW.isoformat(),
    )
    claim = tasks.claim(task["id"], actor="points-browser")
    controls = ExecutionHandoffStore(
        database, EditorialStore(database),
        GovernedDispatcher(SQLiteDispatchStore(database)), clock=lambda: NOW,
    )
    entered, release, disabled = Event(), Event(), Event()
    original = assisted_pull_module.ingest_beehiiv_pull

    def paused_ingest(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(assisted_pull_module, "ingest_beehiiv_pull", paused_ingest)
    results = {}

    def submit():
        results["receipt"] = tasks.submit_receipt(
            task["id"], actor="points-browser", claim_token=claim["claim_token"],
            observed_at=NOW.isoformat(), posts=[aggregate_post("post_concurrent")],
            publication_stats={"active_subscriptions": 10},
        )

    def disable():
        results["control"] = controls.set_control(
            brand["id"], "beehiiv", enabled=False, actor="chris",
        )
        disabled.set()

    submit_thread = Thread(target=submit); submit_thread.start()
    assert entered.wait(5)
    disable_thread = Thread(target=disable); disable_thread.start()
    assert not disabled.wait(0.1)  # kill switch waits behind the receipt writer lock
    release.set(); submit_thread.join(5); disable_thread.join(5)
    assert results["receipt"]["status"] == "completed"
    assert results["control"]["enabled"] is False
    assert tasks.get(task["id"])["status"] == "completed"
