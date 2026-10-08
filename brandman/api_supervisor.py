"""Validated user-launchd lifecycle for the Brand OS loopback API origin."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
from typing import Any, Mapping

from brandman import store
from brandman.api_server import load_preview_password


DEFAULT_LABEL = "com.brandos.api.demo-brand"
MANAGED_MARKER = "brand-os-api-launchd/v1"
_LABEL = re.compile(r"^com\.brandos\.api\.[a-z0-9][a-z0-9.-]{0,63}$")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _file(value: str | Path, description: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"{description} does not exist")
    return path


def _private_parent(path: str | Path, description: str) -> Path:
    parent = Path(path).expanduser().resolve().parent
    if not parent.is_dir() or parent.stat().st_mode & 0o077:
        raise ValueError(f"{description} parent directory must exist and be private")
    return parent


def _public_hostname(value: str) -> str:
    """Return one normalized public DNS hostname or fail closed."""
    host = str(value).strip().casefold().rstrip(".")
    labels = host.split(".")
    if (
        not host
        or len(host) > 253
        or len(labels) < 2
        or any(not _HOST_LABEL.fullmatch(label) for label in labels)
        or labels[-1].isdigit()
    ):
        raise ValueError(
            "allowed host must be one explicit public DNS hostname without a scheme, port, path, or wildcard"
        )
    return host


def build_api_launchd_plist(
    *, database: str | Path, project_root: str | Path, uv: str | Path,
    password_file: str | Path, stdout_log: str | Path, stderr_log: str | Path,
    allowed_host: str, label: str = DEFAULT_LABEL,
) -> dict[str, Any]:
    if not _LABEL.fullmatch(label):
        raise ValueError("API label must match com.brandos.api.<lowercase-name>")
    database_path = _file(database, "database")
    if store.database_profile(database_path) != "operating":
        raise ValueError("API supervisor requires an operating-profile database")
    root = Path(project_root).expanduser().resolve()
    if not (root / "brandman" / "api_server.py").is_file():
        raise ValueError("project root does not contain brandman/api_server.py")
    uv_path = _file(uv, "uv executable")
    if not os.access(uv_path, os.X_OK):
        raise ValueError("uv executable is not executable")
    secret_path = _file(password_file, "preview password file")
    load_preview_password(secret_path)
    _private_parent(stdout_log, "stdout log")
    _private_parent(stderr_log, "stderr log")
    allowed_host = _public_hostname(allowed_host)
    return {
        "Label": label,
        "ProgramArguments": [
            str(uv_path), "run", "--project", str(root), "--no-sync",
            "python", "-m", "brandman.api_server",
        ],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "BRANDMAN_DB": str(database_path),
            "BRANDMAN_DATABASE_PROFILE": "operating",
            "BRANDMAN_ALLOWED_HOSTS": allowed_host,
            "BRANDMAN_REQUIRE_HTTPS": "true",
            "BRANDMAN_PREVIEW_PASSWORD_FILE": str(secret_path),
            "BRANDMAN_API_SUPERVISOR_MANAGED": MANAGED_MARKER,
        },
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "ProcessType": "Background", "LowPriorityIO": False,
        "StandardOutPath": str(Path(stdout_log).expanduser().resolve()),
        "StandardErrorPath": str(Path(stderr_log).expanduser().resolve()),
    }


def validate_api_launchd_plist(payload: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "Label", "ProgramArguments", "WorkingDirectory", "EnvironmentVariables",
        "RunAtLoad", "KeepAlive", "ThrottleInterval", "ProcessType", "LowPriorityIO",
        "StandardOutPath", "StandardErrorPath",
    }
    if set(payload) != expected:
        raise ValueError("managed API definition contains missing or unexpected keys")
    label = str(payload["Label"])
    if not _LABEL.fullmatch(label):
        raise ValueError("invalid managed API label")
    arguments = payload["ProgramArguments"]
    root = Path(str(payload["WorkingDirectory"])).expanduser().resolve()
    if not (root / "brandman" / "api_server.py").is_file():
        raise ValueError("managed working directory is not a Brand OS checkout")
    if not isinstance(arguments, list) or arguments[1:] != [
        "run", "--project", str(root), "--no-sync", "python", "-m", "brandman.api_server",
    ]:
        raise ValueError("managed API must execute brandman.api_server directly")
    _file(arguments[0], "uv executable")
    environment = payload["EnvironmentVariables"]
    expected_environment = {
        "BRANDMAN_DB", "BRANDMAN_DATABASE_PROFILE", "BRANDMAN_ALLOWED_HOSTS",
        "BRANDMAN_REQUIRE_HTTPS", "BRANDMAN_PREVIEW_PASSWORD_FILE",
        "BRANDMAN_API_SUPERVISOR_MANAGED",
    }
    if not isinstance(environment, dict) or set(environment) != expected_environment:
        raise ValueError("managed API environment contains missing or unexpected values")
    if "BRANDMAN_PREVIEW_PASSWORD" in environment:
        raise ValueError("preview password must never be embedded in the plist")
    if environment["BRANDMAN_DATABASE_PROFILE"] != "operating":
        raise ValueError("managed API database profile must be operating")
    database = _file(environment["BRANDMAN_DB"], "database")
    if store.database_profile(database) != "operating":
        raise ValueError("managed API database does not have the operating profile")
    allowed_host = _public_hostname(environment["BRANDMAN_ALLOWED_HOSTS"])
    if environment["BRANDMAN_ALLOWED_HOSTS"] != allowed_host:
        raise ValueError("managed API host allowlist must use one normalized exact hostname")
    if environment["BRANDMAN_REQUIRE_HTTPS"] != "true":
        raise ValueError("managed API must require HTTPS")
    password_file = _file(environment["BRANDMAN_PREVIEW_PASSWORD_FILE"], "preview password file")
    load_preview_password(password_file)
    if environment["BRANDMAN_API_SUPERVISOR_MANAGED"] != MANAGED_MARKER:
        raise ValueError("managed API marker is missing")
    if payload["RunAtLoad"] is not True or payload["KeepAlive"] is not True:
        raise ValueError("managed API must run at load and remain supervised")
    if payload["ThrottleInterval"] != 10 or payload["ProcessType"] != "Background":
        raise ValueError("managed API restart policy is invalid")
    _private_parent(payload["StandardOutPath"], "stdout log")
    _private_parent(payload["StandardErrorPath"], "stderr log")
    return {
        "label": label, "database": str(database), "profile": "operating",
        "bind": "127.0.0.1:8008", "allowed_host": allowed_host,
        "https_required": True, "forwarded_allow_ips": ["127.0.0.1", "::1"],
        "password_file": str(password_file), "contains_embedded_secret": False,
    }


def write_api_plist(payload: Mapping[str, Any], destination: str | Path) -> dict[str, Any]:
    target = Path(destination).expanduser().resolve()
    _private_parent(target, "plist")
    summary = validate_api_launchd_plist(payload)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            plistlib.dump(dict(payload), output, sort_keys=True)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {**summary, "plist": str(target), "mode": "0600"}


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments], text=True, capture_output=True,
        timeout=15, check=check,
    )


def _load(path: str | Path) -> dict[str, Any]:
    with _file(path, "plist").open("rb") as source:
        payload = plistlib.load(source)
    if not isinstance(payload, dict):
        raise ValueError("plist must contain a dictionary")
    return payload


def install_api(plist: str | Path) -> dict[str, Any]:
    source = _file(plist, "plist")
    summary = validate_api_launchd_plist(_load(source))
    directory = Path.home() / "Library" / "LaunchAgents"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / f"{summary['label']}.plist"
    if target.exists():
        if target.read_bytes() != source.read_bytes():
            raise ValueError("a different API launch agent already exists for this label")
        disposition = "already_installed"
    else:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(source.read_bytes())
        disposition = "installed"
    probe = _launchctl("print", f"{_domain()}/{summary['label']}", check=False)
    if probe.returncode != 0:
        try:
            _launchctl("bootstrap", _domain(), str(target))
        except Exception:
            if disposition == "installed" and _launchctl(
                "print", f"{_domain()}/{summary['label']}", check=False,
            ).returncode != 0:
                target.unlink(missing_ok=True)
            raise
    return {**summary, "status": disposition, "installed_plist": str(target)}


def api_status(label: str = DEFAULT_LABEL) -> dict[str, Any]:
    if not _LABEL.fullmatch(label):
        raise ValueError("invalid managed API label")
    target = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    summary = validate_api_launchd_plist(_load(target)) if target.is_file() else {}
    loaded = _launchctl("print", f"{_domain()}/{label}", check=False).returncode == 0
    return {**summary, "label": label, "installed": target.is_file(),
            "managed_configuration_valid": bool(summary), "loaded": loaded}


def uninstall_api(label: str = DEFAULT_LABEL) -> dict[str, Any]:
    if not _LABEL.fullmatch(label):
        raise ValueError("invalid managed API label")
    target = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    if not target.exists():
        loaded = _launchctl("print", f"{_domain()}/{label}", check=False).returncode == 0
        return {"label": label, "status": "already_absent", "loaded": loaded,
                "action": "Inspect loaded unmanaged job manually." if loaded else None}
    summary = validate_api_launchd_plist(_load(target))
    if _launchctl("print", f"{_domain()}/{label}", check=False).returncode == 0:
        _launchctl("bootout", _domain(), str(target))
        if _launchctl("print", f"{_domain()}/{label}", check=False).returncode == 0:
            raise ValueError("managed API remained loaded; configuration was preserved")
    target.unlink()
    return {**summary, "status": "uninstalled", "loaded": False}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brand-os-api-supervisor")
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render")
    render.add_argument("--database", required=True)
    render.add_argument("--password-file", required=True)
    render.add_argument("--stdout-log", required=True)
    render.add_argument("--stderr-log", required=True)
    render.add_argument("--output", required=True)
    render.add_argument(
        "--allowed-host", required=True,
        help="one exact public DNS hostname served by the reverse proxy",
    )
    render.add_argument("--label", default=DEFAULT_LABEL)
    render.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    render.add_argument("--uv", default=shutil.which("uv") or "uv")
    install_parser = sub.add_parser("install"); install_parser.add_argument("plist")
    status_parser = sub.add_parser("status"); status_parser.add_argument("--label", default=DEFAULT_LABEL)
    uninstall_parser = sub.add_parser("uninstall"); uninstall_parser.add_argument("--label", default=DEFAULT_LABEL)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.command == "render":
            payload = build_api_launchd_plist(
                database=args.database, project_root=args.project_root, uv=args.uv,
                password_file=args.password_file, stdout_log=args.stdout_log,
                stderr_log=args.stderr_log, allowed_host=args.allowed_host, label=args.label,
            )
            result = write_api_plist(payload, args.output)
        elif args.command == "install": result = install_api(args.plist)
        elif args.command == "status": result = api_status(args.label)
        elif args.command == "uninstall": result = uninstall_api(args.label)
        else:  # pragma: no cover
            raise ValueError("unsupported command")
    except (OSError, ValueError, plistlib.InvalidFileException, subprocess.SubprocessError) as error:
        raise SystemExit(f"brand-os-api-supervisor: {error}") from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_LABEL", "api_status", "build_api_launchd_plist", "install_api",
    "main", "uninstall_api", "validate_api_launchd_plist", "write_api_plist",
]
