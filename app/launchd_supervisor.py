"""Safe user-level launchd lifecycle and target-host restart evidence.

The managed job is deliberately a one-shot bounded worker. launchd starts it on
an interval and never overlaps an invocation. Browser-assisted deployments are
forced into secretless mode even if the user's launchd environment contains an
unrelated credential variable.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping
from uuid import UUID, uuid4

from app import store


LABEL_PREFIX = "com.brandos.worker."
MANAGED_MARKER = "brand-os-launchd/v1"
ALLOWED_PROFILES = frozenset({"operating", "development", "test", "proof"})
_LABEL = re.compile(r"^com\.brandos\.worker\.[a-z0-9][a-z0-9.-]{0,63}$")


def _resolved_file(value: str | Path, *, description: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"{description} does not exist")
    return path


def _validate_label(label: str) -> str:
    if not _LABEL.fullmatch(label):
        raise ValueError(f"label must match {LABEL_PREFIX}<lowercase-name>")
    return label


def build_launchd_plist(
    *, label: str, database: str | Path, profile: str,
    project_root: str | Path, uv: str | Path,
    stdout_log: str | Path, stderr_log: str | Path,
    interval_seconds: int = 1800, max_jobs: int = 100,
    max_decisions: int = 50, run_at_load: bool = True,
) -> dict[str, Any]:
    """Return a secret-free, bounded, non-overlapping launchd definition."""
    label = _validate_label(label)
    if profile not in ALLOWED_PROFILES:
        raise ValueError("profile must be operating, development, test, or proof")
    if not 60 <= interval_seconds <= 86400:
        raise ValueError("interval must be between 60 and 86400 seconds")
    if not 1 <= max_jobs <= 1000:
        raise ValueError("max jobs must be between 1 and 1000")
    if not 1 <= max_decisions <= 500:
        raise ValueError("max decisions must be between 1 and 500")
    database_path = _resolved_file(database, description="database")
    actual_profile = store.database_profile(database_path)
    if actual_profile != profile:
        raise ValueError(
            f"database is profiled {actual_profile}; refusing launchd profile {profile}"
        )
    root = Path(project_root).expanduser().resolve()
    if not (root / "app" / "worker_cli.py").is_file():
        raise ValueError("project root does not contain app/worker_cli.py")
    uv_path = _resolved_file(uv, description="uv executable")
    if not os.access(uv_path, os.X_OK):
        raise ValueError("uv executable is not executable")
    stdout = Path(stdout_log).expanduser().resolve()
    stderr = Path(stderr_log).expanduser().resolve()
    if not stdout.parent.is_dir() or not stderr.parent.is_dir():
        raise ValueError("log parent directories must already exist")
    result = {
        "Label": label,
        # macOS launchd resolves a virtualenv Python symlink to its base
        # interpreter and can thereby lose the virtualenv site-packages. uv is
        # a regular executable and recreates the checked-in project context
        # without syncing or mutating dependencies.
        "ProgramArguments": [
            str(uv_path), "run", "--project", str(root), "--no-sync",
            "python", "-m", "app.worker_cli",
        ],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "BRAND_OS_DB": str(database_path),
            "BRAND_OS_DATABASE_PROFILE": profile,
            "BRAND_OS_WORKER_MODE": "assisted_secretless",
            "BRAND_OS_WORKER_MAX_JOBS": str(max_jobs),
            "BRAND_OS_SCHEDULER_MAX_DECISIONS": str(max_decisions),
            "BRAND_OS_SUPERVISOR_MANAGED": MANAGED_MARKER,
        },
        "StartInterval": interval_seconds,
        "ThrottleInterval": 10,
        "KeepAlive": False,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": str(stdout),
        "StandardErrorPath": str(stderr),
    }
    # launchd treats some keys as presence-sensitive; omit a false RunAtLoad
    # instead of relying on every macOS release to interpret <false/> alike.
    if run_at_load:
        result["RunAtLoad"] = True
    return result


def validate_launchd_plist(
    payload: Mapping[str, Any], *, allowed_profiles: set[str] | None = None,
) -> dict[str, Any]:
    """Fail closed unless a plist is exactly a managed BrandOS worker."""
    label = _validate_label(str(payload.get("Label", "")))
    expected_keys = {
        "Label", "ProgramArguments", "WorkingDirectory", "EnvironmentVariables",
        "StartInterval", "ThrottleInterval", "KeepAlive", "ProcessType",
        "LowPriorityIO", "StandardOutPath", "StandardErrorPath",
    }
    if payload.get("RunAtLoad") is True:
        expected_keys.add("RunAtLoad")
    if set(payload) != expected_keys:
        raise ValueError("managed launchd definition contains missing or unexpected keys")
    arguments = payload.get("ProgramArguments")
    environment = payload.get("EnvironmentVariables")
    if not isinstance(arguments, list) or len(arguments) != 8:
        raise ValueError("managed ProgramArguments must contain exactly eight values")
    if arguments[1:3] != ["run", "--project"] or arguments[4:] != [
        "--no-sync", "python", "-m", "app.worker_cli",
    ]:
        raise ValueError("managed job must execute app.worker_cli directly")
    _resolved_file(arguments[0], description="uv executable")
    if not isinstance(environment, dict):
        raise ValueError("managed environment is missing")
    expected_environment = {
        "BRAND_OS_DB", "BRAND_OS_DATABASE_PROFILE", "BRAND_OS_WORKER_MODE",
        "BRAND_OS_WORKER_MAX_JOBS", "BRAND_OS_SCHEDULER_MAX_DECISIONS",
        "BRAND_OS_SUPERVISOR_MANAGED",
    }
    if set(environment) != expected_environment:
        raise ValueError("managed environment contains missing or unexpected values")
    if environment["BRAND_OS_WORKER_MODE"] != "assisted_secretless":
        raise ValueError("managed job must be forced into assisted_secretless mode")
    if environment["BRAND_OS_SUPERVISOR_MANAGED"] != MANAGED_MARKER:
        raise ValueError("managed marker is missing")
    profile = str(environment["BRAND_OS_DATABASE_PROFILE"])
    permitted = allowed_profiles if allowed_profiles is not None else set(ALLOWED_PROFILES)
    if profile not in permitted:
        raise ValueError("database profile is not permitted for this operation")
    database = _resolved_file(environment["BRAND_OS_DB"], description="database")
    if store.database_profile(database) != profile:
        raise ValueError("database profile does not match managed environment")
    max_jobs = int(environment["BRAND_OS_WORKER_MAX_JOBS"])
    max_decisions = int(environment["BRAND_OS_SCHEDULER_MAX_DECISIONS"])
    interval = int(payload.get("StartInterval", 0))
    if not (1 <= max_jobs <= 1000 and 1 <= max_decisions <= 500):
        raise ValueError("managed bounds are invalid")
    if not 60 <= interval <= 86400:
        raise ValueError("managed interval is invalid")
    if payload.get("ThrottleInterval") != 10:
        raise ValueError("managed restart throttle is invalid")
    if payload.get("KeepAlive") is not False:
        raise ValueError("managed job must be one-shot, not KeepAlive")
    if payload.get("ProcessType") != "Background":
        raise ValueError("managed job must use Background process type")
    root = Path(str(payload.get("WorkingDirectory", ""))).expanduser().resolve()
    if not (root / "app" / "worker_cli.py").is_file():
        raise ValueError("managed working directory is not a BrandOS checkout")
    if Path(arguments[3]).expanduser().resolve() != root:
        raise ValueError("uv project and managed working directory must match")
    return {
        "label": label, "database": str(database), "profile": profile,
        "interval_seconds": interval, "max_jobs": max_jobs,
        "max_decisions": max_decisions, "worker_mode": "assisted_secretless",
        "contains_embedded_secrets": False,
    }


def write_plist(payload: Mapping[str, Any], destination: str | Path) -> dict[str, Any]:
    target = Path(destination).expanduser().resolve()
    if not target.parent.is_dir():
        raise ValueError("plist parent directory does not exist")
    validate_launchd_plist(payload)
    data = plistlib.dumps(dict(payload), fmt=plistlib.FMT_XML, sort_keys=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {
        **validate_launchd_plist(payload), "plist": str(target),
        "plist_sha256": hashlib.sha256(data).hexdigest(), "mode": "0600",
    }


def load_plist(path: str | Path) -> dict[str, Any]:
    source = _resolved_file(path, description="plist")
    with source.open("rb") as input_file:
        payload = plistlib.load(input_file)
    if not isinstance(payload, dict):
        raise ValueError("plist must contain a dictionary")
    return payload


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments], text=True, capture_output=True,
        timeout=15, check=check,
    )


class TemporaryJobCleanupError(ValueError):
    def __init__(self, message: str, recovery_path: Path) -> None:
        super().__init__(message)
        self.recovery_path = recovery_path


def _preserve_recovery_plist(source: Path, destination: Path) -> Path:
    data = source.read_bytes()
    if destination.exists():
        if destination.read_bytes() == data:
            return destination
        destination = destination.with_name(
            f"{destination.name}.{uuid4().hex}.plist"
        )
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _preserve_recovery_plist(
            source, destination.with_name(f"{destination.name}.{uuid4().hex}.plist"),
        )
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
    return destination


def _unload_temporary_job(label: str, plist: Path, recovery: Path) -> None:
    """Try both launchd bootout forms; preserve management state on failure."""
    validate_launchd_plist(load_plist(plist), allowed_profiles={"proof"})
    attempts = (
        ("bootout", _domain(), str(plist)),
        ("bootout", f"{_domain()}/{label}"),
    )
    for arguments in attempts:
        _launchctl(*arguments, check=False)
        probe = _launchctl("print", f"{_domain()}/{label}", check=False)
        if probe.returncode != 0:
            return
    preserved = _preserve_recovery_plist(plist, recovery)
    raise TemporaryJobCleanupError(
        "temporary launchd job remains loaded; validated management plist preserved at "
        f"{preserved}. Run `/bin/launchctl bootout {_domain()}/{label}`, then verify "
        f"`/bin/launchctl print {_domain()}/{label}` reports the service absent; "
        "remove the recovery plist only after that verification",
        preserved,
    )


def _same_file_contents(first: Path, second: Path | None) -> bool:
    return bool(
        second is not None and first.is_file() and second.is_file()
        and _file_sha256(first) == _file_sha256(second)
    )


def install(plist: str | Path) -> dict[str, Any]:
    source = _resolved_file(plist, description="plist")
    payload = load_plist(source)
    summary = validate_launchd_plist(payload, allowed_profiles={"operating"})
    target_dir = Path.home() / "Library" / "LaunchAgents"
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = target_dir / f"{summary['label']}.plist"
    source_bytes = source.read_bytes()
    if target.exists():
        if target.read_bytes() != source_bytes:
            raise ValueError("a different launch agent already exists for this label")
        disposition = "already_installed"
    else:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(source_bytes)
        disposition = "installed"
    probe = _launchctl("print", f"{_domain()}/{summary['label']}", check=False)
    if probe.returncode != 0:
        try:
            _launchctl("bootstrap", _domain(), str(target))
        except Exception:
            if disposition == "installed":
                # A bootstrap result can be ambiguous. Never erase the only
                # management file for a job that actually became loaded.
                post_failure = _launchctl(
                    "print", f"{_domain()}/{summary['label']}", check=False,
                )
                if post_failure.returncode != 0:
                    target.unlink(missing_ok=True)
            raise
    return {**summary, "status": disposition, "installed_plist": str(target)}


def status(label: str) -> dict[str, Any]:
    label = _validate_label(label)
    target = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    installed = target.is_file()
    valid = False
    summary: dict[str, Any] = {}
    if installed:
        summary = validate_launchd_plist(load_plist(target), allowed_profiles={"operating"})
        valid = True
    probe = _launchctl("print", f"{_domain()}/{label}", check=False)
    return {
        **summary, "label": label, "installed": installed,
        "managed_configuration_valid": valid, "loaded": probe.returncode == 0,
    }


def uninstall(label: str) -> dict[str, Any]:
    label = _validate_label(label)
    target = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    if not target.exists():
        probe = _launchctl("print", f"{_domain()}/{label}", check=False)
        return {
            "label": label, "status": "already_absent",
            "loaded": probe.returncode == 0,
            "action": (
                "Inspect the loaded job manually; no managed plist exists to validate."
                if probe.returncode == 0 else None
            ),
        }
    summary = validate_launchd_plist(load_plist(target), allowed_profiles={"operating"})
    probe = _launchctl("print", f"{_domain()}/{label}", check=False)
    if probe.returncode == 0:
        _launchctl("bootout", _domain(), str(target))
        still_loaded = _launchctl("print", f"{_domain()}/{label}", check=False)
        if still_loaded.returncode == 0:
            raise ValueError("managed launch agent remained loaded; configuration was preserved")
    target.unlink()
    return {**summary, "status": "uninstalled", "loaded": False}


def _json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    results = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and isinstance(item.get("supervision"), dict):
            results.append(item)
    return results


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def target_host_proof(
    *, database: str | Path, report: str | Path, uv: str | Path,
    project_root: str | Path, timeout_seconds: float = 20,
) -> dict[str, Any]:
    """Run two real launchd invocations against a new proof DB, then clean up."""
    database_path = Path(database).expanduser().resolve()
    report_path = Path(report).expanduser().resolve()
    if database_path.exists() or report_path.exists():
        raise ValueError("target-host proof requires new database and report paths")
    if not database_path.parent.is_dir() or not report_path.parent.is_dir():
        raise ValueError("target-host proof parent directories must exist")
    store.DATA_PATH = database_path
    store.init_db(profile="proof")
    os.chmod(database_path, 0o600)
    proof_id = hashlib.sha256(
        f"{database_path}:{os.getpid()}:{time.time_ns()}".encode()
    ).hexdigest()[:16]
    label = f"{LABEL_PREFIX}proof-{proof_id}"
    temp_dir = Path(tempfile.mkdtemp(prefix="brand-os-launchd-proof-"))
    stdout = temp_dir / "stdout.jsonl"
    stderr = temp_dir / "stderr.log"
    plist = temp_dir / f"{label}.plist"
    recovery = report_path.with_name(f"{report_path.name}.{label}.recovery.plist")
    payload = build_launchd_plist(
        label=label, database=database_path, profile="proof",
        project_root=project_root, uv=uv,
        stdout_log=stdout, stderr_log=stderr, interval_seconds=86400,
        max_jobs=25, max_decisions=25, run_at_load=False,
    )
    write_plist(payload, plist)
    bootstrapped = False
    cleanup_attempted = False
    cleanup_recovery: Path | None = None
    invocations: list[dict[str, Any]] = []
    try:
        _launchctl("bootstrap", _domain(), str(plist))
        bootstrapped = True
        for expected in (1, 2):
            # ``launchctl kickstart`` can remain attached to a very short-lived
            # one-shot job on current macOS releases. The user-domain ``start``
            # verb is asynchronous and the receipt below is the completion
            # boundary we actually need to verify.
            _launchctl("start", label)
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                invocations = _json_lines(stdout)
                if len(invocations) >= expected:
                    break
                time.sleep(0.1)
            if len(invocations) < expected:
                raise ValueError(f"launchd invocation {expected} did not complete in time")
            if expected == 1:
                # launchd ignores manual starts inside the declared restart
                # throttle rather than queuing them. Respect that exact host
                # contract before requesting the separate restart process.
                time.sleep(10.1)
        summaries = [item["supervision"] for item in invocations[:2]]
        if len({item["pid"] for item in summaries}) != 2:
            raise ValueError("launchd proof did not observe distinct worker processes")
        if any(item["database"] != str(database_path) for item in summaries):
            raise ValueError("worker receipt database does not match proof database")
        if any(item["database_profile"] != "proof" for item in summaries):
            raise ValueError("worker receipt profile does not match proof profile")
        if any(item["worker_mode"] != "assisted_secretless" for item in summaries):
            raise ValueError("worker did not remain in forced secretless mode")
        cleanup_attempted = True
        try:
            _unload_temporary_job(label, plist, recovery)
        except TemporaryJobCleanupError as error:
            cleanup_recovery = error.recovery_path
            raise
        bootstrapped = False
        cleanup_probe = _launchctl(
            "print", f"{_domain()}/{label}", check=False,
        )
        if cleanup_probe.returncode == 0:
            raise ValueError("temporary launchd proof job remained loaded")
        audit = subprocess.run(
            ["/usr/bin/sqlite3", str(database_path), "pragma integrity_check;"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        evidence = {
            "schema": "brand-os.launchd-target-host-proof/v2",
            "created_at": datetime.now(UTC).isoformat(),
            "host": {"platform": sys.platform, "uid": os.getuid(), "supervisor": "launchd"},
            "configuration": validate_launchd_plist(payload),
            "invocations": summaries,
            "database_sha256": _file_sha256(database_path),
            "checks": {
                "launchd_bootstrap": True, "bounded_worker_cycles": len(summaries) == 2,
                "distinct_process_restarts": len({item["pid"] for item in summaries}) == 2,
                "explicit_database_and_profile": True, "forced_secretless_mode": True,
                "database_integrity": audit == "ok", "provider_calls": 0,
                "launchd_cleanup": True, "persistent_launch_agent_installed": False,
                "private_artifact_permissions": True,
            },
        }
        digest_input = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        # This embedded value detects accidental/internal inconsistency only. It
        # is not an authenticity claim because an editor could recompute it.
        evidence["content_consistency_sha256"] = hashlib.sha256(digest_input).hexdigest()
        descriptor = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(evidence, output, sort_keys=True)
            output.write("\n")
        return {
            "report": str(report_path), "database": str(database_path),
            "report_sha256": _file_sha256(report_path), "evidence": evidence,
            "authenticity": (
                "Retain report_sha256 independently and supply it to verification; "
                "the embedded content digest proves consistency only."
            ),
        }
    finally:
        try:
            if bootstrapped and not cleanup_attempted:
                cleanup_attempted = True
                try:
                    _unload_temporary_job(label, plist, recovery)
                    bootstrapped = False
                except TemporaryJobCleanupError as error:
                    cleanup_recovery = error.recovery_path
                    raise
        finally:
            # When unload cannot be proven, the validated plist has been copied
            # to the stable private recovery path before this temp tree is removed.
            if not bootstrapped or _same_file_contents(plist, cleanup_recovery):
                shutil.rmtree(temp_dir, ignore_errors=True)


def verify_target_host_proof(
    report: str | Path, database: str | Path, *, expected_report_sha256: str,
) -> dict[str, Any]:
    report_path = _resolved_file(report, description="report")
    database_path = _resolved_file(database, description="database")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_report_sha256):
        raise ValueError("expected report SHA-256 must be 64 lowercase hexadecimal characters")
    actual_report_sha256 = _file_sha256(report_path)
    evidence = json.loads(report_path.read_text(encoding="utf-8"))
    supplied = evidence.pop("content_consistency_sha256", None)
    calculated = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    checks = evidence.get("checks", {})
    invocations = evidence.get("invocations", [])
    configuration = evidence.get("configuration", {})
    label = configuration.get("label") if isinstance(configuration, dict) else None
    exact_configuration = bool(
        isinstance(label, str)
        and label.startswith(f"{LABEL_PREFIX}proof-")
        and _LABEL.fullmatch(label)
        and configuration == {
            "label": label, "database": str(database_path), "profile": "proof",
            "interval_seconds": 86400, "max_jobs": 25, "max_decisions": 25,
            "worker_mode": "assisted_secretless", "contains_embedded_secrets": False,
        }
    )
    exact_host = evidence.get("host") == {
        "platform": sys.platform, "uid": os.getuid(), "supervisor": "launchd",
    }
    exact_invocations = bool(
        isinstance(invocations, list) and len(invocations) == 2
        and all(isinstance(item, dict) and set(item) == {
            "pid", "invocation_id", "database", "database_profile", "worker_mode",
        } for item in invocations)
        and all(isinstance(item["pid"], int) and item["pid"] > 1 for item in invocations)
        and all(_valid_uuid(item["invocation_id"]) for item in invocations)
    )
    job_absent = bool(
        isinstance(label, str)
        and _launchctl("print", f"{_domain()}/{label}", check=False).returncode != 0
    )
    expected_checks = {
        "launchd_bootstrap": True, "bounded_worker_cycles": True,
        "distinct_process_restarts": True, "explicit_database_and_profile": True,
        "forced_secretless_mode": True, "database_integrity": True,
        "provider_calls": 0, "launchd_cleanup": True,
        "persistent_launch_agent_installed": False,
        "private_artifact_permissions": True,
    }
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
        actual_database_integrity = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]
    valid = bool(
        actual_report_sha256 == expected_report_sha256
        and supplied == calculated
        and evidence.get("schema") == "brand-os.launchd-target-host-proof/v2"
        and exact_configuration and exact_host and exact_invocations and job_absent
        and len({item.get("pid") for item in invocations}) == 2
        and len({item.get("invocation_id") for item in invocations}) == 2
        and all(item.get("database") == str(database_path) for item in invocations)
        and all(item.get("database_profile") == "proof" for item in invocations)
        and all(item.get("worker_mode") == "assisted_secretless" for item in invocations)
        and checks == expected_checks
        and actual_database_integrity == "ok"
        and store.database_profile(database_path) == "proof"
        and evidence.get("database_sha256") == _file_sha256(database_path)
        and report_path.stat().st_mode & 0o077 == 0
        and database_path.stat().st_mode & 0o077 == 0
    )
    return {
        "valid": valid, "report": str(report_path), "database": str(database_path),
        "expected_report_sha256": expected_report_sha256,
        "actual_report_sha256": actual_report_sha256,
        "content_consistency_sha256": supplied,
        "calculated_content_sha256": calculated,
        "invocation_count": len(invocations) if isinstance(invocations, list) else 0,
        "configuration_valid": exact_configuration, "host_valid": exact_host,
        "invocations_valid": exact_invocations, "launchd_job_absent": job_absent,
        "database_integrity": actual_database_integrity,
    }


def _valid_uuid(value: Any) -> bool:
    try:
        return str(UUID(str(value))) == str(value)
    except (TypeError, ValueError, AttributeError):
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brand-os-supervisor")
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render")
    render.add_argument("--label", default="com.brandos.worker.demo-brand")
    render.add_argument("--database", required=True)
    render.add_argument("--profile", default="operating", choices=sorted(ALLOWED_PROFILES))
    render.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    render.add_argument("--uv", default=shutil.which("uv") or "uv")
    render.add_argument("--stdout-log", required=True); render.add_argument("--stderr-log", required=True)
    render.add_argument("--interval-seconds", type=int, default=1800)
    render.add_argument("--max-jobs", type=int, default=100)
    render.add_argument("--max-decisions", type=int, default=50)
    render.add_argument("--output", required=True)
    install_parser = sub.add_parser("install"); install_parser.add_argument("plist")
    status_parser = sub.add_parser("status"); status_parser.add_argument("--label", default="com.brandos.worker.demo-brand")
    uninstall_parser = sub.add_parser("uninstall"); uninstall_parser.add_argument("--label", default="com.brandos.worker.demo-brand")
    proof = sub.add_parser("target-host-proof")
    proof.add_argument("--database", required=True); proof.add_argument("--report", required=True)
    proof.add_argument("--uv", default=shutil.which("uv") or "uv")
    proof.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    verify = sub.add_parser("target-host-proof-verify")
    verify.add_argument("--database", required=True)
    verify.add_argument("--expected-report-sha256", required=True)
    verify.add_argument("report")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.command == "render":
            payload = build_launchd_plist(
                label=args.label, database=args.database, profile=args.profile,
                project_root=args.project_root, uv=args.uv,
                stdout_log=args.stdout_log, stderr_log=args.stderr_log,
                interval_seconds=args.interval_seconds, max_jobs=args.max_jobs,
                max_decisions=args.max_decisions,
            )
            result = write_plist(payload, args.output)
        elif args.command == "install": result = install(args.plist)
        elif args.command == "status": result = status(args.label)
        elif args.command == "uninstall": result = uninstall(args.label)
        elif args.command == "target-host-proof":
            result = target_host_proof(
                database=args.database, report=args.report, uv=args.uv,
                project_root=args.project_root,
            )
        elif args.command == "target-host-proof-verify":
            result = verify_target_host_proof(
                args.report, args.database,
                expected_report_sha256=args.expected_report_sha256,
            )
            if not result["valid"]:
                print(json.dumps(result, sort_keys=True)); raise SystemExit(1)
        else:  # pragma: no cover
            raise ValueError("unsupported command")
    except (OSError, ValueError, plistlib.InvalidFileException, subprocess.SubprocessError) as error:
        raise SystemExit(f"brand-os-supervisor: {error}") from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = [
    "build_launchd_plist", "install", "main", "status", "target_host_proof",
    "uninstall", "validate_launchd_plist", "verify_target_host_proof", "write_plist",
]
