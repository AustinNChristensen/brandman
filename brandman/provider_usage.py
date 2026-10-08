"""Payload-free provider usage and operator-priced cost accounting."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from fnmatch import fnmatchcase
import hashlib
import json
import re
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from brandman.connectors import HttpResponse, HttpTransport


SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_pricing_versions (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL DEFAULT '*',
  version TEXT NOT NULL, provider TEXT NOT NULL,
  method TEXT NOT NULL, endpoint_pattern TEXT NOT NULL, unit_name TEXT NOT NULL,
  billable_category TEXT NOT NULL DEFAULT '*',
  unit_price TEXT NOT NULL, currency TEXT NOT NULL, effective_at TEXT NOT NULL,
  configured_by TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(brand_id,version,provider,method,endpoint_pattern,billable_category)
);
CREATE TABLE IF NOT EXISTS provider_usage_events (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL, connector_account_id TEXT NOT NULL,
  provider TEXT NOT NULL, method TEXT NOT NULL, endpoint TEXT NOT NULL,
  status_code INTEGER, outcome TEXT NOT NULL, units TEXT NOT NULL,
  unit_name TEXT NOT NULL,
  billable_category TEXT NOT NULL DEFAULT 'unclassified',
  provider_request_id TEXT, response_resource_count INTEGER,
  pricing_version_id TEXT REFERENCES provider_pricing_versions(id),
  estimated_cost TEXT, currency TEXT, observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS provider_usage_brand_time
  ON provider_usage_events(brand_id,observed_at DESC);
"""


