from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import sqlite3

import pytest

from brandman import store
from brandman.scheduler import (
    CONNECTOR_SYNC, DOWNSTREAM_JOB, OPERATING_PLAN_JOB_TYPE,
    PeriodicOrchestrator,
)


def setup_database(tmp_path):
    database = tmp_path / "scheduler.db"
    store.DATA_PATH = database
    store.init_db()
    mission = store.ensure_demo_brand_growth_mission()
    brand = store.get_brand("demo-brand")
    account = store.upsert_connector_account(
        brand["id"], "rss", "https://example.test/feed", "Example feed",
        status="healthy", scopes=[], capabilities=["content.read"],
    )
    return database, brand, mission, account


def test_default_tick_is_bounded_idempotent_and_never_enqueues_public_actions(tmp_path):
    database, _, _, _ = setup_database(tmp_path)
    now = datetime(2026, 9, 2, 12, tzinfo=UTC)
    scheduler = PeriodicOrchestrator(database, clock=lambda: now)
    schedules = scheduler.ensure_defaults()
    assert {item["action_type"] for item in schedules} == {
        "connector_sync", "operating_plan_refresh"
    }

    first = scheduler.tick(max_decisions=10)
    second = scheduler.tick(max_decisions=10)
    assert first.decisions_planned == 2
    assert first.jobs_enqueued == 2
    assert second.decisions_planned == 0
    jobs = store.rows("SELECT * FROM durable_jobs ORDER BY job_type")
    assert {job["job_type"] for job in jobs} == {
        "connector.sync", OPERATING_PLAN_JOB_TYPE
    }
    assert not {"x.dispatch", "beehiiv.newsletter.export"} & {
        job["job_type"] for job in jobs
    }


def test_tick_skips_missed_intervals_without_a_job_storm_and_uses_clock(tmp_path):
    database, brand, _, account = setup_database(tmp_path)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    scheduler = PeriodicOrchestrator(database, clock=lambda: start)
    scheduler.ensure_schedule(
        "one", name="one", action_type=CONNECTOR_SYNC, interval_seconds=900,
        payload={"brand_id": brand["id"], "connector_account_id": account["id"],
                 "stream": "content"},
        brand_id=brand["id"], connector_account_id=account["id"],
        next_run_at=start.isoformat(),
    )
    future = start + timedelta(days=3)
    result = scheduler.tick(as_of=future.isoformat(), max_decisions=1)
    schedule = scheduler.list_schedules()[0]
    assert result.decisions_planned == 1
    assert datetime.fromisoformat(schedule["next_run_at"]) > future
    assert len(store.rows("SELECT id FROM durable_jobs")) == 1


