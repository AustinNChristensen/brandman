"""OAuth 2.1 boundary for BrandMan's hosted Streamable HTTP MCP endpoint.

The tool implementations remain in :mod:`brandman.mcp_server`; this module owns only
client registration, PKCE authorization-code exchange, refresh/revocation, and
bearer-token validation.  Secrets are stored as SHA-256 digests, never returned
after issuance, and all state lives in the configured Brand OS database.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import html
import os
import secrets
import sqlite3
import threading
import time
from contextlib import AsyncExitStack
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import store

# The MCP session manager logs start/stop at INFO through a stdout handler, which
# corrupts scripts that print JSON from inside the app lifespan.
logging.getLogger("mcp.server.streamable_http_manager").setLevel(logging.WARNING)

SCOPE = "brandman:mcp"
ACCESS_TTL_SECONDS = 8 * 60 * 60
REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60
CODE_TTL_SECONDS = 5 * 60


AUTH_FAILURE_LIMIT = 5
AUTH_FAILURE_WINDOW_SECONDS = 10 * 60
_auth_failures: dict[str, list[float]] = {}
_auth_failures_lock = threading.Lock()


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def _throttled(key: str) -> bool:
    """Per-process limiter for operator-password attempts on /oauth/authorize.

    It is in memory, so each web instance counts separately and a restart resets it.
    """
    cutoff = time.monotonic() - AUTH_FAILURE_WINDOW_SECONDS
    with _auth_failures_lock:
        recent = [t for t in _auth_failures.get(key, []) if t > cutoff]
        _auth_failures[key] = recent
        return len(recent) >= AUTH_FAILURE_LIMIT


def _record_failure(key: str) -> None:
    with _auth_failures_lock:
        _auth_failures.setdefault(key, []).append(time.monotonic())


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> int:
    return int(time.time())


def _origin(request: Request) -> str:
    # FastAPI Cloud terminates TLS before the app.  Its forwarded values are
    # meaningful only after the host boundary has accepted the request.
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "")).split(",", 1)[0].strip()
    return f"{scheme}://{host}"


def resource_url(request: Request) -> str:
    # Starlette normalizes an ASGI mount to a trailing slash.  The advertised
    # OAuth resource remains the canonical, no-trailing-slash public URL.
    return f"{_origin(request)}/mcp"


def _connection() -> sqlite3.Connection:
    store.DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(store.DATA_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS hosted_mcp_clients (
          client_id TEXT PRIMARY KEY, redirect_uris_json TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hosted_mcp_codes (
          code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL,
          redirect_uri TEXT NOT NULL, resource TEXT NOT NULL,
          code_challenge TEXT NOT NULL, scope TEXT NOT NULL,
          expires_at INTEGER NOT NULL, consumed_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS hosted_mcp_tokens (
          token_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL,
          resource TEXT NOT NULL, scope TEXT NOT NULL, token_type TEXT NOT NULL,
          expires_at INTEGER NOT NULL, revoked_at INTEGER
        );
        """
    )
    return conn


def _valid_redirect(uri: str) -> bool:
    parsed = urlparse(uri)
    if parsed.scheme == "https" and parsed.netloc:
        return True
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def protected_resource_metadata(request: Request) -> dict[str, object]:
    return {
        "resource": resource_url(request),
        "authorization_servers": [_origin(request)],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
    }


def authorization_server_metadata(request: Request) -> dict[str, object]:
    origin = _origin(request)
    return {
        "issuer": origin,
        "authorization_endpoint": f"{origin}/oauth/authorize",
        "token_endpoint": f"{origin}/oauth/token",
        "registration_endpoint": f"{origin}/oauth/register",
        "revocation_endpoint": f"{origin}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": [SCOPE],
    }


