"""Durable tracked links, inbound attribution, and evidence-backed KPI values."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from brandman.mission_ops import build_utm_identity, generate_utm_url, parse_utm_identity


class AttributionStoreError(RuntimeError):
    """Base error for attribution persistence and policy failures."""


class TrackedLinkConflict(AttributionStoreError):
    """An idempotency key or canonical identity was reused inconsistently."""


class KpiEvidenceRequired(AttributionStoreError):
    """A KPI observation lacks evidence needed to become canonical."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS attribution_schema_migrations (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracked_links (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL, brand_slug TEXT,
  campaign_id TEXT NOT NULL, artifact_id TEXT NOT NULL, cta_id TEXT NOT NULL,
  source TEXT NOT NULL, medium TEXT NOT NULL, destination TEXT NOT NULL,
  tracked_url TEXT NOT NULL, identity_key TEXT NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL UNIQUE, lifecycle TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CHECK(lifecycle IN ('active','deprecated'))
);
CREATE INDEX IF NOT EXISTS tracked_links_lookup
  ON tracked_links(campaign_id, artifact_id, cta_id, source, medium);
CREATE INDEX IF NOT EXISTS tracked_links_brand_lifecycle
  ON tracked_links(brand_id, lifecycle, updated_at DESC);
CREATE TABLE IF NOT EXISTS tracked_link_audit (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, link_id TEXT NOT NULL,
  action TEXT NOT NULL, actor TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL, FOREIGN KEY(link_id) REFERENCES tracked_links(id)
);
CREATE TABLE IF NOT EXISTS kpi_evidence_records (
  id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, metric TEXT NOT NULL,
  value REAL NOT NULL, observed_at TEXT NOT NULL, source TEXT NOT NULL,
  evidence_type TEXT NOT NULL, connector_account_id TEXT,
  connector_event_id TEXT, human_verified_by TEXT,
  human_verification_note TEXT, verified_at TEXT NOT NULL,
  dimensions TEXT NOT NULL DEFAULT '{}', idempotency_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  CHECK(evidence_type IN ('connector_event','human_manual')),
  CHECK(
    (evidence_type='connector_event' AND connector_account_id IS NOT NULL
      AND connector_event_id IS NOT NULL AND human_verified_by IS NULL)
    OR
    (evidence_type='human_manual' AND connector_account_id IS NULL
      AND connector_event_id IS NULL AND human_verified_by IS NOT NULL)
  )
);
CREATE UNIQUE INDEX IF NOT EXISTS kpi_connector_evidence_unique
  ON kpi_evidence_records(connector_account_id, connector_event_id, metric)
  WHERE evidence_type='connector_event';
CREATE INDEX IF NOT EXISTS kpi_evidence_timeline
  ON kpi_evidence_records(mission_id, metric, observed_at DESC);
