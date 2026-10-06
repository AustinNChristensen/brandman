from __future__ import annotations

from pathlib import Path
import sqlite3

from app import store
from app.jobs import JobWorker


def use_database(tmp_path: Path) -> None:
    store.DATA_PATH = tmp_path / "jobs.db"
    # An unprofiled legacy database is intentionally treated as operating;
    # migration must declare that identity rather than inherit pytest's test
    # profile and silently relabel it.
    store.init_db(profile="operating")


def test_connector_events_and_jobs_are_idempotent(tmp_path: Path) -> None:
    use_database(tmp_path)
    brand = store.get_brand("demo-brand")
    assert brand
    connector = store.upsert_connector_account(
        brand["id"], "x", "demobrand", "Demo Brand",
        status="healthy", scopes=["tweet.read"], capabilities=["metrics.read"],
    )
    assert "token" not in connector
    assert connector["scopes"] == ["tweet.read"]

    cursor = store.set_sync_cursor(connector["id"], "metrics", "cursor-2", watermark="2026-09-01T00:00:00Z")
    assert store.get_sync_cursor(connector["id"], "metrics") == cursor

    first_event = store.record_connector_event(
        connector["id"], "metrics", "tweet-1:2026-09-01", "snapshot",
        {"impressions": 10}, "2026-09-01T12:00:00Z",
    )
    duplicate_event = store.record_connector_event(
        connector["id"], "metrics", "tweet-1:2026-09-01", "snapshot",
        {"impressions": 999}, "2026-09-01T12:01:00Z",
    )
    assert duplicate_event["id"] == first_event["id"]
    assert duplicate_event["payload"]["impressions"] == 10

    first_job = store.enqueue_job(
        "x.metrics.sync", "demobrand:metrics:2026-09-01", {"cursor": "cursor-2"},
        brand_id=brand["id"], connector_account_id=connector["id"],
    )
    duplicate_job = store.enqueue_job(
        "x.metrics.sync", "demobrand:metrics:2026-09-01", {"cursor": "different"},
        brand_id=brand["id"], connector_account_id=connector["id"],
    )
    assert duplicate_job["id"] == first_job["id"]
    assert duplicate_job["payload"] == {"cursor": "cursor-2"}


def test_worker_attempts_retry_then_complete(tmp_path: Path) -> None:
    use_database(tmp_path)
    job = store.enqueue_job("test.retry", "one", {"value": 2}, max_attempts=2)
    calls = 0

    def handler(claimed: dict) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary")
        return {"answer": claimed["payload"]["value"] * 2}

    worker = JobWorker("test-worker", retry_base_seconds=0)
    worker.register("test.retry", handler)
    failed = worker.run_once()
    assert failed and failed["status"] == "retry"
    completed = worker.run_once(as_of="9999-12-31T23:59:59+00:00")
    assert completed and completed["status"] == "completed"
    assert completed["result"] == {"answer": 4}
    attempts = store.rows("SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt_number", (job["id"],))
    assert [attempt["status"] for attempt in attempts] == ["failed", "completed"]


def test_worker_retry_uses_explicit_run_clock(tmp_path: Path) -> None:
    use_database(tmp_path)
    store.enqueue_job(
        "test.clock", "one", {}, max_attempts=2,
        run_after="2026-09-02T12:00:00+00:00",
    )
    worker = JobWorker("clock-worker", retry_base_seconds=30)
    worker.register(
        "test.clock", lambda _: (_ for _ in ()).throw(RuntimeError("temporary")),
    )

    failed = worker.run_once(as_of="2026-09-02T12:00:00+00:00")

    assert failed and failed["status"] == "retry"
    assert failed["run_after"] == "2026-09-02T12:00:30+00:00"


def test_terminal_failure_creates_deduplicated_feedback(tmp_path: Path) -> None:
    use_database(tmp_path)
    brand = store.get_brand("demo-brand")
    assert brand
    store.enqueue_job("beehiiv.sync", "daily", {}, brand_id=brand["id"], max_attempts=1)
    worker = JobWorker("test-worker")
    worker.register("beehiiv.sync", lambda _: (_ for _ in ()).throw(RuntimeError("permission missing")))
    failed = worker.run_once()
    assert failed and failed["status"] == "needs_attention"
    feedback = store.rows("SELECT * FROM product_feedback WHERE component='beehiiv.sync'")
    assert len(feedback) == 1
    assert feedback[0]["severity"] == "high"


def test_mission_progress_and_kpi_upsert(tmp_path: Path) -> None:
    use_database(tmp_path)
    brand = store.get_brand("demo-brand")
    assert brand
    mission = store.create_mission(
        brand["id"], "30-day growth", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z",
        timezone="America/Denver",
    )
    store.upsert_mission_goal(mission["id"], "x_followers", 8, 100)
    store.upsert_mission_goal(mission["id"], "active_subscribers", 13, 25)
    first = store.record_kpi_snapshot(
        mission["id"], "x_followers", 20, "2026-09-02T12:00:00Z", "x",
    )
    updated = store.record_kpi_snapshot(
        mission["id"], "x_followers", 21, "2026-09-02T12:00:00Z", "x",
    )
    assert updated["id"] == first["id"]
    progress = store.mission_progress(mission["id"], as_of="2026-09-11T00:00:00Z")
    assert progress
    by_metric = {goal["metric"]: goal for goal in progress["goals"]}
    assert by_metric["x_followers"]["current"] == 21
    assert by_metric["active_subscribers"]["current"] == 13
    assert by_metric["x_followers"]["required_daily_change"] == 3.95
    assert by_metric["x_followers"]["trajectory_status"] == "behind"
    assert by_metric["x_followers"]["trajectory_amount"] > 0
    assert by_metric["x_followers"]["trajectory_days"] < 0


def test_product_feedback_deduplicates_by_fingerprint(tmp_path: Path) -> None:
    use_database(tmp_path)
    brand = store.get_brand("demo-brand")
    assert brand
    kwargs = dict(
        brand_id=brand["id"], reporter="connector", summary="Scope missing",
        details="Cannot read subscribers", component="beehiiv", severity="high",
        fingerprint="beehiiv:subscriber-scope", related_ids=["connection-1"],
    )
    first = store.report_product_feedback(**kwargs)
    second = store.report_product_feedback(**kwargs)
    assert second["id"] == first["id"]
    assert second["occurrence_count"] == 2
    assert second["related_ids"] == ["connection-1"]


def test_init_db_migrates_legacy_feedback_without_data_loss(tmp_path: Path) -> None:
    store.DATA_PATH = tmp_path / "legacy.db"
    conn = sqlite3.connect(store.DATA_PATH)
    conn.execute(
        """CREATE TABLE product_feedback (
           id TEXT PRIMARY KEY, brand_id TEXT, reporter TEXT NOT NULL,
           summary TEXT NOT NULL, details TEXT NOT NULL, status TEXT NOT NULL,
           created_at TEXT NOT NULL)"""
    )
    conn.execute(
        "INSERT INTO product_feedback VALUES ('legacy-1',NULL,'agent','Old issue','Still useful','open','2026-08-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    # Legacy data is fail-safe operating until an operator proves otherwise.
    # Declare that identity explicitly instead of inheriting pytest's profile.
    store.init_db(profile="operating")
    migrated = store.row("SELECT * FROM product_feedback WHERE id='legacy-1'")
    assert migrated
    assert migrated["summary"] == "Old issue"
    assert migrated["component"] == "unknown"
    assert migrated["first_seen_at"] == "2026-08-01T00:00:00Z"