class ProviderUsageLedger:
    def __init__(self, database: str | Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.database = str(database)
        self.clock = clock or (lambda: datetime.now(UTC))
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute(
                "PRAGMA table_info(provider_usage_events)"
            )}
            if "unit_name" not in columns:
                connection.execute(
                    "ALTER TABLE provider_usage_events ADD COLUMN unit_name TEXT NOT NULL DEFAULT 'request'"
                )
            for name, definition in {
                "billable_category": "TEXT NOT NULL DEFAULT 'unclassified'",
                "provider_request_id": "TEXT",
                "response_resource_count": "INTEGER",
            }.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE provider_usage_events ADD COLUMN {name} {definition}"
                    )
            price_columns = {row["name"] for row in connection.execute(
                "PRAGMA table_info(provider_pricing_versions)"
            )}
            if "billable_category" not in price_columns:
                connection.execute(
                    "ALTER TABLE provider_pricing_versions ADD COLUMN billable_category TEXT NOT NULL DEFAULT '*'"
                )
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='provider_pricing_versions'"
            ).fetchone()["sql"]
            normalized_sql = table_sql.replace(" ", "").replace("\n", "")
            if (
                "brand_id" not in price_columns
                or "UNIQUE(brand_id,version,provider,method,endpoint_pattern,billable_category)"
                not in normalized_sql
            ):
                # Adding a column cannot widen SQLite's table-level UNIQUE
                # constraint. Build the final table beside the legacy table,
                # preserve stable IDs used by usage events, then swap names.
                connection.execute("DROP TABLE IF EXISTS provider_pricing_versions_v2")
                connection.execute(
                    """CREATE TABLE provider_pricing_versions_v2 (
                      id TEXT PRIMARY KEY, brand_id TEXT NOT NULL DEFAULT '*',
                      version TEXT NOT NULL, provider TEXT NOT NULL,
                      method TEXT NOT NULL, endpoint_pattern TEXT NOT NULL,
                      unit_name TEXT NOT NULL, billable_category TEXT NOT NULL DEFAULT '*',
                      unit_price TEXT NOT NULL, currency TEXT NOT NULL,
                      effective_at TEXT NOT NULL, configured_by TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      UNIQUE(brand_id,version,provider,method,endpoint_pattern,billable_category)
                    )"""
                )
                brand_expression = "brand_id" if "brand_id" in price_columns else "'*'"
                connection.execute(
                    f"""INSERT INTO provider_pricing_versions_v2
                       (id,brand_id,version,provider,method,endpoint_pattern,unit_name,
                        billable_category,unit_price,currency,effective_at,
                        configured_by,created_at)
                       SELECT id,{brand_expression},version,provider,method,endpoint_pattern,unit_name,
                              billable_category,unit_price,currency,effective_at,
                              configured_by,created_at
                       FROM provider_pricing_versions"""
                )
                connection.execute("DROP TABLE provider_pricing_versions")
                connection.execute(
                    "ALTER TABLE provider_pricing_versions_v2 RENAME TO provider_pricing_versions"
                )

    def configure_price(
        self, *, version: str, provider: str, method: str,
        endpoint_pattern: str, unit_name: str, unit_price: str,
        currency: str, effective_at: str, actor: str,
        billable_category: str = "*",
        brand_id: str = "*",
    ) -> dict[str, Any]:
        price = _decimal(unit_price, "unit_price")
        if price < 0:
            raise ValueError("unit_price cannot be negative")
        effective = _timestamp(effective_at)
        record = {
            "id": str(uuid4()), "brand_id": _text(brand_id, "brand_id"),
            "version": _text(version, "version"),
            "provider": _text(provider, "provider").lower(),
            "method": _text(method, "method").upper(),
            "billable_category": _text(billable_category, "billable_category"),
            "endpoint_pattern": _provider_pattern(provider, endpoint_pattern),
            "unit_name": _text(unit_name, "unit_name"), "unit_price": str(price),
            "currency": _text(currency, "currency").upper(),
            "effective_at": effective, "configured_by": _text(actor, "actor"),
            "created_at": self._now(),
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT * FROM provider_pricing_versions
                   WHERE brand_id=:brand_id AND version=:version AND provider=:provider
                     AND method=:method AND endpoint_pattern=:endpoint_pattern
                     AND billable_category=:billable_category""",
                record,
            ).fetchone()
            if existing is not None:
                current = dict(existing)
                material = ("unit_name", "unit_price", "currency", "effective_at")
                if all(current[key] == record[key] for key in material):
                    return current
                raise ValueError(
                    "rate-card identity already exists with different pricing; use a new version"
                )
            connection.execute(
                """INSERT INTO provider_pricing_versions
                   (id,brand_id,version,provider,method,endpoint_pattern,billable_category,unit_name,unit_price,
                    currency,effective_at,configured_by,created_at)
                   VALUES (:id,:brand_id,:version,:provider,:method,:endpoint_pattern,:billable_category,:unit_name,
                           :unit_price,:currency,:effective_at,:configured_by,:created_at)""",
                record,
            )
        return record

    def list_prices(self, brand_id: str) -> list[dict[str, Any]]:
        """Return customer-supplied rate versions for exactly one brand."""
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT * FROM provider_pricing_versions WHERE brand_id=?
                   ORDER BY effective_at DESC,created_at DESC,id""",
                (_text(brand_id, "brand_id"),),
            )]

    def record(
        self, *, brand_id: str, connector_account_id: str, provider: str,
        method: str, url: str, status_code: int | None, units: str = "1",
        outcome: str = "response",
        billable_category: str = "unclassified",
        provider_request_id: str | None = None,
        response_resource_count: int | None = None,
    ) -> dict[str, Any]:
        observed_at = self._now()
        endpoint = safe_endpoint(url)
        quantity = _decimal(units, "units")
        if quantity < 0:
            raise ValueError("units cannot be negative")
        price = self._matching_price(
            brand_id, provider, method, endpoint, billable_category, observed_at,
        )
        estimated = str(quantity * Decimal(price["unit_price"])) if price else None
        event = {
            "id": str(uuid4()), "brand_id": brand_id,
            "connector_account_id": connector_account_id,
            "provider": provider.lower(), "method": method.upper(),
            "endpoint": endpoint, "status_code": status_code,
            "outcome": outcome, "units": str(quantity),
            "unit_name": price["unit_name"] if price else "request",
            "billable_category": billable_category,
            "provider_request_id": provider_request_id,
            "response_resource_count": response_resource_count,
            "pricing_version_id": price["id"] if price else None,
            "estimated_cost": estimated,
            "currency": price["currency"] if price else None,
            "observed_at": observed_at,
        }
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO provider_usage_events
                   (id,brand_id,connector_account_id,provider,method,endpoint,status_code,
                    outcome,units,unit_name,billable_category,provider_request_id,
                    response_resource_count,pricing_version_id,estimated_cost,currency,observed_at)
                   VALUES (:id,:brand_id,:connector_account_id,:provider,:method,:endpoint,
                           :status_code,:outcome,:units,:unit_name,:billable_category,
                           :provider_request_id,:response_resource_count,:pricing_version_id,:estimated_cost,
                           :currency,:observed_at)""", event,
            )
        return event

    def report(self, brand_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT provider,method,endpoint,billable_category,status_code,outcome,
                          provider_request_id,response_resource_count,units,unit_name,
                          estimated_cost,currency,pricing_version_id,observed_at,
                          connector_account_id
                   FROM provider_usage_events WHERE brand_id=?
                   ORDER BY observed_at DESC,id DESC""", (brand_id,),
            )]
            prices = [dict(row) for row in connection.execute(
                """SELECT id,brand_id,version,provider,method,endpoint_pattern,
                          billable_category,unit_name,unit_price,currency,
                          effective_at,configured_by,created_at
                   FROM provider_pricing_versions WHERE brand_id IN (?, '*')
                   ORDER BY effective_at DESC,provider,method,endpoint_pattern,
                            billable_category,id""", (brand_id,)
            )]
        totals: dict[str, dict[str, Any]] = {}
        breakdowns: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        unpriced = 0
        for row in rows:
            key = ":".join((
                row["provider"], row["billable_category"],
                row["currency"] or "unpriced", row["unit_name"],
            ))
            total = totals.setdefault(key, {
                "provider": row["provider"],
                "billable_category": row["billable_category"],
                "currency": row["currency"], "unit_name": row["unit_name"],
                "requests": 0, "units": "0",
                "estimated_cost": None if row["currency"] is None else "0",
            })
            total["requests"] += 1
            total["units"] = str(Decimal(total["units"]) + Decimal(row["units"]))
            if row["estimated_cost"] is None:
                unpriced += 1
            else:
                total["estimated_cost"] = str(Decimal(total["estimated_cost"]) + Decimal(row["estimated_cost"]))
            breakdown_key = (
                row["provider"], row["billable_category"],
                row["currency"] or "unpriced", row["unit_name"],
            )
            breakdown = breakdowns.setdefault(breakdown_key, {
                "provider": row["provider"],
                "billable_category": row["billable_category"],
                "currency": row["currency"], "unit_name": row["unit_name"],
                "requests": 0, "units": "0",
                "estimated_cost": None if row["currency"] is None else "0",
            })
            breakdown["requests"] += 1
            breakdown["units"] = str(
                Decimal(breakdown["units"]) + Decimal(row["units"])
            )
            if row["estimated_cost"] is not None:
                breakdown["estimated_cost"] = str(
                    Decimal(breakdown["estimated_cost"])
                    + Decimal(row["estimated_cost"])
                )
        return {
            "brand_id": brand_id, "generated_at": self._now(),
            "request_count": len(rows), "unpriced_request_count": unpriced,
            "totals": list(totals.values()),
            "breakdowns": list(breakdowns.values()),
            "pricing_versions": prices,
            "events": rows,
        }

    def _matching_price(self, brand_id: str, provider: str, method: str, endpoint: str, category: str, at: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            candidates = [dict(row) for row in connection.execute(
                """SELECT * FROM provider_pricing_versions
                   WHERE brand_id IN (?, '*') AND provider=? AND method=? AND effective_at<=?
                   ORDER BY CASE WHEN brand_id=? THEN 0 ELSE 1 END,
                            effective_at DESC,created_at DESC""",
                (brand_id, provider.lower(), method.upper(), at, brand_id),
            )]
        return next((row for row in candidates if (
            fnmatchcase(endpoint, row["endpoint_pattern"])
            and fnmatchcase(category, row["billable_category"])
        )), None)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _now(self) -> str:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("clock must be timezone-aware")
        return value.astimezone(UTC).isoformat()


class MeteredTransport:
    """HTTP decorator that records only request metadata, never content or auth."""
    def __init__(self, delegate: HttpTransport, ledger: ProviderUsageLedger, *, brand_id: str, connector_account_id: str, provider: str, owned_user_id: str | None = None) -> None:
        self.delegate = delegate
        self.ledger = ledger
        self.brand_id = brand_id
        self.connector_account_id = connector_account_id
        self.provider = provider
        self.owned_user_id = owned_user_id

    def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
        try:
            response = self.delegate.request(method, url, **kwargs)
        except Exception:
            self.ledger.record(
                brand_id=self.brand_id, connector_account_id=self.connector_account_id,
                provider=self.provider, method=method, url=url, status_code=None,
                outcome="transport_error",
                billable_category=classify_request(
                    self.provider, method, url, kwargs.get("json_body"), self.owned_user_id,
                ),
            )
            raise
        resource_count = response_resource_count(response)
        category = classify_request(
            self.provider, method, url, kwargs.get("json_body"), self.owned_user_id,
        )
        self.ledger.record(
            brand_id=self.brand_id, connector_account_id=self.connector_account_id,
            provider=self.provider, method=method, url=url,
            status_code=response.status_code,
            units=str(resource_count if resource_count is not None else 1),
            billable_category=category,
            provider_request_id=_provider_request_id(response.headers),
            response_resource_count=resource_count,
        )
        return response


def safe_endpoint(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("provider URL must be HTTP(S)")
    host = parts.hostname.lower()
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", "", ""))


def classify_request(
    provider: str, method: str, url: str,
    json_body: Mapping[str, Any] | None = None, owned_user_id: str | None = None,
) -> str:
    """Classify billable shape without persisting request content."""
    path = urlsplit(url).path.rstrip("/") or "/"
    method = method.upper()
    if provider.lower() == "x":
        if method == "POST" and path == "/2/tweets":
            text = (json_body or {}).get("text") or (json_body or {}).get("body") or ""
            return "post.create_with_url" if isinstance(text, str) and re.search(r"https?://", text) else "post.create_plain"
        user_tweets = re.fullmatch(r"/2/users/([^/]+)/tweets", path)
        if method == "GET" and user_tweets:
            return "post.read_owned" if owned_user_id and user_tweets.group(1) == owned_user_id else "post.read_general"
        if method == "GET" and (path.startswith("/2/tweets") or "/tweets/search/" in path):
            return "post.read_general"
        if method == "GET" and path.startswith("/2/users"):
            return "user.read"
    if provider.lower() == "beehiiv":
        if method == "POST" and "/posts" in path:
            return "post.create"
        if method == "GET" and "/posts" in path:
            return "post.read"
    return "unclassified"


def response_resource_count(response: HttpResponse) -> int | None:
    try:
        document = response.json()
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    meta = document.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("result_count"), int):
        return max(0, meta["result_count"])
    data = document.get("data")
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return 1
    return None


def _provider_request_id(headers: Mapping[str, str]) -> str | None:
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    for key in ("x-request-id", "request-id", "x-correlation-id"):
        value = lowered.get(key)
        if value:
            return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
    return None


def _safe_pattern(value: str) -> str:
    value = _text(value, "endpoint_pattern")
    if "?" in value or "#" in value or "@" in value:
        raise ValueError("endpoint_pattern cannot contain query, fragment, or user info")
    return value


def _provider_pattern(provider: str, value: str) -> str:
    value = _safe_pattern(value)
    parts = urlsplit(value)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ValueError("endpoint_pattern must be an absolute HTTPS provider URL")
    expected_host = {
        "x": "api.x.com", "beehiiv": "api.beehiiv.com",
    }.get(provider.strip().lower())
    if expected_host and parts.hostname.lower() != expected_host:
        raise ValueError("endpoint_pattern host does not match provider")
    return value


def _decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("effective_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("effective_at must include timezone")
    return parsed.astimezone(UTC).isoformat()


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


__all__ = [
    "MeteredTransport", "ProviderUsageLedger", "classify_request",
    "response_resource_count", "safe_endpoint",
]
