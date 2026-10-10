from __future__ import annotations

import json

import pytest

from brandman.ops_cli import main
from brandman.supervisor_proof import (
    run_supervisor_proof, verify_supervisor_evidence, verify_supervisor_report,
)


def test_supervisor_proof_covers_named_launch_invariants_offline(tmp_path):
    database = tmp_path / "supervisor-proof.db"
    report_file = tmp_path / "supervisor-proof.json"

    report = run_supervisor_proof(database, report_file)

    assert report["status"] == "passed"
    assert report["all_checks_passed"] is True
    assert set(report["checks"]) == {
        "scheduler_heartbeat", "restart_safe_idempotency", "lease_expiry_recovery",
        "retry_exhaustion", "feedback_dedup_recurrence",
        "post_receipt_measurement", "database_integrity", "provider_isolation",
    }
    assert all(check["passed"] for check in report["checks"].values())
    assert report["checks"]["provider_isolation"] == {
        "passed": True, "network_transports_constructed": 0,
        "provider_requests": 0, "fixture_only": True,
    }
    assert report["checks"]["post_receipt_measurement"]["unique_measurement_records"] == 1
    assert report["checks"]["feedback_dedup_recurrence"]["occurrence_count"] == 2
    persisted = json.loads(report_file.read_text())
    assert verify_supervisor_report(persisted)
    verified = verify_supervisor_evidence(report_file, database)
    assert verified["valid"] is True
    assert verified["database_digest_valid"] is True
    assert oct(database.stat().st_mode & 0o777) == "0o600"
    assert oct(report_file.stat().st_mode & 0o777) == "0o600"


def test_supervisor_proof_is_scratch_only_and_report_is_create_only(tmp_path):
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"user data")
    with pytest.raises(ValueError, match="new scratch database"):
        run_supervisor_proof(existing)

    database = tmp_path / "new.db"
    report_file = tmp_path / "existing.json"
    report_file.write_text("do not replace")
    with pytest.raises(FileExistsError):
        run_supervisor_proof(database, report_file)
    assert not database.exists()
    assert report_file.read_text() == "do not replace"


def test_supervisor_report_detects_tampering(tmp_path):
    report = run_supervisor_proof(tmp_path / "proof.db")
    assert verify_supervisor_report(report)
    report["checks"]["provider_isolation"]["provider_requests"] = 1
    assert not verify_supervisor_report(report)


def test_supervisor_evidence_detects_database_tampering(tmp_path):
    database = tmp_path / "proof.db"
    report_file = tmp_path / "proof.json"
    run_supervisor_proof(database, report_file)
    with database.open("ab") as output:
        output.write(b"tampered")
    result = verify_supervisor_evidence(report_file, database)
    assert result["report_digest_valid"] is True
    assert result["database_digest_valid"] is False
    assert result["valid"] is False


def test_supervisor_proof_cli_emits_machine_readable_result(tmp_path, capsys):
    database = tmp_path / "cli-proof.db"
    report_file = tmp_path / "cli-proof.json"
    main([
        "--database", str(database), "supervisor-proof",
        "--report", str(report_file),
    ])
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "passed"
    assert output["report_file"] == str(report_file)
    assert verify_supervisor_report(json.loads(report_file.read_text()))
    main([
        "--database", str(database), "supervisor-proof-verify", str(report_file),
    ])
    verified = json.loads(capsys.readouterr().out)
    assert verified["valid"] is True


def test_supervisor_proof_cli_emits_structured_tamper_diagnosis(tmp_path, capsys):
    database = tmp_path / "cli-proof.db"
    report_file = tmp_path / "cli-proof.json"
    main([
        "--database", str(database), "supervisor-proof",
        "--report", str(report_file),
    ])
    capsys.readouterr()
    database.write_bytes(database.read_bytes() + b"tamper")

    with pytest.raises(SystemExit) as error:
        main([
            "--database", str(database), "supervisor-proof-verify", str(report_file),
        ])
    assert error.value.code == 1
    diagnosis = json.loads(capsys.readouterr().out)
    assert diagnosis["valid"] is False
    assert diagnosis["report_digest_valid"] is True
    assert diagnosis["database_exists"] is True
    assert diagnosis["database_digest_valid"] is False
