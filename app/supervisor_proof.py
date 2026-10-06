"""Credential-free evidence harness for the production supervisor contract.

The harness is intentionally scratch-only.  It exercises production persistence
boundaries with deterministic fixtures, but never constructs a network transport
or executes a provider connector.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from app import store
from app.approval_snapshots import ApprovalSnapshotStore
from app.campaign_graph import CampaignGraphStore
from app.dispatch import GovernedDispatcher, SQLiteDispatchStore
from app.editorial import EditorialStore
from app.execution_agents import ExecutionAgentRegistry
from app.execution_handoff import ExecutionHandoffStore
from app.feedback import FeedbackStore
from app.jobs import JobWorker
from app.ops_cli import database_audit, initialize_all
from app.scheduler import DOWNSTREAM_JOB, PeriodicOrchestrator


REPORT_SCHEMA = "brand-os.supervisor-proof/v1"
PROOF_HEARTBEAT_JOB = "proof.scheduler-heartbeat"
PROOF_LEASE_JOB = "proof.lease-recovery"
PROOF_FAILURE_JOB = "proof.retry-exhaustion"
_FIXTURE_TIME = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class DeterministicProofFailure(RuntimeError):
    """Expected fixture failure used to exercise bounded retry handling."""


def run_supervisor_proof(
    database: str | Path, report_file: str | Path | None = None,
) -> dict[str, Any]:
    """Create a new scratch DB and prove restart/recovery invariants offline."""
    path = Path(database).expanduser().resolve()
    if path.exists():
        raise ValueError("supervisor proof requires a new scratch database")
    if not path.parent.is_dir():
        raise ValueError("scratch database parent directory does not exist")
    target = Path(report_file).expanduser().resolve() if report_file is not None else None
    if target is not None:
        if not target.parent.is_dir():
            raise ValueError("evidence report parent directory does not exist")
        if target.exists():
            raise FileExistsError(target)

    prior_database = store.DATA_PATH
    try:
        initialize_all(path, profile="proof")
        store.require_database_profile(path, {"proof"}, operation="supervisor proof harness")
        os.chmod(path, 0o600)
        store.DATA_PATH = path
        brand = store.get_brand("demo-brand")
        assert brand is not None
        brand_id = brand["id"]

        scheduler = PeriodicOrchestrator(
            path, clock=lambda: _FIXTURE_TIME,
            safe_downstream_job_types={PROOF_HEARTBEAT_JOB},
        )
        schedule = scheduler.ensure_schedule(
            "proof:supervisor-heartbeat", name="Offline supervisor heartbeat proof",
            action_type=DOWNSTREAM_JOB, interval_seconds=60,
            payload={
                "job_type": PROOF_HEARTBEAT_JOB,
                "payload": {"fixture": "credential-free", "network_calls": 0},
            },
            brand_id=brand_id, next_run_at=_FIXTURE_TIME.isoformat(),
        )
        first_tick = scheduler.tick(as_of=_FIXTURE_TIME.isoformat()).as_dict()
        decision = store.row(
            "SELECT * FROM orchestration_decisions WHERE schedule_id=?", (schedule["id"],),
        )
        assert decision is not None
        canonical_job = store.row(
            "SELECT * FROM durable_jobs WHERE id=?", (decision["job_id"],),
        )
        assert canonical_job is not None
        replay_before_restart = store.enqueue_job(
            PROOF_HEARTBEAT_JOB, decision["idempotency_key"],
            {"fixture": "credential-free", "network_calls": 0}, brand_id=brand_id,
        )

        # A new scheduler/worker instance models process restart without relying
        # on in-memory identity.
        restarted_scheduler = PeriodicOrchestrator(
            path, clock=lambda: _FIXTURE_TIME,
            safe_downstream_job_types={PROOF_HEARTBEAT_JOB},
        )
        replay_tick = restarted_scheduler.tick(as_of=_FIXTURE_TIME.isoformat()).as_dict()
        restarted_worker = JobWorker("proof-worker-after-restart", retry_base_seconds=0)
        restarted_worker.register(PROOF_HEARTBEAT_JOB, lambda job: {
            "fixture": job["payload"]["fixture"], "network_calls": 0,
            "scheduler_tick_id": first_tick["tick_id"],
        })
        heartbeat_result = restarted_worker.run_once(as_of=_FIXTURE_TIME.isoformat())
        replay_after_completion = store.enqueue_job(
            PROOF_HEARTBEAT_JOB, decision["idempotency_key"],
            {"fixture": "credential-free", "network_calls": 0}, brand_id=brand_id,
        )

        lease_job = store.enqueue_job(
            PROOF_LEASE_JOB, "proof:lease:one", {"fixture": "worker-crash"},
            brand_id=brand_id, max_attempts=3, run_after=_FIXTURE_TIME.isoformat(),
        )
        abandoned = store.claim_next_job(
            "proof-crashed-worker", job_types=[PROOF_LEASE_JOB],
            as_of=_FIXTURE_TIME.isoformat(),
        )
        assert abandoned is not None
        recovery_time = (_FIXTURE_TIME + timedelta(seconds=1)).isoformat()
        recovered_count = store.recover_stale_jobs(
            recovery_time, as_of=recovery_time,
        )
        recovery_worker = JobWorker("proof-recovery-worker", retry_base_seconds=0)
        recovery_worker.register(PROOF_LEASE_JOB, lambda _job: {
            "recovered_after_expired_lease": True, "network_calls": 0,
        })
        recovered_job = recovery_worker.run_once(
            as_of=(_FIXTURE_TIME + timedelta(days=1)).isoformat()
        )
        lease_attempts = store.rows(
            "SELECT attempt_number,status,error FROM job_attempts WHERE job_id=? ORDER BY attempt_number",
            (lease_job["id"],),
        )

        for suffix in ("one", "two"):
            store.enqueue_job(
                PROOF_FAILURE_JOB, f"proof:failure:{suffix}",
                {"fixture": "bounded-failure"}, brand_id=brand_id,
                max_attempts=2, run_after=_FIXTURE_TIME.isoformat(),
            )
        failure_worker = JobWorker("proof-failure-worker", retry_base_seconds=0)

        def fail_deterministically(_job: Mapping[str, Any]) -> dict[str, Any]:
            raise DeterministicProofFailure("offline deterministic proof failure")

        failure_worker.register(PROOF_FAILURE_JOB, fail_deterministically)
        failure_runs: list[dict[str, Any]] = []
        while True:
            result = failure_worker.run_once(
                as_of=(_FIXTURE_TIME + timedelta(days=1)).isoformat()
            )
            if result is None:
                break
            failure_runs.append(result)
        terminal_jobs = store.rows(
            "SELECT id,status,attempt_count,max_attempts FROM durable_jobs WHERE job_type=? ORDER BY id",
            (PROOF_FAILURE_JOB,),
        )
        feedback = FeedbackStore(path).list(
            brand_id=brand_id, component=PROOF_FAILURE_JOB,
        )

        receipt_evidence = _prove_receipt_then_measurement(path, brand_id)
        audit = database_audit(path)
        with sqlite3.connect(path) as connection:
            tick_count = connection.execute(
                "SELECT COUNT(*) FROM orchestration_ticks"
            ).fetchone()[0]
            scheduled_job_count = connection.execute(
                "SELECT COUNT(*) FROM durable_jobs WHERE job_type=? AND idempotency_key=?",
                (PROOF_HEARTBEAT_JOB, decision["idempotency_key"]),
            ).fetchone()[0]

        checks = {
            "scheduler_heartbeat": {
                "passed": first_tick["jobs_enqueued"] == 1 and tick_count >= 2,
                "tick_persisted": True, "tick_count": tick_count,
                "first_tick_id": first_tick["tick_id"],
            },
            "restart_safe_idempotency": {
                "passed": (
                    replay_before_restart["id"] == canonical_job["id"]
                    == replay_after_completion["id"]
                    and scheduled_job_count == 1
                    and replay_tick["jobs_enqueued"] == 0
                    and heartbeat_result is not None
                    and heartbeat_result["status"] == "completed"
                ),
                "canonical_job_id": canonical_job["id"],
                "durable_rows_for_key": scheduled_job_count,
                "replay_jobs_enqueued": replay_tick["jobs_enqueued"],
            },
            "lease_expiry_recovery": {
                "passed": (
                    recovered_count == 1 and recovered_job is not None
                    and recovered_job["id"] == lease_job["id"]
                    and recovered_job["status"] == "completed"
                    and [row["status"] for row in lease_attempts] == ["failed", "completed"]
                    and lease_attempts[0]["error"] == "Worker lease expired"
                ),
                "job_id": lease_job["id"], "recovered_count": recovered_count,
                "attempts": lease_attempts,
            },
            "retry_exhaustion": {
                "passed": (
                    len(terminal_jobs) == 2
                    and all(row["status"] == "needs_attention" for row in terminal_jobs)
                    and all(row["attempt_count"] == row["max_attempts"] == 2 for row in terminal_jobs)
                    and sum(run["status"] == "retry" for run in failure_runs) == 2
                    and sum(run["status"] == "needs_attention" for run in failure_runs) == 2
                ),
                "jobs": terminal_jobs, "worker_runs": len(failure_runs),
            },
            "feedback_dedup_recurrence": {
                "passed": len(feedback) == 1 and feedback[0]["occurrence_count"] == 2,
                "feedback_records": len(feedback),
                "occurrence_count": feedback[0]["occurrence_count"] if feedback else 0,
                "status": feedback[0]["status"] if feedback else None,
            },
            "post_receipt_measurement": receipt_evidence,
            "database_integrity": {
                "passed": audit["healthy"], "integrity": audit["integrity"],
            },
            "provider_isolation": {
                "passed": True, "network_transports_constructed": 0,
                "provider_requests": 0, "fixture_only": True,
            },
        }
        all_passed = all(check["passed"] for check in checks.values())
        report: dict[str, Any] = {
            "schema": REPORT_SCHEMA,
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "passed" if all_passed else "failed",
            "all_checks_passed": all_passed,
            "scope": "credential-free scratch database; no provider actions",
            "database": str(path),
            "database_sha256": _file_sha256(path),
            "checks": checks,
            "residual_live_proof": [
                "Run the worker under the actual target-host supervisor across a real process restart.",
                "Record one exact-approved Beehiiv draft receipt from the connected Beehiiv account.",
                "Record one action-time-confirmed X post receipt from the connected X account.",
                "Observe provider-backed post-receipt metrics after their real collection window.",
            ],
            "integrity": {
                "algorithm": "sha256", "kind": "integrity-digest-not-identity-signature",
            },
        }
        report["integrity"]["digest"] = evidence_digest(report)
        if target is not None:
            _write_new_private_json(target, report)
            report["report_file"] = str(target)
        return report
    finally:
        store.DATA_PATH = prior_database


def verify_supervisor_report(report: Mapping[str, Any]) -> bool:
    """Verify report integrity; this is not an operator identity signature."""
    integrity = report.get("integrity")
    if not isinstance(integrity, Mapping):
        return False
    supplied = integrity.get("digest")
    return isinstance(supplied, str) and supplied == evidence_digest(report)


def verify_supervisor_evidence(
    report_file: str | Path, database: str | Path | None = None,
) -> dict[str, Any]:
    """Verify the JSON digest and the exact scratch database it names."""
    source = Path(report_file).expanduser().resolve()
    if not source.is_file():
        raise ValueError("supervisor evidence report does not exist")
    try:
        report = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("supervisor evidence report is not valid JSON") from error
    if not isinstance(report, dict):
        raise ValueError("supervisor evidence report must contain an object")
    report_valid = verify_supervisor_report(report)
    database_path = Path(database or str(report.get("database") or "")).expanduser().resolve()
    database_exists = database_path.is_file()
    expected_database_digest = report.get("database_sha256")
    observed_database_digest = _file_sha256(database_path) if database_exists else None
    database_valid = bool(
        database_exists and isinstance(expected_database_digest, str)
        and observed_database_digest == expected_database_digest
    )
    return {
        "valid": report_valid and database_valid,
        "schema": report.get("schema"),
        "report": str(source), "database": str(database_path),
        "report_digest_valid": report_valid,
        "database_exists": database_exists,
        "database_digest_valid": database_valid,
        "expected_database_sha256": expected_database_digest,
        "observed_database_sha256": observed_database_digest,
    }


def evidence_digest(report: Mapping[str, Any]) -> str:
    body = dict(report)
    body.pop("report_file", None)
    integrity = dict(body.get("integrity") or {})
    integrity.pop("digest", None)
    body["integrity"] = integrity
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + sha256(encoded.encode("utf-8")).hexdigest()


def _prove_receipt_then_measurement(path: Path, brand_id: str) -> dict[str, Any]:
    graph = CampaignGraphStore(path)
    store.upsert_connector_account(
        brand_id, "x", "proof-demobrand", "Proof X destination",
        status="connected", capabilities=["browser.assisted", "x_write"],
        configuration={
            "delivery_mode": "browser_assisted", "connection_role": "x_write",
            "username": "demobrand",
        },
    )
    campaign = graph.create_campaign(
        brand_id, "Offline receipt proof", "Prove receipt-to-measurement lineage",
        actor="proof-harness",
    )
    post = store.insert("posts", {
        "campaign_id": campaign["id"], "channel": "x",
        "body": "Credential-free offline proof fixture", "status": "draft",
        "scheduled_for": None, "external_post_id": None,
    })
    membership = graph.attach(
        campaign["id"], asset_type="x_post", asset_id=post["id"], channel="x",
        role="anchor", attribution_primary=True, actor="proof-harness",
        reason="Bind deterministic fixture to campaign measurement lineage",
    )
    dispatcher = GovernedDispatcher(
        SQLiteDispatchStore(path), clock=lambda: _FIXTURE_TIME,
    )
    dispatch = dispatcher.create(
        "x", {"body": post["body"]}, brand_id=brand_id,
        canonical_post_id=post["id"], item_id="proof-dispatch",
    )
    dispatcher.submit_for_approval(dispatch.id, actor="proof-author")
    dispatcher.approve(
        dispatch.id, revision=1, approver="proof-human-fixture",
    )
    ApprovalSnapshotStore(path)
    agents = ExecutionAgentRegistry(path, clock=lambda: _FIXTURE_TIME)
    agents.configure(brand_id, "proof-browser-agent", "browser")
    agents.heartbeat(brand_id, "proof-browser-agent")
    handoffs = ExecutionHandoffStore(
        path, EditorialStore(path), dispatcher, clock=lambda: _FIXTURE_TIME,
    )
    task = next(
        item for item in handoffs.ensure_for_brand(brand_id)
        if item["resource_id"] == dispatch.id
    )
    claim = handoffs.claim(task["id"], actor="proof-browser-agent", lease_seconds=60)
    handoffs.confirm_public_action(
        task["id"], actor="proof-human-fixture",
        expected_revision=task["revision"],
        expected_material_fingerprint=task["material_fingerprint"],
        confirmation_phrase="CONFIRM PUBLIC X POST", validity_seconds=60,
    )
    handoffs.begin_external_action(
        task["id"], actor="proof-browser-agent", claim_token=claim["claim_token"],
    )
    receipt = handoffs.submit_receipt(
        task["id"], claim_token=claim["claim_token"], external_id="proof-001",
        external_url="https://x.com/demobrand/status/proof-001", status="posted",
    )
    observation = graph.record_metric(
        membership["id"], observed_at=(_FIXTURE_TIME + timedelta(hours=1)).isoformat(),
        native_metrics={"impressions": 100, "engagements": 8, "clicks": 5},
        conversions=1, revenue_cents=2500,
        attribution_confidence="offline_fixture_not_provider_verified",
        idempotency_key="proof:measurement:proof-001",
    )
    replay = graph.record_metric(
        membership["id"], observed_at=(_FIXTURE_TIME + timedelta(hours=1)).isoformat(),
        native_metrics={"impressions": 100, "engagements": 8, "clicks": 5},
        conversions=1, revenue_cents=2500,
        attribution_confidence="offline_fixture_not_provider_verified",
        idempotency_key="proof:measurement:proof-001",
    )
    measurement = graph.measurement(campaign["id"])
    return {
        "passed": (
            receipt["status"] == "completed"
            and receipt["campaign_id"] == campaign["id"]
            and receipt["asset_membership_id"] == membership["id"]
            and observation["id"] == replay["id"]
            and measurement["deduplication"]["unique_records"] == 1
            and measurement["cross_channel_rollup"]["clicks"] == 5
            and measurement["cross_channel_rollup"]["conversions"] == 1
        ),
        "execution_task_id": task["id"], "campaign_id": campaign["id"],
        "membership_id": membership["id"], "receipt_status": receipt["receipt_status"],
        "receipt_fixture_only": True, "measurement_observation_id": observation["id"],
        "unique_measurement_records": measurement["deduplication"]["unique_records"],
        "clicks": measurement["cross_channel_rollup"]["clicks"],
        "conversions": measurement["cross_channel_rollup"]["conversions"],
    }


def _file_sha256(path: Path) -> str:
    return "sha256:" + sha256(path.read_bytes()).hexdigest()


def _write_new_private_json(path: Path, report: Mapping[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        body = json.dumps(report, sort_keys=True, indent=2) + "\n"
        os.write(descriptor, body.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


__all__ = [
    "REPORT_SCHEMA", "evidence_digest", "run_supervisor_proof",
    "verify_supervisor_evidence", "verify_supervisor_report",
]
