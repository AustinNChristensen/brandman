from __future__ import annotations

import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

import pytest

from brandman import store
from brandman.launchd_supervisor import (
    _unload_temporary_job, build_launchd_plist, install, status, uninstall, validate_launchd_plist,
    verify_target_host_proof, write_plist,
)
from brandman.worker_cli import main as worker_main


def _database(tmp_path: Path, monkeypatch, profile: str = "test") -> Path:
    database = tmp_path / "brand-os.db"
    store.DATA_PATH = database
    monkeypatch.setenv("BRANDMAN_DATABASE_PROFILE", profile)
    store.init_db(profile=profile)
    return database


def _plist(tmp_path: Path, database: Path, *, profile: str = "test") -> dict:
    return build_launchd_plist(
        label="com.brandos.worker.test-proof", database=database,
        profile=profile, project_root=Path(__file__).parents[1],
        uv=shutil.which("uv"), stdout_log=tmp_path / "stdout.log",
        stderr_log=tmp_path / "stderr.log", interval_seconds=300,
        max_jobs=17, max_decisions=19,
    )


def test_plist_is_secret_free_explicit_bounded_and_create_only(tmp_path, monkeypatch):
    database = _database(tmp_path, monkeypatch)
    payload = _plist(tmp_path, database)
    summary = validate_launchd_plist(payload)
    assert summary == {
        "label": "com.brandos.worker.test-proof",
        "database": str(database.resolve()), "profile": "test",
        "interval_seconds": 300, "max_jobs": 17, "max_decisions": 19,
        "worker_mode": "assisted_secretless", "contains_embedded_secrets": False,
    }
    assert payload["KeepAlive"] is False
    assert payload["ProgramArguments"][1:] == [
        "run", "--project", str(Path(__file__).parents[1].resolve()),
        "--no-sync", "python", "-m", "brandman.worker_cli",
    ]
    assert set(payload["EnvironmentVariables"]) == {
        "BRANDMAN_DB", "BRANDMAN_DATABASE_PROFILE", "BRANDMAN_WORKER_MODE",
        "BRANDMAN_WORKER_MAX_JOBS", "BRANDMAN_SCHEDULER_MAX_DECISIONS",
        "BRANDMAN_SUPERVISOR_MANAGED",
    }
    destination = tmp_path / "worker.plist"
    result = write_plist(payload, destination)
    assert result["mode"] == "0600"
    assert destination.stat().st_mode & 0o777 == 0o600
    with destination.open("rb") as stream:
        assert plistlib.load(stream) == payload
    with pytest.raises(FileExistsError):
        write_plist(payload, destination)


def test_validation_rejects_secret_or_unbounded_or_profile_drift(tmp_path, monkeypatch):
    database = _database(tmp_path, monkeypatch)
    payload = _plist(tmp_path, database)
    payload["EnvironmentVariables"]["BRANDMAN_CREDENTIAL_MASTER_KEY"] = "secret"
    with pytest.raises(ValueError, match="unexpected"):
        validate_launchd_plist(payload)
    payload = _plist(tmp_path, database)
    payload["EnvironmentVariables"]["BRANDMAN_WORKER_MAX_JOBS"] = "1001"
    with pytest.raises(ValueError, match="bounds"):
        validate_launchd_plist(payload)
    payload = _plist(tmp_path, database)
    payload["EnvironmentVariables"]["BRANDMAN_DATABASE_PROFILE"] = "operating"
    with pytest.raises(ValueError, match="profile"):
        validate_launchd_plist(payload)


def test_forced_secretless_worker_ignores_inherited_master_key(
    tmp_path, monkeypatch, capsys,
):
    database = _database(tmp_path, monkeypatch)
    monkeypatch.setenv("BRANDMAN_DB", str(database))
    monkeypatch.setenv("BRANDMAN_WORKER_MODE", "assisted_secretless")
    monkeypatch.setenv("BRANDMAN_CREDENTIAL_MASTER_KEY", "would-enable-native-auto-mode")
    monkeypatch.setenv("BRANDMAN_WORKER_MAX_JOBS", "2")
    monkeypatch.setenv("BRANDMAN_SCHEDULER_MAX_DECISIONS", "2")
    worker_main()
    result = json.loads(capsys.readouterr().out)
    assert result["configuration"]["mode"] == "assisted_secretless"
    assert result["supervision"]["database"] == str(database.resolve())
    assert result["supervision"]["database_profile"] == "test"
    assert result["supervision"]["worker_mode"] == "assisted_secretless"


