"""Governed canonical-page snapshots for third-party source drift detection.

The fetcher accepts the existing connector transport, so production may use the
pinned-IP SSRF-safe HTTPS boundary and tests may inject a fake.  Classification
is deliberately limited to metadata and explicit numeric/date claim drift; it
does not claim semantic fact checking.
"""
from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import ssl
from typing import Any, Mapping
from uuid import uuid4

from . import store
from .connectors import (
    ConnectorError, HttpTransport, canonical_url, content_fingerprint, plain_text,
)


class CanonicalRevalidationError(ValueError):
    pass


class CanonicalSourceRevalidationStore:
    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS source_canonical_revalidations (
                  id TEXT PRIMARY KEY,brand_id TEXT NOT NULL,source_id TEXT NOT NULL,
                  canonical_url TEXT NOT NULL,observed_at TEXT NOT NULL,
                  snapshot_fingerprint TEXT NOT NULL,feed_fingerprint TEXT,
                  status TEXT NOT NULL CHECK(status IN ('verified','drift','conflict','unavailable')),
                  confidence TEXT NOT NULL,title TEXT NOT NULL,summary TEXT NOT NULL,
                  claims_json TEXT NOT NULL,rationale_json TEXT NOT NULL,
                  actor TEXT NOT NULL,idempotency_key TEXT NOT NULL,
                  request_fingerprint TEXT,created_at TEXT NOT NULL,
                  UNIQUE(brand_id,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS source_canonical_revalidation_latest
                  ON source_canonical_revalidations(brand_id,source_id,observed_at DESC,created_at DESC);
            """)
            columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(source_canonical_revalidations)"
                )
            }
            if "request_fingerprint" not in columns:
                connection.execute(
                    "ALTER TABLE source_canonical_revalidations ADD COLUMN request_fingerprint TEXT"
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def record(
        self, brand_id: str, source_id: str, snapshot: Mapping[str, Any], *, actor: str,
    ) -> dict[str, Any]:
        source_url = canonical_url(str(snapshot.get("canonical_url") or ""))
        if not source_url or not source_url.startswith("https://"):
            raise CanonicalRevalidationError("canonical revalidation requires a public HTTPS URL")
        status = str(snapshot.get("status") or "").strip()
        if status not in {"verified", "drift", "conflict", "unavailable"}:
            raise CanonicalRevalidationError("invalid canonical revalidation status")
        observed_at = _timestamp(str(snapshot.get("observed_at") or ""))
        fingerprint = str(snapshot.get("snapshot_fingerprint") or "").strip()
        if not fingerprint.startswith("sha256:"):
            raise CanonicalRevalidationError("snapshot fingerprint must be sha256-bound")
        rationale = [str(item) for item in (snapshot.get("rationale") or []) if str(item).strip()]
        if status in {"drift", "conflict", "unavailable"} and not rationale:
            raise CanonicalRevalidationError("non-verified revalidation requires rationale")
        idempotency_key = str(snapshot.get("idempotency_key") or fingerprint).strip()
        confidence = str(snapshot.get("confidence") or "low").strip()
        title = str(snapshot.get("title") or "")
        summary = str(snapshot.get("summary") or "")
        claims = list(snapshot.get("claims") or [])
        feed_fingerprint = str(snapshot.get("feed_fingerprint") or "") or None
        normalized_actor = actor.strip() or "connector"
        request_material = {
            "brand_id": brand_id, "source_id": source_id, "canonical_url": source_url,
            "observed_at": observed_at, "snapshot_fingerprint": fingerprint,
            "feed_fingerprint": feed_fingerprint, "status": status,
            "confidence": confidence, "title": title, "summary": summary,
            "claims": claims, "rationale": rationale, "actor": normalized_actor,
            "idempotency_key": idempotency_key,
        }
        request_fingerprint = "sha256:" + sha256(_json(request_material).encode()).hexdigest()
        timestamp = store.now()
        with self._connect() as connection:
            source = connection.execute(
                "SELECT * FROM sources WHERE id=? AND brand_id=?", (source_id, brand_id),
            ).fetchone()
            if source is None or canonical_url(source["url"]) != source_url:
                raise CanonicalRevalidationError("canonical snapshot does not match this brand source URL")
            existing = connection.execute(
                "SELECT * FROM source_canonical_revalidations WHERE brand_id=? AND idempotency_key=?",
                (brand_id, idempotency_key),
            ).fetchone()
            if existing:
                decoded = self._decode(existing)
                if existing["request_fingerprint"] is None and _legacy_auto_downgraded(decoded):
                    raise CanonicalRevalidationError(
                        "legacy revalidation replay cannot verify the original pre-downgrade "
                        "request because it was not fingerprinted; use a new idempotency key"
                    )
                bound = existing["request_fingerprint"] or _stored_request_fingerprint(decoded)
                if bound != request_fingerprint:
                    if existing["request_fingerprint"] is None:
                        raise CanonicalRevalidationError(
                            "legacy revalidation replay cannot verify the original complete "
                            "snapshot request because it was not fingerprinted; use a new "
                            "idempotency key"
                        )
                    raise CanonicalRevalidationError(
                        "revalidation idempotency key is already bound to a different complete snapshot request"
                    )
                if existing["request_fingerprint"] is None:
                    connection.execute(
                        "UPDATE source_canonical_revalidations SET request_fingerprint=? WHERE id=?",
                        (request_fingerprint, existing["id"]),
                    )
                    decoded["request_fingerprint"] = request_fingerprint
                return decoded
            prior = connection.execute(
                """SELECT snapshot_fingerprint FROM source_canonical_revalidations
                   WHERE brand_id=? AND source_id=? ORDER BY observed_at DESC,created_at DESC LIMIT 1""",
                (brand_id, source_id),
            ).fetchone()
            if prior and prior["snapshot_fingerprint"] != fingerprint and status == "verified":
                status = "drift"
                rationale.append("canonical metadata fingerprint changed since the prior snapshot")
            values = (
                str(uuid4()), brand_id, source_id, source_url, observed_at, fingerprint,
                feed_fingerprint, status, confidence, title, summary, _json(claims),
                _json(rationale), normalized_actor, idempotency_key, request_fingerprint, timestamp,
            )
            connection.execute(
                """INSERT INTO source_canonical_revalidations
                   (id,brand_id,source_id,canonical_url,observed_at,snapshot_fingerprint,
                    feed_fingerprint,status,confidence,title,summary,claims_json,rationale_json,
                    actor,idempotency_key,request_fingerprint,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values,
            )
            row = connection.execute(
                "SELECT * FROM source_canonical_revalidations WHERE id=?", (values[0],),
            ).fetchone()
        return self._decode(row)

    def latest(self, brand_id: str, source_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM source_canonical_revalidations WHERE brand_id=? AND source_id=?
                   ORDER BY observed_at DESC,created_at DESC,id DESC LIMIT 1""", (brand_id, source_id),
            ).fetchone()
        return self._decode(row) if row else None

    def list(self, brand_id: str, *, source_id: str | None = None,
             limit: int = 100) -> list[dict[str, Any]]:
        """Return tenant-scoped, payload-safe metadata snapshots newest first."""
        if limit < 1 or limit > 500:
            raise CanonicalRevalidationError("limit must be between 1 and 500")
        query = "SELECT * FROM source_canonical_revalidations WHERE brand_id=?"
        values: list[Any] = [brand_id]
        if source_id is not None:
            query += " AND source_id=?"
            values.append(source_id)
        query += " ORDER BY observed_at DESC,created_at DESC,id DESC LIMIT ?"
        values.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, tuple(values)).fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["claims"] = json.loads(result.pop("claims_json"))
        result["rationale"] = json.loads(result.pop("rationale_json"))
        result["evidence_scope"] = "canonical_metadata_only"
        result["semantic_fact_check"] = False
        return result


class CanonicalPageFetcher:
    """Fetch one canonical page through an injected connector transport."""

    def __init__(self, transport: HttpTransport) -> None:
        self.transport = transport

    def snapshot(
        self, url: str, *, feed_title: str = "", feed_summary: str = "",
        feed_fingerprint: str = "", observed_at: str | None = None,
    ) -> dict[str, Any]:
        target = canonical_url(url)
        if not target or not target.startswith("https://"):
            raise CanonicalRevalidationError("canonical page must use public HTTPS")
        response = self.transport.request(
            "GET", target, headers={"Accept": "text/html,application/xhtml+xml"},
        )
        if not 200 <= response.status_code < 300:
            return _snapshot(target, observed_at, "", "", [], feed_fingerprint,
                             "unavailable", "low", [f"canonical page returned HTTP {response.status_code}"])
        parser = _MetadataParser()
        try:
            parser.feed(response.body.decode("utf-8", errors="replace"))
        except Exception as exc:
            raise CanonicalRevalidationError("canonical page metadata could not be parsed") from exc
        title, summary = plain_text(parser.title), plain_text(parser.description)
        if not title and not summary:
            return _snapshot(
                target, observed_at, "", "", [], feed_fingerprint,
                "unavailable", "low",
                ["canonical page returned 2xx but contained no usable title or description metadata"],
                snapshot_fingerprint="sha256:" + sha256(response.body).hexdigest(),
            )
        canonical_claims = _claims(f"{title} {summary}")
        feed_claims = _claims(f"{feed_title} {feed_summary}")
        conflicts = sorted(set(feed_claims) - set(canonical_claims)) if feed_claims else []
        status = "conflict" if conflicts else "verified"
        rationale = (
            ["feed numeric/date claims absent from current canonical metadata: " + ", ".join(conflicts)]
            if conflicts else ["canonical title/description snapshot captured; semantic fact checking still required"]
        )
        return _snapshot(target, observed_at, title, summary, canonical_claims,
                         feed_fingerprint, status, "medium", rationale)

    def safe_snapshot(
        self, url: str, *, feed_title: str = "", feed_summary: str = "",
        feed_fingerprint: str = "", observed_at: str | None = None,
    ) -> dict[str, Any]:
        """Return sanitized unavailable evidence for bounded retrieval failures.

        This method intentionally never turns page metadata into a semantic fact
        check. DNS, socket, TLS, timeout, and connector failures are reduced to a
        stable operator-safe rationale that cannot persist exception text.
        """
        target = canonical_url(url)
        if not target or not target.startswith("https://"):
            raise CanonicalRevalidationError("canonical page must use public HTTPS")
        try:
            return self.snapshot(
                target, feed_title=feed_title, feed_summary=feed_summary,
                feed_fingerprint=feed_fingerprint, observed_at=observed_at,
            )
        except (ConnectorError, TimeoutError, OSError, ssl.SSLError):
            return unavailable_snapshot(
                target, observed_at=observed_at, feed_fingerprint=feed_fingerprint,
            )


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_title = False
        self.title = ""
        self.description = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "title":
            self._in_title = True
        if tag.casefold() == "meta" and values.get("name", "").casefold() == "description":
            self.description = values.get("content", "")
        if tag.casefold() == "meta" and values.get("property", "").casefold() == "og:title":
            self.title = values.get("content", "") or self.title

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title += data


def _claims(value: str) -> list[str]:
    patterns = (
        r"(?<!\w)(?:\$[\d,]+|[\d,]+(?:\.\d+)?%|[\d,]+\s+(?:points|miles))(?!\w)",
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:,\s*\d{4})?\b",
    )
    return sorted({match.casefold() for pattern in patterns for match in re.findall(pattern, value, re.I)})


def _stored_request_fingerprint(item: Mapping[str, Any]) -> str:
    material = {
        key: item.get(key) for key in (
            "brand_id", "source_id", "canonical_url", "observed_at",
            "snapshot_fingerprint", "feed_fingerprint", "status", "confidence",
            "title", "summary", "claims", "rationale", "actor", "idempotency_key",
        )
    }
    return "sha256:" + sha256(_json(material).encode()).hexdigest()


def _legacy_auto_downgraded(item: Mapping[str, Any]) -> bool:
    return item.get("status") == "drift" and any(
        rationale == "canonical metadata fingerprint changed since the prior snapshot"
        for rationale in item.get("rationale", [])
    )


def _snapshot(
    url: str, observed_at: str | None, title: str, summary: str, claims: list[str],
    feed_fingerprint: str, status: str, confidence: str, rationale: list[str], *,
    snapshot_fingerprint: str | None = None,
) -> dict[str, Any]:
    moment = _timestamp(observed_at or datetime.now(UTC).isoformat())
    fingerprint = snapshot_fingerprint or "sha256:" + content_fingerprint(title, summary)
    return {
        "canonical_url": url, "observed_at": moment, "snapshot_fingerprint": fingerprint,
        "feed_fingerprint": feed_fingerprint or None, "status": status,
        "confidence": confidence, "title": title, "summary": summary, "claims": claims,
        "rationale": rationale, "idempotency_key": sha256(
            f"{url}:{moment}:{fingerprint}".encode()
        ).hexdigest(),
    }


def unavailable_snapshot(
    url: str, *, observed_at: str | None = None, feed_fingerprint: str = "",
) -> dict[str, Any]:
    """Build low-confidence evidence without copying provider exception details."""
    return _snapshot(
        url, observed_at, "", "", [], feed_fingerprint, "unavailable", "low",
        ["canonical page was unavailable during bounded HTTPS retrieval"],
        snapshot_fingerprint="sha256:" + sha256(
            f"unavailable:{url}:{observed_at or ''}:{feed_fingerprint}".encode()
        ).hexdigest(),
    )


def _timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CanonicalRevalidationError("revalidation timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CanonicalRevalidationError("revalidation timestamp must include timezone")
    return parsed.astimezone(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


__all__ = [
    "CanonicalPageFetcher", "CanonicalRevalidationError",
    "CanonicalSourceRevalidationStore", "unavailable_snapshot",
]
