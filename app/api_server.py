"""Fail-closed target-host API launcher for the loopback Cloudflare origin."""
from __future__ import annotations

import os
from pathlib import Path

import uvicorn


PASSWORD_FILE_ENV = "BRAND_OS_PREVIEW_PASSWORD_FILE"


def load_preview_password(path: str | Path) -> str:
    """Read a strong preview password from an owner-only regular file."""
    source = Path(path).expanduser().resolve()
    if Path(path).expanduser().is_symlink() or not source.is_file():
        raise ValueError("preview password path must be a regular, non-symlink file")
    stat = source.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o777 != 0o600:
        raise ValueError("preview password file must be owned by the current user and mode 0600")
    if stat.st_size > 4096:
        raise ValueError("preview password file is too large")
    value = source.read_text(encoding="utf-8").rstrip("\r\n")
    if len(value) < 16 or not value.strip() or "\n" in value or "\r" in value:
        raise ValueError("preview password file must contain one value of at least 16 characters")
    return value


def main() -> None:
    password_file = os.environ.get(PASSWORD_FILE_ENV)
    if not password_file:
        raise SystemExit(f"{PASSWORD_FILE_ENV} is required")
    try:
        password = load_preview_password(password_file)
    except (OSError, UnicodeError, ValueError) as error:
        raise SystemExit(f"brand-os-api: {error}") from None
    variable = "BRAND_OS_PREVIEW_PASSWORD"
    previous = os.environ.get(variable)
    os.environ[variable] = password
    try:
        # The public hostname is enforced by the app. Only the local tunnel may
        # assert the original HTTPS scheme through X-Forwarded-Proto.
        uvicorn.run(
            "app.main:app", host="127.0.0.1", port=8008,
            proxy_headers=True, forwarded_allow_ips="127.0.0.1,::1",
        )
    finally:
        if previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = previous


if __name__ == "__main__":
    main()


__all__ = ["PASSWORD_FILE_ENV", "load_preview_password", "main"]
