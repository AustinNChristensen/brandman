"""Projection of authenticated Beehiiv post stats onto campaign assets."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
import sqlite3
import json
from uuid import uuid4

from brandman import store
from brandman.campaign_graph import CampaignGraphStore
from brandman.connectors import ConnectorEvent, ConnectorKind, EventKind


class BeehiivCampaignMetricProjector:
    """Bind provider post observations to the exact exported newsletter issue."""

    def __init__(
        self, database: str | Path, *, connection: sqlite3.Connection | None = None,
    ) -> None:
        self.database = str(database)
        self.connection = connection
        self.graph = None if connection is not None else CampaignGraphStore(database)

    def project(
        self, *, brand_id: str, connector_account_id: str,
        connector_event: Mapping[str, Any], event: ConnectorEvent,
    ) -> int:
        if not (
            event.connector == ConnectorKind.BEEHIIV
            and event.kind == EventKind.METRIC_OBSERVED
            and event.payload.get("evidence_type") == "beehiiv_post_stats"
            and event.external_id and event.occurred_at
        ):
            return 0
        if (
            connector_event.get("connector_account_id") != connector_account_id
            or not connector_event.get("id")
        ):
            return 0
        connection = self.connection or self._connect()
        owned_connection = self.connection is None
        try:
            tables = {row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if not {
                "newsletter_issues", "campaign_asset_memberships",
                "campaign_asset_metric_observations",
            } <= tables:
                return 0
            account = connection.execute(
                """SELECT 1 FROM connector_accounts
                   WHERE id=? AND brand_id=? AND connector_type='beehiiv'
                     AND status IN ('healthy','connected','active')""",
                (connector_account_id, brand_id),
            ).fetchone()
            issue = connection.execute(
                """SELECT id FROM newsletter_issues
                   WHERE brand_id=? AND beehiiv_external_id=?
                     AND lifecycle IN ('exported','scheduled','published')""",
                (brand_id, event.external_id),
            ).fetchone()
            membership = None if issue is None else connection.execute(
                """SELECT id FROM campaign_asset_memberships
                   WHERE asset_type='newsletter_issue' AND asset_id=? AND active=1
                     AND channel IN ('newsletter','email')
                   ORDER BY attribution_primary DESC,sequence,id LIMIT 1""",
                (issue["id"],),
            ).fetchone()
        finally:
            if owned_connection:
                connection.close()
        if account is None or issue is None or membership is None:
            return 0
        raw = event.payload.get("native_metrics")
        if not isinstance(raw, Mapping):
            return 0
        native = {
            key: int(raw[key]) for key in ("delivered", "opens", "clicks", "unsubscribes")
            if isinstance(raw.get(key), int) and not isinstance(raw.get(key), bool)
            and int(raw[key]) >= 0
        }
        if not native:
            return 0
        idempotency_key = (
            f"beehiiv-campaign-metric:{membership['id']}:"
            f"{connector_event['id']}"
        )
        connection = self.connection or self._connect()
        owned_connection = self.connection is None
        existing = connection.execute(
            """SELECT * FROM campaign_asset_metric_observations
               WHERE idempotency_key=?""", (idempotency_key,),
        ).fetchone()
        if existing is not None:
            if owned_connection:
                connection.close()
            return 0
        if self.connection is not None:
            connection.execute(
                """INSERT INTO campaign_asset_metric_observations
                   (id,brand_id,membership_id,observed_at,native_metrics_json,
                    conversions,revenue_cents,attribution_confidence,idempotency_key,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid4()), brand_id, membership["id"], event.occurred_at,
                 json.dumps(native, sort_keys=True), int(event.payload.get("conversions") or 0),
                 0, "authenticated_provider_reported", idempotency_key, store.now()),
            )
            return 1
        connection.close()
        assert self.graph is not None
        self.graph.record_metric(
            membership["id"], observed_at=event.occurred_at,
            native_metrics=native,
            conversions=int(event.payload.get("conversions") or 0),
            attribution_confidence="authenticated_provider_reported",
            idempotency_key=idempotency_key,
        )
        return 1

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


__all__ = ["BeehiivCampaignMetricProjector"]