def test_native_mode_requires_key(monkeypatch):
    monkeypatch.setenv("BRANDMAN_WORKER_MODE", "native_api")
    monkeypatch.delenv("BRANDMAN_CREDENTIAL_MASTER_KEY", raising=False)
    with pytest.raises(SystemExit, match="requires"):
        worker_main()


def test_install_status_uninstall_are_idempotent_and_managed(tmp_path, monkeypatch):
    database = _database(tmp_path, monkeypatch, profile="operating")
    payload = _plist(tmp_path, database, profile="operating")
    source = tmp_path / "source.plist"
    write_plist(payload, source)
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    loaded = set()

    def fake_launchctl(*arguments, check=True):
        command = arguments[0]
        label = payload["Label"]
        if command == "print":
            return subprocess.CompletedProcess(arguments, 0 if label in loaded else 113, "", "")
        if command == "bootstrap":
            loaded.add(label)
        elif command == "bootout":
            loaded.discard(label)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr("brandman.launchd_supervisor._launchctl", fake_launchctl)
    first = install(source)
    assert first["status"] == "installed"
    assert first["installed_plist"].startswith(str(home))
    assert install(source)["status"] == "already_installed"
    state = status(payload["Label"])
    assert state["installed"] is True and state["loaded"] is True
    assert uninstall(payload["Label"])["status"] == "uninstalled"
    assert uninstall(payload["Label"])["status"] == "already_absent"


