from __future__ import annotations

from datetime import UTC, datetime

from brandman import store
from brandman.feedback import FeedbackStore
from brandman.jobs import JobWorker
from brandman.connectors import ConnectorKind
from brandman.runtime import BrandOSRuntime
from brandman.scheduler import CONNECTOR_SYNC, PeriodicOrchestrator
from brandman.sync import enqueue_sync_job
from brandman.operational_feedback import (
    report_approval_dead_end, report_missing_permission, report_stale_metric,
    report_successful_workaround,
)


def setup_database(tmp_path):
    database = tmp_path / "operational-feedback.db"
    store.DATA_PATH = database
    store.init_db()
    brand = store.get_brand("demo-brand")
    account = store.upsert_connector_account(
        brand["id"], "beehiiv", "publication-1", "Newsletter",
        status="healthy", scopes=["posts.read"],
    )
    return database, brand, account


def test_terminal_jobs_deduplicate_without_leaking_and_preserve_governed_status(tmp_path):
    database, brand, account = setup_database(tmp_path)
    worker = JobWorker("failure-test")
    worker.register(
        "beehiiv.newsletter.export_draft",
        lambda _job: (_ for _ in ()).throw(RuntimeError("Bearer super-secret-token")),
    )

    def fail(cycle):
        store.enqueue_job(
            "beehiiv.newsletter.export_draft", cycle,
            {
                "issue_id": "11111111-1111-4111-8111-111111111111",
                "mission_id": "api_key=malformed-related-id-secret",
                "revision": 1,
            },
            brand_id=brand["id"], connector_account_id=account["id"], max_attempts=1,
        )
        return worker.run_once(as_of="9999-12-31T00:00:00+00:00")

    assert fail("export-cycle-1")["status"] == "needs_attention"
    feedback = FeedbackStore(database)
    item = feedback.list(brand_id=brand["id"], component="beehiiv.newsletter.export_draft")[0]
    assert item["occurrence_count"] == 1
    assert item["severity"] == "high"
    assert {"11111111-1111-4111-8111-111111111111", account["id"]}.issubset(item["related_ids"])
    assert any(value.startswith("ref:sha256:") for value in item["related_ids"])
    persisted = " ".join(str(item[key]) for key in (
        "summary", "details", "reproduction", "actual_behavior", "workaround",
    ))
    assert "super-secret-token" not in persisted
    assert "malformed-related-id-secret" not in str(item)
    assert "RuntimeError" in item["actual_behavior"]

    feedback.start(item["id"], assignee="operator", actor="operator")
    feedback.resolve(item["id"], actor="chris", resolution_evidence="Connection repaired")
    assert fail("export-cycle-2")["status"] == "needs_attention"
    recurrence = feedback.get(item["id"])
    assert recurrence["occurrence_count"] == 2
    assert recurrence["status"] == "resolved"  # Reporting cannot make a lifecycle decision.


def test_orchestration_enqueue_failure_is_deduplicated_and_safe(tmp_path, monkeypatch):
    database, brand, account = setup_database(tmp_path)
    now = datetime(2026, 9, 2, 12, tzinfo=UTC)
    scheduler = PeriodicOrchestrator(database, clock=lambda: now)
    scheduler.ensure_schedule(
        "beehiiv:posts", name="Sync newsletter", action_type=CONNECTOR_SYNC,
        interval_seconds=900,
        payload={"brand_id": brand["id"], "connector_account_id": account["id"], "stream": "posts"},
        brand_id=brand["id"], connector_account_id=account["id"], next_run_at=now.isoformat(),
    )
    monkeypatch.setattr(
        store, "enqueue_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("api_key=scheduler-secret")),
    )

    assert scheduler.tick().jobs_enqueued == 0
    assert scheduler.tick().jobs_enqueued == 0  # Reconciles the same planned decision.
    rows = FeedbackStore(database).list(
        brand_id=brand["id"], component="orchestration.scheduler",
    )
    assert len(rows) == 1
    assert rows[0]["occurrence_count"] == 2
    assert rows[0]["status"] == "open"
    persisted = " ".join(str(rows[0][key]) for key in (
        "details", "reproduction", "actual_behavior", "workaround",
    ))
    assert "scheduler-secret" not in persisted
    assert account["id"] in rows[0]["related_ids"]


def test_terminal_connector_cycles_increment_two_stable_feedback_records(tmp_path):
    database, brand, account = setup_database(tmp_path)

    class FailingConnector:
        kind = ConnectorKind.BEEHIIV

        def sync(self, cursor=None):
            raise RuntimeError("Authorization: Bearer connector-secret")

    runtime = BrandOSRuntime(
        "connector-failure", {account["id"]: FailingConnector()}, retry_base_seconds=0,
    )
    for cycle in ("cycle-1", "cycle-2"):
        enqueue_sync_job(
            brand_id=brand["id"], connector_account_id=account["id"], stream="posts",
            idempotency_key=cycle,
        )
        # Make each cycle terminal for a compact deterministic proof.
        with store.connection() as connection:
            connection.execute(
                "UPDATE durable_jobs SET max_attempts=1 WHERE idempotency_key=?", (cycle,),
            )
        assert runtime.run_once(as_of="9999-12-31T00:00:00+00:00")["status"] == "needs_attention"

    feedback = FeedbackStore(database).list(brand_id=brand["id"])
    assert {item["component"] for item in feedback} == {"beehiiv.posts.sync"}
    assert all(item["occurrence_count"] == 2 for item in feedback)
    persisted = " ".join(
        str(item[field]) for item in feedback
        for field in ("details", "actual_behavior", "reproduction", "workaround")
    )
    assert "connector-secret" not in persisted
    assert all(item["status"] == "open" for item in feedback)


def test_nonterminal_governance_feedback_is_deduplicated_and_secret_safe(tmp_path):
    database, brand, account = setup_database(tmp_path)
    unsafe = "Bearer secret-value should-not-persist"
    for _ in range(2):
        report_approval_dead_end(
            brand_id=brand["id"], resource_id=unsafe,
            resource_type="newsletter", operation="export-draft",
        )
        report_missing_permission(
            brand_id=brand["id"], connector_account_id=account["id"],
            provider="x", operation="read", missing_scopes=["tweet.read", "users.read"],
        )
        report_stale_metric(
            brand_id=brand["id"], metric="engagement-rate",
            evidence_window="seven-days", related_ids=[unsafe],
        )
        report_successful_workaround(
            brand_id=brand["id"], component="execution-handoff",
            workaround_code="receipt-recovery", related_ids=[unsafe],
        )

    rows = FeedbackStore(database).list(brand_id=brand["id"])
    assert len(rows) == 4
    assert all(item["occurrence_count"] == 2 for item in rows)
    assert {item["severity"] for item in rows} == {"low", "medium", "high"}
    assert unsafe not in repr(rows)
    assert all(
        reference == account["id"] or reference.startswith("ref:sha256:")
        for item in rows for reference in item["related_ids"]
    )
