"""Fail-closed HTTP boundary for the local assisted operator service."""
from __future__ import annotations

import ipaddress
import base64
import hashlib
import hmac
import os
import re
import time
from collections.abc import Mapping
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, Response


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "testserver"})
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_PLACEHOLDER_PASSWORDS = frozenset({
    "password", "changeme", "change-me", "configured", "preview-password",
    "<stored preview password>", "<retrieve from the protected environment>",
})
PREVIEW_SESSION_COOKIE = "brandos_session"
PREVIEW_SESSION_TTL_SECONDS = 8 * 60 * 60


def enforce_request_boundary(
    request: Request, *, environment: Mapping[str, str] | None = None,
) -> Response | None:
    """Reject unsafe host, transport, or cross-site browser requests.

    Headerless non-browser clients remain supported because they explicitly
    supply HTTP Basic authentication. Browser requests carrying Origin,
    Referer, or Fetch Metadata must be same-origin for every mutation.
    """

    env = environment if environment is not None else os.environ
    host = _request_host(request)
    allowed, configuration_error = _allowed_hosts(env)
    if configuration_error:
        return _error(503, configuration_error)
    if host is None or host not in allowed:
        return _error(421, "Request host is not allowed by this deployment.")

    require_https, configuration_error = _require_https(env)
    if configuration_error:
        return _error(503, configuration_error)
    is_local = _is_loopback(host)
    if is_local and host != "testserver" and not _client_is_loopback(request):
        return _error(421, "Loopback hostnames are accepted only from a loopback client.")
    if request.url.scheme != "https" and (require_https or not is_local):
        return _error(400, "HTTPS is required for this deployment.")

    password = str(env.get("BRANDMAN_PREVIEW_PASSWORD") or "")
    if not is_local and not _strong_remote_password(password):
        return _error(
            503,
            "Remote preview authentication requires a non-placeholder password of at least 16 characters.",
        )

    if request.method.upper() not in _SAFE_METHODS:
        # Login authenticates the local operator; it does not perform a BrandMan
        # mutation. Some Chrome navigation paths classify this top-level form
        # submission as cross-site even though it targets the page's own URL.
        # Host, loopback/HTTPS, password verification, and strict session-cookie
        # checks still apply. Every operational/API mutation remains below.
        if request.url.path == "/login":
            return None
        fetch_site = request.headers.get("sec-fetch-site", "").strip().casefold()
        if fetch_site in {"cross-site", "none"}:
            return _error(403, "Cross-site browser mutations are not allowed.")
        origin = request.headers.get("origin")
        if origin is not None and not _same_origin(origin, request):
            return _error(403, "Cross-site browser mutations are not allowed.")
        referer = request.headers.get("referer")
        if origin is None and referer is not None and not _same_origin(referer, request):
            return _error(403, "Cross-site browser mutations are not allowed.")
    return None


def harden_response(response: Response, request: Request) -> Response:
    """Apply browser and intermediary protections to every response."""

    headers = {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        "Content-Security-Policy": (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'; "
            "form-action 'self'; connect-src 'self'; "
            "img-src 'self' data: https://fastapi.tiangolo.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com"
        ),
    }
    if request.url.scheme == "https":
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    for name, value in headers.items():
        response.headers[name] = value
    return response


def deployment_boundary_status(environment: Mapping[str, str]) -> dict[str, object]:
    """Return secret-free deployment readiness for the HTTP boundary."""

    password = str(environment.get("BRANDMAN_PREVIEW_PASSWORD") or "")
    allowed, host_error = _allowed_hosts(environment)
    require_https, https_error = _require_https(environment)
    remote_hosts = sorted(host for host in allowed if not _is_loopback(host))
    remote_password_ready = not remote_hosts or _strong_remote_password(password)
    healthy = bool(password) and host_error is None and https_error is None
    healthy = healthy and remote_password_ready and (not remote_hosts or require_https)
    actions: list[str] = []
    if not password:
        actions.append("Set BRANDMAN_PREVIEW_PASSWORD in the deployed service environment.")
    if host_error:
        actions.append("Set BRANDMAN_ALLOWED_HOSTS to explicit hostnames without wildcards.")
    if remote_hosts and not require_https:
        actions.append("Set BRANDMAN_REQUIRE_HTTPS=true and terminate TLS before exposing a remote hostname.")
    if not remote_password_ready:
        actions.append("Use a non-placeholder preview password of at least 16 characters for remote access.")
    return {
        "configured": bool(password), "healthy": healthy,
        "local_only": not remote_hosts, "remote_host_count": len(remote_hosts),
        "https_required": require_https, "host_allowlist_valid": host_error is None,
        "remote_password_ready": remote_password_ready, "actions": actions,
    }


