"""Project authenticated first-party conversions onto exact tracked assets."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from datetime import datetime

from app.campaign_graph import CampaignGraphStore
from app.connectors import ConnectorEvent, ConnectorKind, EventKind


SCHEMA = """
CREATE TABLE IF NOT EXISTS website_event_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  brand_id TEXT NOT NULL, connector_account_id TEXT NOT NULL,
  connector_event_id TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL,
  observed_at TEXT NOT NULL, session_ref TEXT, authenticated INTEGER,
  tool_key TEXT, deployment_id TEXT, deployment_revision TEXT, environment TEXT,
  campaign_id TEXT, asset_membership_id TEXT, tracked_link_id TEXT,
  cta_id TEXT, attribution_source TEXT, attribution_confidence TEXT,
  value INTEGER NOT NULL DEFAULT 0,
  UNIQUE(connector_account_id,event_type,connector_event_id)
);
CREATE INDEX IF NOT EXISTS website_event_observations_session
  ON website_event_observations(brand_id,connector_account_id,session_ref,event_type);
CREATE INDEX IF NOT EXISTS website_event_observations_campaign
  ON website_event_observations(brand_id,campaign_id,asset_membership_id,event_type);
"""


class WebsiteCampaignMetricProjector:
    """Attach only exact tracked-link conversions to a campaign asset.

    Explicitly unattributed conversions remain durable connector/performance
    evidence, but are intentionally not assigned to any campaign.
    """

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        self.graph = CampaignGraphStore(database)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def project(
        self, *, brand_id: str, connector_account_id: str,
        connector_event: Mapping[str, Any], event: ConnectorEvent,
    ) -> int:
        if event.connector != ConnectorKind.WEBSITE:
            return 0
        event_type = self._event_type(event)
        if event_type not in {
            "authenticated_session", "tool_use", "conversion", "deployment_changed",
        }:
            return 0
        if not (
            event.occurred_at and connector_event.get("id")
            and connector_event.get("connector_account_id") == connector_account_id
        ):
            raise ValueError("website event requires an authenticated canonical connector event")
        expected_payload = {**dict(event.payload), "provider_external_id": event.external_id}
        if connector_event.get("payload") != expected_payload:
            raise ValueError("website connector event identity is already bound to different material")
        exact = self._resolve_exact_attribution(
            brand_id=brand_id, connector_account_id=connector_account_id, event=event,
        )
        value = 0 if event_type == "deployment_changed" else _count(
            event.payload.get("value"), f"website {event_type} value",
        )
        with self._connect() as connection:
            inserted = connection.execute(
                """INSERT OR IGNORE INTO website_event_observations
                   (brand_id,connector_account_id,connector_event_id,event_type,observed_at,
                    session_ref,authenticated,tool_key,deployment_id,deployment_revision,
                    environment,campaign_id,asset_membership_id,tracked_link_id,cta_id,
                    attribution_source,attribution_confidence,value)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    brand_id, connector_account_id, connector_event["id"], event_type,
                    event.occurred_at, event.payload.get("session_ref"),
                    int(event.payload.get("authenticated"))
                    if isinstance(event.payload.get("authenticated"), bool) else None,
                    event.payload.get("tool_key"), event.payload.get("deployment_id"),
                    event.payload.get("deployment_revision"), event.payload.get("environment"),
                    exact.get("campaign_id"), exact.get("membership_id"),
                    exact.get("tracked_link_id"), exact.get("cta_id"), exact.get("source"),
                    event.payload.get("attribution_confidence"), value,
                ),
            ).rowcount
        if not inserted or event_type != "conversion":
            return 0
        confidence = event.payload.get("attribution_confidence")
        if confidence == "unattributed":
            return 0
        if confidence != "tracked_link_exact":
            raise ValueError(
                "website conversion must be explicitly unattributed or bound to an exact tracked link"
            )
        campaign_id = exact["campaign_id"]
        tracked_link_id = exact["tracked_link_id"]
        metric = str(event.payload.get("metric") or "")
        conversions = value if metric in {"conversion", "newsletter_conversion"} else 0
        revenue_cents = value if metric == "revenue" else 0
        if not conversions and not revenue_cents:
            return 0
        key = f"website-campaign-metric:{exact['membership_id']}:{connector_event['id']}"
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM campaign_asset_metric_observations WHERE idempotency_key=?",
                (key,),
            ).fetchone() is not None:
                return 0
        self.graph.record_metric(
            exact["membership_id"], observed_at=event.occurred_at,
            native_metrics={}, conversions=conversions,
            revenue_cents=revenue_cents,
            attribution_confidence="tracked_link_exact",
            idempotency_key=key,
        )
        return 1

    def validate(
        self, *, brand_id: str, connector_account_id: str, event: ConnectorEvent,
    ) -> None:
        """Fail before any projection when an attribution claim is malformed."""
        if event.connector != ConnectorKind.WEBSITE:
            return
        event_type = self._event_type(event)
        if event_type not in {
            "authenticated_session", "tool_use", "conversion", "deployment_changed",
        }:
            return
        if not event.occurred_at:
            raise ValueError("website event requires observed_at")
        try:
            observed = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise ValueError("website event observed_at must be an ISO-8601 timestamp") from None
        if observed.tzinfo is None:
            raise ValueError("website event observed_at must include a timezone")
        self._require_account(brand_id, connector_account_id)
        if event_type == "deployment_changed":
            if event.kind != EventKind.DEPLOYMENT_CHANGED or not all(
                str(event.payload.get(name) or "").strip()
                for name in ("deployment_id", "deployment_revision", "environment")
            ):
                raise ValueError("website deployment change is incomplete")
            if any(event.payload.get(name) for name in (
                "brand_id", "campaign_id", "asset_id", "tracked_link_id", "cta_id",
                "source", "session_ref", "tool_key",
            )):
                raise ValueError("website deployment change cannot claim funnel attribution")
            return
        value = _count(event.payload.get("value"), f"website {event_type} value")
        if event_type == "authenticated_session":
            if (
                value != 1 or event.payload.get("authenticated") is not True
                or not _session_ref_valid(event.payload.get("session_ref"))
            ):
                raise ValueError("website authenticated session evidence is incomplete")
            if any(event.payload.get(name) for name in (
                "brand_id", "campaign_id", "asset_id", "tracked_link_id", "cta_id", "source",
            )):
                raise ValueError("website authenticated session cannot claim campaign attribution")
            return
        if event_type == "tool_use" and not (
            event.payload.get("authenticated") is True
            and _session_ref_valid(event.payload.get("session_ref"))
            and str(event.payload.get("tool_key") or "").strip()
        ):
            raise ValueError("website tool use requires an authenticated session and tool_key")
        confidence = event.payload.get("attribution_confidence")
        metric = str(event.payload.get("metric") or "")
        supported = (
            {"tool_use"} if event_type == "tool_use"
            else {"conversion", "newsletter_conversion", "revenue"}
        )
        if metric not in supported:
            raise ValueError(f"website {event_type} metric is unsupported")
        identifiers = tuple(
            str(event.payload.get(name) or "").strip()
            for name in (
                "brand_id", "campaign_id", "asset_id", "tracked_link_id", "cta_id", "source",
            )
        )
        if confidence == "unattributed":
            # Legacy metric-shaped conversion events predate the explicit
            # event_type contract and carry only the original three fields.
            legacy = str(event.payload.get("event_type") or "metric") == "metric"
            checked = identifiers[1:4] if legacy else identifiers
            if any(checked):
                raise ValueError(f"unattributed website {event_type} cannot name campaign attribution")
            return
        legacy = str(event.payload.get("event_type") or "metric") == "metric"
        required = identifiers[1:4] if legacy else identifiers
        if confidence != "tracked_link_exact" or not all(required):
            raise ValueError(
                f"website {event_type} must be explicitly unattributed or bound to an exact tracked link"
            )
        self._resolve_exact_attribution(
            brand_id=brand_id, connector_account_id=connector_account_id, event=event,
        )

    def validate_page(
        self, *, brand_id: str, connector_account_id: str,
        stream: str | None = None,
        events: tuple[ConnectorEvent, ...] | list[ConnectorEvent],
    ) -> None:
        """Validate all attribution and session references before the first write."""
        for event in events:
            self.validate(
                brand_id=brand_id, connector_account_id=connector_account_id, event=event,
            )
        # A provider identity is account-wide. Preflight every identity and its
        # immutable material before projection so a conflict later in the page
        # cannot leave earlier connector or metric writes behind.
        page_fingerprints: dict[str, str] = {}
        website_events = [
            event for event in events if event.connector == ConnectorKind.WEBSITE
        ]
        for event in website_events:
            fingerprint = _event_material_fingerprint(
                event_kind=event.kind.value,
                provider_external_id=event.external_id,
                observed_at=event.occurred_at,
                payload={**dict(event.payload), "provider_external_id": event.external_id},
            )
            prior = page_fingerprints.setdefault(event.dedup_key, fingerprint)
            if prior != fingerprint:
                raise ValueError(
                    "website provider event identity is already bound to different material"
                )
        if page_fingerprints:
            placeholders = ",".join("?" for _ in page_fingerprints)
            with self._connect() as connection:
                stored_events = connection.execute(
                    f"""SELECT external_id,event_type,payload,observed_at
                        FROM connector_events
                        WHERE connector_account_id=? AND external_id IN ({placeholders})""",
                    (connector_account_id, *page_fingerprints),
                ).fetchall()
            for stored in stored_events:
                try:
                    stored_payload = json.loads(stored["payload"])
                except (TypeError, ValueError):
                    raise ValueError(
                        "website provider event identity has invalid stored material"
                    ) from None
                stored_fingerprint = _event_material_fingerprint(
                    event_kind=stored["event_type"],
                    provider_external_id=stored_payload.get("provider_external_id"),
                    observed_at=stored["observed_at"],
                    payload=stored_payload,
                )
                if page_fingerprints[stored["external_id"]] != stored_fingerprint:
                    raise ValueError(
                        "website provider event identity is already bound to different material"
                    )
        page_sessions: dict[str, list[datetime]] = {}
        for event in events:
            if not (
                self._event_type(event) == "authenticated_session"
                and event.payload.get("authenticated") is True
            ):
                continue
            page_sessions.setdefault(str(event.payload.get("session_ref")), []).append(
                datetime.fromisoformat(str(event.occurred_at).replace("Z", "+00:00"))
            )
        tool_events = [
            event
            for event in events
            if self._event_type(event) == "tool_use"
        ]
        for tool in tool_events:
            session_ref = str(tool.payload.get("session_ref"))
            tool_at = datetime.fromisoformat(str(tool.occurred_at).replace("Z", "+00:00"))
            known_in_page = any(
                session_at <= tool_at for session_at in page_sessions.get(session_ref, [])
            )
            if known_in_page:
                continue
            with self._connect() as connection:
                prior = connection.execute(
                        """SELECT observed_at FROM website_event_observations
                           WHERE brand_id=? AND connector_account_id=?
                             AND event_type='authenticated_session' AND authenticated=1
                             AND session_ref=?""",
                        (brand_id, connector_account_id, session_ref),
                    ).fetchall()
            known_before_tool = any(
                datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00")) <= tool_at
                for row in prior
            )
            if not known_before_tool:
                raise ValueError(
                    "website tool use references an unknown authenticated session or one observed later"
                )

    @staticmethod
    def _event_type(event: ConnectorEvent) -> str:
        value = str(event.payload.get("event_type") or "metric")
        if event.kind == EventKind.DEPLOYMENT_CHANGED:
            return "deployment_changed"
        if value == "metric" and event.payload.get("evidence_type") == "website_conversion":
            return "conversion"
        return value

    def _require_account(self, brand_id: str, connector_account_id: str) -> None:
        with self._connect() as connection:
            account = connection.execute(
                """SELECT 1 FROM connector_accounts
                   WHERE id=? AND brand_id=? AND connector_type='website'
                     AND status IN ('healthy','connected','active')""",
                (connector_account_id, brand_id),
            ).fetchone()
        if account is None:
            raise ValueError("website event connector is not healthy for this brand")

    def _resolve_exact_attribution(
        self, *, brand_id: str, connector_account_id: str, event: ConnectorEvent,
    ) -> dict[str, Any]:
        self._require_account(brand_id, connector_account_id)
        if self._event_type(event) in {"authenticated_session", "deployment_changed"}:
            return {}
        if event.payload.get("attribution_confidence") == "unattributed":
            return {}
        campaign_id = str(event.payload.get("campaign_id") or "").strip()
        asset_id = str(event.payload.get("asset_id") or event.payload.get("post_id") or "").strip()
        tracked_link_id = str(event.payload.get("tracked_link_id") or "").strip()
        with self._connect() as connection:
            campaign = connection.execute(
                "SELECT 1 FROM campaigns WHERE id=? AND brand_id=?",
                (campaign_id, brand_id),
            ).fetchone()
            link = connection.execute(
                """SELECT cta_id,source FROM tracked_links
                   WHERE id=? AND brand_id=? AND campaign_id=? AND artifact_id=?
                     AND lifecycle='active'""",
                (tracked_link_id, brand_id, campaign_id, asset_id),
            ).fetchone()
            membership = connection.execute(
                """SELECT id FROM campaign_asset_memberships
                   WHERE campaign_id=? AND asset_id=? AND active=1""",
                (campaign_id, asset_id),
            ).fetchone()
        if campaign is None or link is None or membership is None:
            raise ValueError("website event does not match an active tracked campaign asset")
        supplied_brand = str(event.payload.get("brand_id") or "").strip()
        supplied_cta = str(event.payload.get("cta_id") or "").strip()
        supplied_source = str(event.payload.get("source") or "").strip()
        explicit_contract = str(event.payload.get("event_type") or "metric") != "metric"
        if explicit_contract and (
            supplied_brand != brand_id
            or supplied_cta != str(link["cta_id"])
            or supplied_source != str(link["source"])
        ):
            raise ValueError("website event tracked CTA or source attribution does not match")
        return {
            "campaign_id": campaign_id, "membership_id": membership["id"],
            "tracked_link_id": tracked_link_id, "cta_id": link["cta_id"],
            "source": link["source"],
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


class CompositeCampaignMetricProjector:
    def __init__(self, *projectors: Any) -> None:
        self.projectors = projectors

    def project(self, **kwargs: Any) -> int:
        return sum(int(projector.project(**kwargs)) for projector in self.projectors)

    def validate(self, **kwargs: Any) -> None:
        for projector in self.projectors:
            validator = getattr(projector, "validate", None)
            if validator is not None:
                validator(**kwargs)

    def validate_page(self, **kwargs: Any) -> None:
        for projector in self.projectors:
            validator = getattr(projector, "validate_page", None)
            if validator is not None:
                validator(**kwargs)
            else:
                item_validator = getattr(projector, "validate", None)
                if item_validator is not None:
                    for event in kwargs.get("events", ()):
                        item_validator(
                            brand_id=kwargs["brand_id"],
                            connector_account_id=kwargs["connector_account_id"],
                            event=event,
                        )


def _count(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a non-negative integer") from error
    if parsed < 0 or float(value) != parsed:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def _event_material_fingerprint(
    *, event_kind: str, provider_external_id: Any, observed_at: Any,
    payload: Mapping[str, Any],
) -> str:
    canonical = json.dumps(
        {
            "event_kind": event_kind,
            "provider_external_id": provider_external_id,
            "observed_at": observed_at,
            "payload": dict(payload),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _session_ref_valid(value: Any) -> bool:
    text = str(value or "")
    return (
        len(text) == 71 and text.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in text[7:])
    )


__all__ = ["CompositeCampaignMetricProjector", "WebsiteCampaignMetricProjector"]
