from __future__ import annotations

import os
from pathlib import Path
import plistlib
import shutil
import subprocess

import pytest

from brandman import store
from brandman.api_server import load_preview_password, main as api_main
from brandman.api_supervisor import (
    DEFAULT_LABEL, api_status, build_api_launchd_plist, install_api,
    uninstall_api, validate_api_launchd_plist, write_api_plist,
)
from brandman.launchd_supervisor import build_launchd_plist, validate_launchd_plist


def setup_files(tmp_path: Path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700, parents=True)
    database = private / "brand_os.db"
    store.DATA_PATH = database
    monkeypatch.setenv("BRAND_OS_DATABASE_PROFILE", "operating")
    store.init_db(profile="operating")
    database.chmod(0o600)
    password = private / "preview-password"
    password.write_text("strong-test-preview-password", encoding="utf-8")
    password.chmod(0o600)
    return private, database, password


def payload(tmp_path: Path, monkeypatch):
    private, database, password = setup_files(tmp_path, monkeypatch)
    value = build_api_launchd_plist(
        database=database, project_root=Path(__file__).parents[1],
        uv=shutil.which("uv"), password_file=password,
        stdout_log=private / "api.jsonl", stderr_log=private / "api.stderr.log",
        allowed_host="usebrandman.com",
    )
    return private, database, password, value


def test_api_plist_is_secret_free_and_enforces_exact_origin_boundary(tmp_path, monkeypatch):
    private, database, password, value = payload(tmp_path, monkeypatch)
    summary = validate_api_launchd_plist(value)
    assert summary == {
        "label": DEFAULT_LABEL, "database": str(database.resolve()),
        "profile": "operating", "bind": "127.0.0.1:8008",
        "allowed_host": "usebrandman.com", "https_required": True,
        "forwarded_allow_ips": ["127.0.0.1", "::1"],
        "password_file": str(password.resolve()), "contains_embedded_secret": False,
    }
    serialized = plistlib.dumps(value).decode()
    assert "strong-test-preview-password" not in serialized
    assert "BRAND_OS_PREVIEW_PASSWORD" not in value["EnvironmentVariables"]
    assert value["EnvironmentVariables"]["BRAND_OS_ALLOWED_HOSTS"] == "usebrandman.com"
    assert value["EnvironmentVariables"]["BRAND_OS_REQUIRE_HTTPS"] == "true"
    assert value["ProgramArguments"][-2:] == ["-m", "brandman.api_server"]
    result = write_api_plist(value, private / "api.plist")
    assert result["mode"] == "0600"
    assert (private / "api.plist").stat().st_mode & 0o777 == 0o600


def test_repository_runtime_directory_is_ignored():
    project_root = Path(__file__).parents[1]
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".runtime/preview-password"],
        cwd=project_root,
        check=False,
    )
    assert ignored.returncode == 0