CREATE TABLE IF NOT EXISTS canonical_kpi_values (
  mission_id TEXT NOT NULL, metric TEXT NOT NULL, evidence_record_id TEXT NOT NULL,
  value REAL NOT NULL, observed_at TEXT NOT NULL, source TEXT NOT NULL,
  promoted_by TEXT NOT NULL, promoted_at TEXT NOT NULL,
  PRIMARY KEY(mission_id, metric),
  FOREIGN KEY(evidence_record_id) REFERENCES kpi_evidence_records(id)
);
CREATE TABLE IF NOT EXISTS canonical_kpi_audit (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT NOT NULL,
  metric TEXT NOT NULL, evidence_record_id TEXT NOT NULL, action TEXT NOT NULL,
  actor TEXT NOT NULL, at TEXT NOT NULL,
  FOREIGN KEY(evidence_record_id) REFERENCES kpi_evidence_records(id)
);
"""


_UTM_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_cta", "utm_brand"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be JSON-persistable") from exc


def _timestamp(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return str(value)


def _finite(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("KPI value must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("KPI value must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError("KPI value must be a finite number")
    return result


def _destination_without_utm(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ValueError("destination must be an absolute HTTP(S) URL")
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _UTM_KEYS
    )
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), ""))


def _link_identity_key(values: Mapping[str, Any]) -> str:
    material = {key: values.get(key) for key in (
        "brand_id", "brand_slug", "campaign_id", "artifact_id", "cta_id", "source", "medium"
    )}
    return sha256(_json(material).encode()).hexdigest()


def _decode(row: sqlite3.Row | None, *json_fields: str) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for field in json_fields:
        result[field] = json.loads(result[field])
    return result


class AttributionStore:
    """Additive SQLite store for durable links and trustworthy current KPIs."""

    def __init__(self, database: str | Path, *, clock: Callable[[], str] = _now) -> None:
        self.database = str(database)
        self.clock = clock
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def init_schema(self) -> None:
        if self.database != ":memory:":
            Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO attribution_schema_migrations(version, applied_at) VALUES (1, ?)",
                (self.clock(),),
            )

    def create_tracked_link(
        self, *, brand_id: str, campaign_id: str, artifact_id: str, cta_id: str,
        source: str, medium: str, destination: str, brand_slug: str | None = None,
        idempotency_key: str | None = None, actor: str = "system",
    ) -> dict[str, Any]:
        if not brand_id.strip() or not actor.strip():
            raise ValueError("brand_id and actor are required")
        identity = build_utm_identity(
            source=source, medium=medium, campaign_id=campaign_id,
            artifact_id=artifact_id, cta_id=cta_id, brand_slug=brand_slug,
        )
        canonical_destination = _destination_without_utm(destination)
        values: dict[str, Any] = {
            "brand_id": brand_id, "brand_slug": identity.brand_slug,
            "campaign_id": identity.campaign_id, "artifact_id": identity.artifact_id,
            "cta_id": identity.cta_id, "source": identity.source, "medium": identity.medium,
            "destination": canonical_destination,
        }
        values["identity_key"] = _link_identity_key(values)
        values["idempotency_key"] = idempotency_key or f"tracked-link:{values['identity_key']}"
        values["tracked_url"] = generate_utm_url(canonical_destination, identity)
        values["id"] = str(uuid4())
        values["created_at"] = values["updated_at"] = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM tracked_links WHERE idempotency_key=? OR identity_key=?",
                (values["idempotency_key"], values["identity_key"]),
            ).fetchone()
            if existing:
                same = all(existing[key] == values[key] for key in (
                    "brand_id", "brand_slug", "campaign_id", "artifact_id", "cta_id", "source",
                    "medium", "destination", "idempotency_key", "identity_key",
                ))
                if not same:
                    raise TrackedLinkConflict("link identity or idempotency key is already bound differently")
                return dict(existing)
            connection.execute(
                """INSERT INTO tracked_links
                   (id,brand_id,brand_slug,campaign_id,artifact_id,cta_id,source,medium,
                    destination,tracked_url,identity_key,idempotency_key,lifecycle,created_at,updated_at)
                   VALUES (:id,:brand_id,:brand_slug,:campaign_id,:artifact_id,:cta_id,
                    :source,:medium,:destination,:tracked_url,:identity_key,:idempotency_key,
                    'active',:created_at,:updated_at)""",
                values,
            )
            self._audit_link(connection, values["id"], "created", actor, "")
        return self.get_tracked_link(values["id"])

    def get_tracked_link(self, link_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tracked_links WHERE id=?", (link_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown tracked link: {link_id}")
        return dict(row)

    def list_tracked_links(
        self, brand_id: str, *, lifecycle: str | None = None, campaign_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if lifecycle not in {None, "active", "deprecated"}:
            raise ValueError("lifecycle must be active or deprecated")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        clauses, values = ["brand_id=?"], [brand_id]
        if lifecycle:
            clauses.append("lifecycle=?")
            values.append(lifecycle)
        if campaign_id:
            clauses.append("campaign_id=?")
            values.append(campaign_id)
        values.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tracked_links WHERE " + " AND ".join(clauses)
                + " ORDER BY updated_at DESC, id LIMIT ?", values,
            ).fetchall()
        return [dict(row) for row in rows]

    def set_link_lifecycle(
        self, link_id: str, lifecycle: str, *, actor: str, detail: str = "",
    ) -> dict[str, Any]:
        if lifecycle not in {"active", "deprecated"}:
            raise ValueError("lifecycle must be active or deprecated")
        if not actor.strip():
            raise ValueError("actor is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            found = connection.execute("SELECT lifecycle FROM tracked_links WHERE id=?", (link_id,)).fetchone()
            if found is None:
                raise KeyError(f"unknown tracked link: {link_id}")
            if found["lifecycle"] != lifecycle:
                connection.execute(
                    "UPDATE tracked_links SET lifecycle=?, updated_at=? WHERE id=?",
                    (lifecycle, self.clock(), link_id),
                )
                self._audit_link(connection, link_id, lifecycle, actor, detail)
        return self.get_tracked_link(link_id)

    def list_link_audit(self, link_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tracked_link_audit WHERE link_id=? ORDER BY sequence", (link_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_inbound_attribution(self, url: str) -> dict[str, Any]:
        parsed = parse_utm_identity(url)
        complete = all(parsed[key] for key in ("campaign_id", "artifact_id", "cta_id"))
        if complete:
            brand_clause = " AND brand_slug=?" if parsed["brand_slug"] else ""
            parameters: list[Any] = [
                parsed["campaign_id"], parsed["artifact_id"], parsed["cta_id"],
                parsed["source"], parsed["medium"],
            ]
            if parsed["brand_slug"]:
                parameters.append(parsed["brand_slug"])
            with self._connect() as connection:
                rows = connection.execute(
                    """SELECT * FROM tracked_links WHERE campaign_id=? AND artifact_id=?
                       AND cta_id=? AND source=? AND medium=?""" + brand_clause
                    + " ORDER BY lifecycle='active' DESC, updated_at DESC LIMIT 2",
                    parameters,
                ).fetchall()
            # Without a brand slug, coincident identities across brands are not
            # enough evidence for a direct attribution.
            if len(rows) == 1:
                return {**parsed, "confidence": "direct", "tracked_link": dict(rows[0]), "reason": "canonical_link_match"}
        if any(parsed[key] for key in ("source", "medium", "campaign_id", "artifact_id", "cta_id")):
            return {**parsed, "confidence": "assisted", "tracked_link": None, "reason": "partial_or_unknown_tracking"}
        return {**parsed, "confidence": "unattributed", "tracked_link": None, "reason": "no_tracking_identity"}

    def record_kpi_evidence(
        self, *, mission_id: str, metric: str, value: float, observed_at: str,
        source: str, connector_account_id: str | None = None,
        connector_event_id: str | None = None, human_manual: bool = False,
        human_verified_by: str | None = None, human_verification_note: str | None = None,
        dimensions: Mapping[str, Any] | None = None, idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not all(str(item).strip() for item in (mission_id, metric, source)):
            raise ValueError("mission_id, metric, and source are required")
        observed_at = _timestamp(observed_at, "observed_at")
        if human_manual:
            if connector_account_id or connector_event_id or not (human_verified_by or "").strip():
                raise KpiEvidenceRequired("human_manual evidence requires a verifier and no connector IDs")
            evidence_type = "human_manual"
        else:
            if not (connector_account_id or "").strip() or not (connector_event_id or "").strip():
                raise KpiEvidenceRequired("connector evidence requires connector_account_id and connector_event_id")
            if human_verified_by:
                raise KpiEvidenceRequired("connector evidence cannot masquerade as human verification")
            evidence_type = "connector_event"
        key_material = {
            "mission_id": mission_id, "metric": metric, "observed_at": observed_at,
            "source": source, "connector_account_id": connector_account_id,
            "connector_event_id": connector_event_id, "human_verified_by": human_verified_by,
        }
        key = idempotency_key or "kpi-evidence:" + sha256(_json(key_material).encode()).hexdigest()
        record = {
            "id": str(uuid4()), "mission_id": mission_id, "metric": metric,
            "value": _finite(value), "observed_at": observed_at, "source": source,
            "evidence_type": evidence_type, "connector_account_id": connector_account_id,
            "connector_event_id": connector_event_id, "human_verified_by": human_verified_by,
            "human_verification_note": human_verification_note, "verified_at": self.clock(),
            "dimensions": _json(dict(dimensions or {})), "idempotency_key": key,
            "created_at": self.clock(),
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM kpi_evidence_records WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                comparable = ("mission_id", "metric", "value", "observed_at", "source", "evidence_type",
                              "connector_account_id", "connector_event_id", "human_verified_by", "dimensions")
                if not all(existing[field] == record[field] for field in comparable):
                    raise KpiEvidenceRequired("KPI evidence idempotency key is already bound differently")
                decoded = _decode(existing, "dimensions")
                assert decoded is not None
                return decoded
            try:
                connection.execute(
                    """INSERT INTO kpi_evidence_records
                       (id,mission_id,metric,value,observed_at,source,evidence_type,
                        connector_account_id,connector_event_id,human_verified_by,
                        human_verification_note,verified_at,dimensions,idempotency_key,created_at)
                       VALUES (:id,:mission_id,:metric,:value,:observed_at,:source,:evidence_type,
                        :connector_account_id,:connector_event_id,:human_verified_by,
                        :human_verification_note,:verified_at,:dimensions,:idempotency_key,:created_at)""",
                    record,
                )
            except sqlite3.IntegrityError as exc:
                raise KpiEvidenceRequired("connector event is already evidence for this KPI") from exc
        record["dimensions"] = dict(dimensions or {})
        return record

    def promote_kpi_evidence(self, evidence_record_id: str, *, actor: str) -> dict[str, Any]:
        if not actor.strip():
            raise ValueError("actor is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            evidence = connection.execute("SELECT * FROM kpi_evidence_records WHERE id=?", (evidence_record_id,)).fetchone()
            if evidence is None:
                raise KeyError(f"unknown KPI evidence record: {evidence_record_id}")
            current = connection.execute(
                "SELECT * FROM canonical_kpi_values WHERE mission_id=? AND metric=?",
                (evidence["mission_id"], evidence["metric"]),
            ).fetchone()
            if current is not None:
                proposed_time = datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00"))
                current_time = datetime.fromisoformat(current["observed_at"].replace("Z", "+00:00"))
                if proposed_time < current_time:
                    raise KpiEvidenceRequired("stale KPI evidence cannot replace a newer canonical value")
            timestamp = self.clock()
            connection.execute(
                """INSERT INTO canonical_kpi_values
                   (mission_id,metric,evidence_record_id,value,observed_at,source,promoted_by,promoted_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(mission_id,metric) DO UPDATE SET
                    evidence_record_id=excluded.evidence_record_id, value=excluded.value,
                    observed_at=excluded.observed_at, source=excluded.source,
                    promoted_by=excluded.promoted_by, promoted_at=excluded.promoted_at""",
                (evidence["mission_id"], evidence["metric"], evidence["id"], evidence["value"],
                 evidence["observed_at"], evidence["source"], actor, timestamp),
            )
            connection.execute(
                """INSERT INTO canonical_kpi_audit
                   (mission_id,metric,evidence_record_id,action,actor,at)
                   VALUES (?,?,?,'promoted',?,?)""",
                (evidence["mission_id"], evidence["metric"], evidence["id"], actor, timestamp),
            )
        current = self.get_canonical_kpi(evidence["mission_id"], evidence["metric"])
        assert current is not None
        return current

    def get_canonical_kpi(self, mission_id: str, metric: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT c.*, e.evidence_type, e.connector_account_id,
                   e.connector_event_id, e.human_verified_by, e.human_verification_note,
                   e.verified_at, e.dimensions
                   FROM canonical_kpi_values c JOIN kpi_evidence_records e
                   ON e.id=c.evidence_record_id WHERE c.mission_id=? AND c.metric=?""",
                (mission_id, metric),
            ).fetchone()
        return _decode(row, "dimensions")

    def list_kpi_evidence(self, mission_id: str, metric: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        query, values = "SELECT * FROM kpi_evidence_records WHERE mission_id=?", [mission_id]
        if metric:
            query += " AND metric=?"
            values.append(metric)
        query += " ORDER BY observed_at DESC, created_at DESC LIMIT ?"
        values.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [_decode(row, "dimensions") for row in rows]  # type: ignore[misc]

    def list_kpi_audit(self, mission_id: str, metric: str | None = None) -> list[dict[str, Any]]:
        query, values = "SELECT * FROM canonical_kpi_audit WHERE mission_id=?", [mission_id]
        if metric:
            query += " AND metric=?"
            values.append(metric)
        query += " ORDER BY sequence"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, values).fetchall()]

    @staticmethod
    def _audit_link(connection: sqlite3.Connection, link_id: str, action: str, actor: str, detail: str) -> None:
        timestamp = connection.execute("SELECT updated_at FROM tracked_links WHERE id=?", (link_id,)).fetchone()[0]
        connection.execute(
            "INSERT INTO tracked_link_audit(link_id,action,actor,detail,at) VALUES (?,?,?,?,?)",
            (link_id, action, actor, detail, timestamp),
        )