async def register_client(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception as error:
        raise HTTPException(status_code=400, detail="invalid_client_metadata") from error
    redirect_uris = payload.get("redirect_uris") if isinstance(payload, dict) else None
    if not isinstance(redirect_uris, list) or not redirect_uris or not all(isinstance(uri, str) and _valid_redirect(uri) for uri in redirect_uris):
        raise HTTPException(status_code=400, detail="invalid_redirect_uri")
    client_id = f"brandman_{secrets.token_urlsafe(24)}"
    import json
    with _connection() as conn:
        conn.execute(
            "INSERT INTO hosted_mcp_clients(client_id, redirect_uris_json, created_at) VALUES (?, ?, ?)",
            (client_id, json.dumps(sorted(set(redirect_uris))), _now()),
        )
    return JSONResponse({"client_id": client_id, "token_endpoint_auth_method": "none"}, status_code=201)


def _authorize_input(request: Request) -> tuple[str, str, str, str, str, str]:
    params = request.query_params
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    resource = params.get("resource", "")
    challenge = params.get("code_challenge", "")
    scope = params.get("scope", "")
    if params.get("response_type") != "code" or not client_id or not redirect_uri or not state or resource != resource_url(request) or challenge == "" or scope != SCOPE:
        raise HTTPException(status_code=400, detail="invalid_request")
    with _connection() as conn:
        client = conn.execute("SELECT redirect_uris_json FROM hosted_mcp_clients WHERE client_id=?", (client_id,)).fetchone()
    if client is None or redirect_uri not in __import__("json").loads(client["redirect_uris_json"]):
        raise HTTPException(status_code=400, detail="invalid_client")
    return client_id, redirect_uri, state, resource, challenge, scope


def _authorization_page(request: Request, *, error: str | None = None) -> HTMLResponse:
    message = f'<p role="alert">{html.escape(error)}</p>' if error else ""
    hidden = "".join(f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}">' for key, value in request.query_params.items())
    return HTMLResponse(
        "<!doctype html><title>Authorize BrandMan</title>"
        "<main><h1>Authorize BrandMan MCP</h1><p>This lets the requesting client use BrandMan's MCP tools. It can read brand data and create or edit drafts, campaigns, records and connector jobs. Approving and publishing public content stay with a human in the dashboard.</p>"
        f"{message}<form method=post>{hidden}<label>Operator password <input name=password type=password required autofocus></label>"
        "<button type=submit>Authorize</button></form></main>"
    )


async def authorize(request: Request) -> RedirectResponse | HTMLResponse:
    try:
        client_id, redirect_uri, state, resource, challenge, scope = _authorize_input(request)
    except HTTPException as error:
        if request.method == "GET":
            return JSONResponse({"error": error.detail}, status_code=error.status_code)
        raise
    if request.method == "GET":
        return _authorization_page(request)
    throttle_key = _client_key(request)
    if _throttled(throttle_key):
        return HTMLResponse("Too many attempts. Try again later.", status_code=429, headers={"Retry-After": str(AUTH_FAILURE_WINDOW_SECONDS)})
    body = await request.body()
    supplied = parse_qs(body.decode("utf-8", errors="replace")).get("password", [""])[0]
    expected = os.getenv("BRAND_OS_PREVIEW_PASSWORD", "")
    if not expected or not secrets.compare_digest(supplied, expected):
        _record_failure(throttle_key)
        return _authorization_page(request, error="That password was not accepted.")
    code = secrets.token_urlsafe(32)
    with _connection() as conn:
        conn.execute(
            "INSERT INTO hosted_mcp_codes(code_hash, client_id, redirect_uri, resource, code_challenge, scope, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_digest(code), client_id, redirect_uri, resource, challenge, scope, _now() + CODE_TTL_SECONDS),
        )
    query = urlencode({"code": code, "state": state})
    return RedirectResponse(f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}{query}", status_code=303)


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


async def token(request: Request) -> JSONResponse:
    fields = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    grant_type = fields.get("grant_type", [""])[0]
    if grant_type == "authorization_code":
        code = fields.get("code", [""])[0]
        verifier = fields.get("code_verifier", [""])[0]
        client_id = fields.get("client_id", [""])[0]
        redirect_uri = fields.get("redirect_uri", [""])[0]
        resource = fields.get("resource", [""])[0]
        with _connection() as conn:
            row = conn.execute("SELECT * FROM hosted_mcp_codes WHERE code_hash=?", (_digest(code),)).fetchone()
            if row is None or row["consumed_at"] is not None or row["expires_at"] < _now() or row["client_id"] != client_id or row["redirect_uri"] != redirect_uri or row["resource"] != resource or not verifier or not secrets.compare_digest(row["code_challenge"], _pkce_challenge(verifier)):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            claimed = conn.execute(
                "UPDATE hosted_mcp_codes SET consumed_at=? WHERE code_hash=? AND consumed_at IS NULL",
                (_now(), _digest(code)),
            )
            if claimed.rowcount != 1:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            scope = row["scope"]
    elif grant_type == "refresh_token":
        refresh = fields.get("refresh_token", [""])[0]
        client_id = fields.get("client_id", [""])[0]
        resource = fields.get("resource", [""])[0]
        with _connection() as conn:
            row = conn.execute("SELECT * FROM hosted_mcp_tokens WHERE token_hash=?", (_digest(refresh),)).fetchone()
            if row is None or row["token_type"] != "refresh" or row["revoked_at"] is not None or row["expires_at"] < _now() or row["client_id"] != client_id or row["resource"] != resource:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            claimed = conn.execute(
                "UPDATE hosted_mcp_tokens SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (_now(), _digest(refresh)),
            )
            if claimed.rowcount != 1:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            scope = row["scope"]
    else:
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with _connection() as conn:
        conn.execute("INSERT INTO hosted_mcp_tokens(token_hash, client_id, resource, scope, token_type, expires_at) VALUES (?, ?, ?, ?, 'access', ?)", (_digest(access), client_id, resource, scope, _now() + ACCESS_TTL_SECONDS))
        conn.execute("INSERT INTO hosted_mcp_tokens(token_hash, client_id, resource, scope, token_type, expires_at) VALUES (?, ?, ?, ?, 'refresh', ?)", (_digest(refresh), client_id, resource, scope, _now() + REFRESH_TTL_SECONDS))
    return JSONResponse({"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL_SECONDS, "refresh_token": refresh, "scope": scope})


async def revoke(request: Request) -> JSONResponse:
    fields = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    value = fields.get("token", [""])[0]
    if value:
        with _connection() as conn:
            conn.execute("UPDATE hosted_mcp_tokens SET revoked_at=? WHERE token_hash=?", (_now(), _digest(value)))
    return JSONResponse({}, status_code=200)


def verify_mcp_bearer(request: Request) -> None:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid_token")
    value = authorization.removeprefix("Bearer ")
    with _connection() as conn:
        row = conn.execute("SELECT * FROM hosted_mcp_tokens WHERE token_hash=?", (_digest(value),)).fetchone()
    if row is None or row["token_type"] != "access" or row["revoked_at"] is not None or row["expires_at"] < _now() or row["resource"] != resource_url(request) or row["scope"] != SCOPE:
        raise HTTPException(status_code=401, detail="invalid_token")


class LazyMcpApplication:
    """Mounted MCP ASGI app with a fresh session manager per web lifespan."""

    def __init__(self) -> None:
        self.application: object | None = None
        self._lifespan: AsyncExitStack | None = None

    async def start(self) -> None:
        from .mcp_server import mcp
        # FastMCP keeps a one-shot session manager on the server object.  A
        # process gets one production lifespan, while TestClient deliberately
        # opens several; reset only after the previous lifespan has closed.
        mcp._session_manager = None  # type: ignore[attr-defined]
        self.application = mcp.streamable_http_app()
        stack = AsyncExitStack()
        await stack.enter_async_context(self.application.router.lifespan_context(self.application))  # type: ignore[union-attr]
        self._lifespan = stack

    async def stop(self) -> None:
        if self._lifespan is not None:
            await self._lifespan.aclose()
        self._lifespan = None
        self.application = None

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if self.application is None:
            raise RuntimeError("Hosted MCP transport was not initialized")
        await self.application(scope, receive, send)  # type: ignore[misc]