def create_preview_session(
    password: str, *, now: int | None = None, nonce: str | None = None,
) -> str:
    """Create a short-lived signed browser session without storing credentials."""

    issued_at = int(time.time() if now is None else now)
    random_nonce = nonce or secrets_token()
    payload = f"v1:{issued_at + PREVIEW_SESSION_TTL_SECONDS}:{random_nonce}"
    signature = hmac.new(password.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return _urlsafe_encode(f"{payload}:{signature}".encode())


def validate_preview_session(
    token: str, password: str, *, now: int | None = None,
) -> bool:
    """Validate expiry and signature; malformed cookies always fail closed."""

    if not token or not password:
        return False
    try:
        decoded = _urlsafe_decode(token).decode()
        version, expires, nonce, supplied = decoded.split(":", 3)
        expires_at = int(expires)
    except (ValueError, UnicodeDecodeError):
        return False
    if version != "v1" or not nonce or expires_at < int(time.time() if now is None else now):
        return False
    payload = f"{version}:{expires_at}:{nonce}"
    expected = hmac.new(password.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, expected)


def secrets_token() -> str:
    """Return a URL-safe nonce through an isolated seam for deterministic tests."""

    import secrets
    return secrets.token_urlsafe(18)


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _request_host(request: Request) -> str | None:
    raw = request.headers.get("host", "").strip()
    if not raw or any(character in raw for character in ("/", "\\", "@", "\x00")):
        return None
    try:
        parsed = urlsplit(f"//{raw}")
        # Accessing port validates malformed and out-of-range ports.
        _ = parsed.port
    except ValueError:
        return None
    return parsed.hostname.casefold().rstrip(".") if parsed.hostname else None


def _allowed_hosts(environment: Mapping[str, str]) -> tuple[frozenset[str], str | None]:
    configured = str(environment.get("BRANDMAN_ALLOWED_HOSTS") or "").strip()
    if not configured:
        return _LOCAL_HOSTS, None
    values: set[str] = set()
    for raw in configured.split(","):
        host = raw.strip().casefold().rstrip(".")
        if not host or host == "*" or (host not in _LOCAL_HOSTS and not _HOST.fullmatch(host)):
            return frozenset(), "BRANDMAN_ALLOWED_HOSTS must contain explicit hostnames without wildcards."
        values.add(host)
    return frozenset(values | set(_LOCAL_HOSTS)), None


def _require_https(environment: Mapping[str, str]) -> tuple[bool, str | None]:
    raw = str(environment.get("BRANDMAN_REQUIRE_HTTPS") or "").strip().casefold()
    if raw in _TRUE:
        return True, None
    if raw in _FALSE:
        return False, None
    return False, "BRANDMAN_REQUIRE_HTTPS must be true or false."


def _is_loopback(host: str) -> bool:
    if host in {"localhost", "testserver"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _client_is_loopback(request: Request) -> bool:
    if request.client is None:
        return False
    host = str(request.client.host or "").strip().casefold()
    if host in {"localhost", "testclient"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or any(address in network for network in _trusted_local_networks())


def _trusted_local_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Private networks treated as the local machine, for container setups.

    Docker forwards a host's loopback request from the bridge gateway, not from
    127.0.0.1. ``BRANDMAN_TRUSTED_LOCAL_NETWORKS`` (comma-separated CIDRs) lets
    such a request count as local. Only set it when the published port is bound
    to the host's loopback interface; public or global ranges are ignored.
    """
    networks = []
    for value in os.environ.get("BRANDMAN_TRUSTED_LOCAL_NETWORKS", "").split(","):
        value = value.strip()
        if not value:
            continue
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError:
            continue
        if network.is_private and not network.is_global:
            networks.append(network)
    return networks


def _strong_remote_password(password: str) -> bool:
    return len(password) >= 16 and password.casefold() not in _PLACEHOLDER_PASSWORDS


def _same_origin(value: str, request: Request) -> bool:
    if value.strip().casefold() == "null":
        return False
    try:
        candidate = urlsplit(value)
        candidate_port = candidate.port
    except ValueError:
        return False
    if candidate.scheme not in {"http", "https"} or not candidate.hostname:
        return False
    request_host = _request_host(request)
    if request_host is None or candidate.hostname.casefold().rstrip(".") != request_host:
        return False
    request_port = request.url.port or (443 if request.url.scheme == "https" else 80)
    normalized_candidate_port = candidate_port or (443 if candidate.scheme == "https" else 80)
    return candidate.scheme == request.url.scheme and normalized_candidate_port == request_port
