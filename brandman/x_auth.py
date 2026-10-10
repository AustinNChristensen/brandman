"""Encrypted OAuth2 refresh-token rotation for durable X workers."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json
import threading
from typing import Callable

from brandman.connectors import ConnectorError, ConnectorKind, HttpTransport
from brandman.credentials import CredentialStore


TOKEN_ENDPOINT = "https://api.x.com/2/oauth2/token"


class XOAuthRefreshError(RuntimeError):
    """Credential-safe refresh failure."""


class XOAuthTokenSupplier:
    def __init__(
        self, credentials: CredentialStore, account_id: str, transport: HttpTransport,
        *, token_endpoint: str = TOKEN_ENDPOINT,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        refresh_skew_seconds: int = 120,
    ) -> None:
        self.credentials = credentials
        self.account_id = account_id
        self.transport = transport
        self.token_endpoint = token_endpoint
        self.clock = clock
        self.refresh_skew_seconds = refresh_skew_seconds
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            metadata = self.credentials.get("x", self.account_id)
            values = self.credentials.secret("x", self.account_id).reveal()
            token = _required(values, "access_token")
            # Native durable mode is intentionally refresh-only. An unverified
            # static token must not silently become a production worker token.
            refresh_token = _required(values, "refresh_token")
            client_id = _required(values, "client_id")
            expires_at = _expiry(_required(values, "expires_at"))
            if expires_at > self.clock().astimezone(UTC) + timedelta(
                seconds=self.refresh_skew_seconds
            ):
                return token if token.startswith("Bearer ") else f"Bearer {token}"
            headers = {"Accept": "application/json"}
            client_secret = values.get("client_secret")
            if isinstance(client_secret, str) and client_secret:
                basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
                headers["Authorization"] = f"Basic {basic}"
            response = self.transport.request(
                "POST", self.token_endpoint, headers=headers,
                form_body={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
            )
            if not 200 <= response.status_code < 300:
                raise ConnectorError(ConnectorKind.X, "oauth token refresh", response.status_code)
            try:
                document = response.json()
                new_access = _required(document, "access_token")
                expires_in = int(document["expires_in"])
                if expires_in <= 0:
                    raise ValueError
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise XOAuthRefreshError("X OAuth refresh response is invalid") from exc
            rotated = {
                **values,
                "access_token": new_access,
                "refresh_token": str(document.get("refresh_token") or refresh_token),
                "expires_at": (
                    self.clock().astimezone(UTC) + timedelta(seconds=expires_in)
                ).isoformat(),
            }
            self.credentials.put(
                "x", self.account_id, metadata.display_name, rotated,
                required_scopes=metadata.required_scopes,
                granted_scopes=metadata.granted_scopes,
                actor="system:x-oauth-refresh",
            )
            return f"Bearer {new_access}"


def _required(values, key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise XOAuthRefreshError(f"X OAuth credential is missing {key}")
    return value


def _expiry(value) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise XOAuthRefreshError("X OAuth expires_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise XOAuthRefreshError("X OAuth expires_at is invalid") from exc
    if parsed.tzinfo is None:
        raise XOAuthRefreshError("X OAuth expires_at must include a timezone")
    return parsed.astimezone(UTC)


__all__ = ["TOKEN_ENDPOINT", "XOAuthRefreshError", "XOAuthTokenSupplier"]