def test_password_file_must_be_private_regular_and_strong(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("short")
    secret.chmod(0o600)
    with pytest.raises(ValueError, match="at least 16"):
        load_preview_password(secret)
    secret.write_text("strong-enough-password")
    secret.chmod(0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        load_preview_password(secret)
    secret.chmod(0o600)
    link = tmp_path / "link"; link.symlink_to(secret)
    with pytest.raises(ValueError, match="non-symlink"):
        load_preview_password(link)


def test_api_launcher_reads_secret_then_trusts_only_loopback_proxy(tmp_path, monkeypatch):
    secret = tmp_path / "secret"
    secret.write_text("strong-runtime-password")
    secret.chmod(0o600)
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD_FILE", str(secret))
    previous_password = os.environ.get("BRAND_OS_PREVIEW_PASSWORD")
    observed = {}
    monkeypatch.setattr("brandman.api_server.uvicorn.run", lambda app, **kwargs: observed.update(
        {"app": app, **kwargs, "password": os.environ.get("BRAND_OS_PREVIEW_PASSWORD")}
    ))
    api_main()
    assert observed == {
        "app": "brandman.main:app", "host": "127.0.0.1", "port": 8008,
        "proxy_headers": True, "forwarded_allow_ips": "127.0.0.1,::1",
        "password": "strong-runtime-password",
    }
    assert os.environ.get("BRAND_OS_PREVIEW_PASSWORD") == previous_password


def test_validation_rejects_embedded_secret_host_drift_and_public_logs(tmp_path, monkeypatch):
    _, _, _, value = payload(tmp_path, monkeypatch)
    value["EnvironmentVariables"]["BRAND_OS_PREVIEW_PASSWORD"] = "embedded"
    with pytest.raises(ValueError, match="unexpected"):
        validate_api_launchd_plist(value)
    _, _, _, value = payload(tmp_path / "second", monkeypatch)
    value["EnvironmentVariables"]["BRAND_OS_ALLOWED_HOSTS"] = "*"
    with pytest.raises(ValueError, match="one explicit public DNS hostname"):
        validate_api_launchd_plist(value)
    _, _, _, value = payload(tmp_path / "third", monkeypatch)
    public = tmp_path / "public"; public.mkdir(mode=0o755)
    value["StandardOutPath"] = str(public / "api.log")
    with pytest.raises(ValueError, match="private"):
        validate_api_launchd_plist(value)


@pytest.mark.parametrize(
    "host",
    ["", "localhost", "*", "usebrandman.com,legacy.example", "https://usebrandman.com", "usebrandman.com:443", "-bad.example"],
)
def test_api_supervisor_rejects_non_exact_public_hostname(tmp_path, monkeypatch, host):
    private, database, password = setup_files(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="one explicit public DNS hostname"):
        build_api_launchd_plist(
            database=database, project_root=Path(__file__).parents[1],
            uv=shutil.which("uv"), password_file=password,
            stdout_log=private / "api.jsonl", stderr_log=private / "api.stderr.log",
            allowed_host=host,
        )


def test_api_supervisor_allows_explicit_legacy_fallback(tmp_path, monkeypatch):
    private, database, password = setup_files(tmp_path, monkeypatch)
    value = build_api_launchd_plist(
        database=database, project_root=Path(__file__).parents[1],
        uv=shutil.which("uv"), password_file=password,
        stdout_log=private / "api.jsonl", stderr_log=private / "api.stderr.log",
        allowed_host="brandman.example.com",
    )
    assert validate_api_launchd_plist(value)["allowed_host"] == "brandman.example.com"


def test_api_and_worker_supervisors_coexist_on_same_operating_database(tmp_path, monkeypatch):
    private, database, _, api = payload(tmp_path, monkeypatch)
    worker = build_launchd_plist(
        label="com.brandos.worker.demo-brand", database=database,
        profile="operating", project_root=Path(__file__).parents[1],
        uv=shutil.which("uv"), stdout_log=private / "worker.jsonl",
        stderr_log=private / "worker.stderr.log",
    )
    api_summary = validate_api_launchd_plist(api)
    worker_summary = validate_launchd_plist(worker, allowed_profiles={"operating"})
    assert api_summary["label"] != worker_summary["label"]
    assert api_summary["database"] == worker_summary["database"] == str(database.resolve())
    assert api_summary["profile"] == worker_summary["profile"] == "operating"


def test_install_status_uninstall_are_idempotent_and_managed(tmp_path, monkeypatch):
    private, _, _, value = payload(tmp_path, monkeypatch)
    source = private / "source.plist"; write_api_plist(value, source)
    home = tmp_path / "home"; home.mkdir(); monkeypatch.setenv("HOME", str(home))
    loaded = set()

    def fake_launchctl(*arguments, check=True):
        if arguments[0] == "print":
            return subprocess.CompletedProcess(arguments, 0 if DEFAULT_LABEL in loaded else 113, "", "")
        if arguments[0] == "bootstrap": loaded.add(DEFAULT_LABEL)
        if arguments[0] == "bootout": loaded.discard(DEFAULT_LABEL)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr("brandman.api_supervisor._launchctl", fake_launchctl)
    assert install_api(source)["status"] == "installed"
    assert install_api(source)["status"] == "already_installed"
    assert api_status()["loaded"] is True
    assert uninstall_api()["status"] == "uninstalled"
    assert uninstall_api()["status"] == "already_absent"