def test_uninstall_preserves_configuration_when_bootout_fails(tmp_path, monkeypatch):
    database = _database(tmp_path, monkeypatch, profile="operating")
    payload = _plist(tmp_path, database, profile="operating")
    source = tmp_path / "source.plist"; write_plist(payload, source)
    home = tmp_path / "home"; home.mkdir(); monkeypatch.setenv("HOME", str(home))
    loaded = set()

    def fake_launchctl(*arguments, check=True):
        label = payload["Label"]
        if arguments[0] == "print":
            return subprocess.CompletedProcess(arguments, 0 if label in loaded else 113, "", "")
        if arguments[0] == "bootstrap":
            loaded.add(label); return subprocess.CompletedProcess(arguments, 0, "", "")
        if arguments[0] == "bootout":
            raise subprocess.CalledProcessError(5, arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr("brandman.launchd_supervisor._launchctl", fake_launchctl)
    installed = Path(install(source)["installed_plist"])
    with pytest.raises(subprocess.CalledProcessError):
        uninstall(payload["Label"])
    assert installed.is_file()


def test_temporary_cleanup_preserves_private_recovery_plist_after_both_failures(
    tmp_path, monkeypatch,
):
    database = _database(tmp_path, monkeypatch, profile="proof")
    payload = _plist(tmp_path, database, profile="proof")
    plist = tmp_path / "temporary.plist"; write_plist(payload, plist)
    recovery = tmp_path / "recovery.plist"
    calls = []

    def always_loaded(*arguments, check=True):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr("brandman.launchd_supervisor._launchctl", always_loaded)
    with pytest.raises(ValueError, match="management plist preserved") as failure:
        _unload_temporary_job(payload["Label"], plist, recovery)
    assert recovery.read_bytes() == plist.read_bytes()
    assert recovery.stat().st_mode & 0o777 == 0o600
    assert sum(call[0] == "bootout" for call in calls) == 2
    assert f"gui/{__import__('os').getuid()}/{payload['Label']}" in str(failure.value)


def test_cleanup_collision_preserves_exact_plist_at_new_private_path(
    tmp_path, monkeypatch,
):
    database = _database(tmp_path, monkeypatch, profile="proof")
    payload = _plist(tmp_path, database, profile="proof")
    plist = tmp_path / "temporary.plist"; write_plist(payload, plist)
    recovery = tmp_path / "recovery.plist"
    recovery.write_bytes(b"unrelated existing bytes"); recovery.chmod(0o600)
    monkeypatch.setattr(
        "brandman.launchd_supervisor._launchctl",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    with pytest.raises(ValueError, match="management plist preserved") as caught:
        _unload_temporary_job(payload["Label"], plist, recovery)
    alternate = caught.value.recovery_path
    assert recovery.read_bytes() == b"unrelated existing bytes"
    assert alternate != recovery
    assert alternate.read_bytes() == plist.read_bytes()
    assert alternate.stat().st_mode & 0o777 == 0o600


def test_proof_verifier_binds_report_and_database(tmp_path, monkeypatch):
    database = _database(tmp_path, monkeypatch, profile="proof")
    database_digest = __import__("hashlib").sha256(database.read_bytes()).hexdigest()
    invocations = [
        {"pid": 101, "invocation_id": "00000000-0000-4000-8000-000000000001", "database": str(database.resolve()),
         "database_profile": "proof", "worker_mode": "assisted_secretless"},
        {"pid": 102, "invocation_id": "00000000-0000-4000-8000-000000000002", "database": str(database.resolve()),
         "database_profile": "proof", "worker_mode": "assisted_secretless"},
    ]
    label = "com.brandos.worker.proof-0123456789abcdef"
    evidence = {
        "schema": "brand-os.launchd-target-host-proof/v2", "created_at": "now",
        "host": {"platform": sys.platform, "uid": __import__("os").getuid(), "supervisor": "launchd"},
        "configuration": {
            "label": label, "database": str(database.resolve()), "profile": "proof",
            "interval_seconds": 86400, "max_jobs": 25, "max_decisions": 25,
            "worker_mode": "assisted_secretless", "contains_embedded_secrets": False,
        }, "invocations": invocations,
        "database_sha256": database_digest,
        "checks": {
            "launchd_bootstrap": True, "bounded_worker_cycles": True,
            "distinct_process_restarts": True,
            "explicit_database_and_profile": True, "forced_secretless_mode": True,
            "database_integrity": True, "provider_calls": 0,
            "launchd_cleanup": True, "persistent_launch_agent_installed": False,
            "private_artifact_permissions": True,
        },
    }
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    evidence["content_consistency_sha256"] = __import__("hashlib").sha256(canonical).hexdigest()
    report = tmp_path / "report.json"
    report.write_text(json.dumps(evidence), encoding="utf-8")
    report.chmod(0o600); database.chmod(0o600)
    expected = __import__("hashlib").sha256(report.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "brandman.launchd_supervisor._launchctl",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 113, "", ""),
    )
    assert verify_target_host_proof(
        report, database, expected_report_sha256=expected,
    )["valid"] is True
    with database.open("ab") as stream:
        stream.write(b"tamper")
    assert verify_target_host_proof(
        report, database, expected_report_sha256=expected,
    )["valid"] is False


def test_recomputed_inner_digest_cannot_authenticate_forged_proof(tmp_path, monkeypatch):
    database = _database(tmp_path, monkeypatch, profile="proof"); database.chmod(0o600)
    label = "com.brandos.worker.proof-fedcba9876543210"
    invocations = [
        {"pid": 111, "invocation_id": "00000000-0000-4000-8000-000000000001",
         "database": str(database.resolve()), "database_profile": "proof",
         "worker_mode": "assisted_secretless"},
        {"pid": 222, "invocation_id": "00000000-0000-4000-8000-000000000002",
         "database": str(database.resolve()), "database_profile": "proof",
         "worker_mode": "assisted_secretless"},
    ]
    evidence = {
        "schema": "brand-os.launchd-target-host-proof/v2", "created_at": "now",
        "host": {"platform": sys.platform, "uid": __import__("os").getuid(), "supervisor": "launchd"},
        "configuration": {"label": label, "database": str(database.resolve()),
            "profile": "proof", "interval_seconds": 86400, "max_jobs": 25,
            "max_decisions": 25, "worker_mode": "assisted_secretless",
            "contains_embedded_secrets": False},
        "invocations": invocations,
        "database_sha256": __import__("hashlib").sha256(database.read_bytes()).hexdigest(),
        "checks": {"launchd_bootstrap": True, "bounded_worker_cycles": True,
            "distinct_process_restarts": True, "explicit_database_and_profile": True,
            "forced_secretless_mode": True, "database_integrity": True,
            "provider_calls": 0, "launchd_cleanup": True,
            "persistent_launch_agent_installed": False,
            "private_artifact_permissions": True},
    }
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    evidence["content_consistency_sha256"] = __import__("hashlib").sha256(canonical).hexdigest()
    report = tmp_path / "evidence.json"; report.write_text(json.dumps(evidence)); report.chmod(0o600)
    trusted_digest = __import__("hashlib").sha256(report.read_bytes()).hexdigest()
    evidence["invocations"][0]["pid"] = 999
    unsigned = dict(evidence); unsigned.pop("content_consistency_sha256")
    evidence["content_consistency_sha256"] = __import__("hashlib").sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    report.write_text(json.dumps(evidence)); report.chmod(0o600)
    monkeypatch.setattr(
        "brandman.launchd_supervisor._launchctl",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 113, "", ""),
    )
    result = verify_target_host_proof(
        report, database, expected_report_sha256=trusted_digest,
    )
    assert result["valid"] is False
    assert result["actual_report_sha256"] != trusted_digest