def test_tick_suppresses_overlapping_sync_and_dns_retry_amplification(tmp_path):
    database, brand, _, account = setup_database(tmp_path)
    now = datetime(2026, 9, 2, 12, tzinfo=UTC)
    scheduler = PeriodicOrchestrator(database, clock=lambda: now)
    scheduler.ensure_schedule(
        "rss", name="rss", action_type=CONNECTOR_SYNC, interval_seconds=900,
        payload={"brand_id": brand["id"], "connector_account_id": account["id"],
                 "stream": "content"}, brand_id=brand["id"],
        connector_account_id=account["id"], next_run_at=now.isoformat(),
    )
    store.enqueue_job(
        "connector.sync", "existing", {"connector_account_id": account["id"], "stream": "content"},
        brand_id=brand["id"], connector_account_id=account["id"], run_after=now.isoformat(),
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO sync_cursors(connector_account_id,stream,cursor,created_at,updated_at)
               VALUES (?,?,?,?,?)""",
            (account["id"], "content", "rss:durable-before-incident",
             now.isoformat(), now.isoformat()),
        )
    active = scheduler.tick()
    assert active.jobs_enqueued == 0
    assert store.rows("SELECT status,error FROM orchestration_decisions") == [
        {"status": "suppressed", "error": "equivalent connector sync already active"}
    ]
    assert store.rows(
        "SELECT cursor FROM sync_cursors WHERE connector_account_id=?", (account["id"],)
    ) == [{"cursor": "rss:durable-before-incident"}]

    with sqlite3.connect(database) as connection:
        connection.execute(
            """UPDATE durable_jobs SET status='needs_attention',attempt_count=max_attempts,
               last_error='rss DNS resolution failed',updated_at=?""",
            (now.isoformat(),),
        )
        connection.execute(
            "UPDATE periodic_schedules SET next_run_at=?",
            ((now + timedelta(minutes=15)).isoformat(),),
        )
    cooled = scheduler.tick(as_of=(now + timedelta(minutes=15)).isoformat())
    assert cooled.jobs_enqueued == 0
    assert store.rows("SELECT error FROM orchestration_decisions ORDER BY due_at")[-1]["error"] == (
        "rss DNS incident cooling down after retry exhaustion"
    )
    assert store.rows(
        "SELECT cursor FROM sync_cursors WHERE connector_account_id=?", (account["id"],)
    ) == [{"cursor": "rss:durable-before-incident"}]

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE periodic_schedules SET next_run_at=?",
            ((now + timedelta(hours=2)).isoformat(),),
        )
    resumed = scheduler.tick(as_of=(now + timedelta(hours=2)).isoformat())
    assert resumed.jobs_enqueued == 1


def test_brand_scoped_tick_never_enqueues_another_brands_due_schedule(tmp_path):
    database, brand, _, account = setup_database(tmp_path)
    other = store.create_brand({
        "slug": "other", "name": "Other", "mission": "Other mission", "voice": "Other voice",
        "compliance_rules": "Other rules", "approval_policy": "human_approval_required",
    })
    other_account = store.upsert_connector_account(
        other["id"], "rss", "https://other.test/feed", "Other feed",
        status="healthy", scopes=[], capabilities=["content.read"],
    )
    now = datetime(2026, 9, 2, tzinfo=UTC)
    scheduler = PeriodicOrchestrator(database, clock=lambda: now)
    for key, scoped_brand, scoped_account in (
        ("points", brand, account), ("other", other, other_account),
    ):
        scheduler.ensure_schedule(
            key, name=key, action_type=CONNECTOR_SYNC, interval_seconds=900,
            payload={"brand_id": scoped_brand["id"], "connector_account_id": scoped_account["id"], "stream": "content"},
            brand_id=scoped_brand["id"], connector_account_id=scoped_account["id"],
            next_run_at=now.isoformat(),
        )
    result = scheduler.tick(brand_id=brand["id"])
    assert result.decisions_planned == 1
    jobs = store.rows("SELECT brand_id FROM durable_jobs")
    assert jobs == [{"brand_id": brand["id"]}]
    assert scheduler.list_schedules(brand_id=other["id"])[0]["next_run_at"] == now.isoformat()
    scoped = scheduler.status(brand_id=brand["id"])
    assert scoped["schedules"] == {"total": 1, "enabled": 1, "due": 0}
    assert scoped["pending_decisions"] == 0
    assert scoped["latest_tick"]["brand_id"] == brand["id"]
    assert scheduler.status(brand_id=other["id"])["latest_tick"] is None
    with pytest.raises(KeyError, match="unknown periodic schedule"):
        scheduler.set_schedule_enabled("other", False, brand_id=brand["id"])
    assert scheduler.list_schedules(brand_id=other["id"])[0]["enabled"] is True


def test_failed_enqueue_is_reconciled_after_restart_with_stable_identity(tmp_path, monkeypatch):
    database, brand, _, account = setup_database(tmp_path)
    now = datetime(2026, 9, 2, tzinfo=UTC)
    scheduler = PeriodicOrchestrator(database, clock=lambda: now)
    scheduler.ensure_schedule(
        "recover", name="recover", action_type=CONNECTOR_SYNC, interval_seconds=900,
        payload={"brand_id": brand["id"], "connector_account_id": account["id"],
                 "stream": "content"}, brand_id=brand["id"],
        connector_account_id=account["id"], next_run_at=now.isoformat(),
    )
    original = store.enqueue_job
    monkeypatch.setattr(store, "enqueue_job", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError()))
    failed = scheduler.tick()
    assert failed.jobs_enqueued == 0
    assert scheduler.status()["pending_decisions"] == 1

    monkeypatch.setattr(store, "enqueue_job", original)
    restarted = PeriodicOrchestrator(database, clock=lambda: now)
    recovered = restarted.tick()
    assert recovered.decisions_planned == 0
    assert recovered.jobs_enqueued == 1
    assert restarted.status()["pending_decisions"] == 0
    assert len(store.rows("SELECT id FROM durable_jobs")) == 1


def test_downstream_schedules_fail_closed_without_explicit_safe_allowlist(tmp_path):
    database, brand, _, _ = setup_database(tmp_path)
    scheduler = PeriodicOrchestrator(database)
    with pytest.raises(ValueError, match="allowlisted"):
        scheduler.ensure_schedule(
            "publish", name="publish", action_type=DOWNSTREAM_JOB,
            interval_seconds=900,
            payload={"job_type": "x.dispatch", "payload": {"body": "no"}},
            brand_id=brand["id"],
        )


def test_operating_plan_job_handler_runs_via_service_runtime(tmp_path):
    from cryptography.fernet import Fernet
    from brandman.service_runtime import build_service_runtime

    database, brand, mission, account = setup_database(tmp_path)
    store.upsert_connector_account(
        brand["id"], "rss", account["account_key"], "Example feed",
        status="unhealthy", scopes=[], capabilities=["content.read"],
    )
    runtime = build_service_runtime(
        "scheduler-test", database, Fernet.generate_key().decode(),
        lambda _account: (_ for _ in ()).throw(AssertionError("no transport expected")),
    )
    result = runtime.tick(max_decisions=10)
    run = runtime.run_until_idle(max_jobs=10)
    assert result["jobs_enqueued"] >= 1
    assert any(job["job_type"] == OPERATING_PLAN_JOB_TYPE and
               job["status"] == "completed" for job in run.jobs)
    artifacts = store.rows(
        "SELECT kind,payload FROM mission_daily_artifacts WHERE mission_id=?", (mission["id"],)
    )
    assert {item["kind"] for item in artifacts} == {"morning_plan", "end_of_day_scorecard"}
    assert all(json.loads(item["payload"]) for item in artifacts)
