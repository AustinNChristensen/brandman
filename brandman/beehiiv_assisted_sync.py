"""Safe ingestion boundary for Beehiiv measurements fetched by an MCP/browser helper."""

from __future__ import annotations

from datetime import datetime
import json
import re
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from brandman import store
from brandman.beehiiv_metrics import BeehiivCampaignMetricProjector
from brandman.beehiiv_lifecycle import BeehiivNewsletterLifecycleProjector
from brandman.connectors import (
    ConnectorEvent, ConnectorKind, ConnectorResult, EventKind, dedup_identity,
    normalize_beehiiv_post_metrics,
    normalize_beehiiv_publication_stats,
)
from brandman.kpi_projection import build_mission_kpi_projector
from brandman.sync import SyncOrchestrator


class BeehiivAssistedSyncError(ValueError):
    pass


def ingest_beehiiv_measurements(
    database: str | Path, *, brand_id: str, posts: Sequence[Mapping[str, Any]],
    publication_stats: Mapping[str, Any] | None, observed_at: str,
    connector_account_id: str | None = None,
) -> dict[str, Any]:
    """Persist only aggregate provider measurements; discard any subscriber PII."""
    observed = _aware_timestamp(observed_at)
    account = _assisted_account(brand_id, connector_account_id)
    events = []
    for post in posts:
        try:
            event = normalize_beehiiv_post_metrics(post, observed_at=observed)
        except ValueError as error:
            raise BeehiivAssistedSyncError(str(error)) from error
        if event is not None:
            events.append(event)
    if publication_stats is not None:
        event = normalize_beehiiv_publication_stats(
            account["account_key"], publication_stats, observed_at=observed,
        )
        if event is None:
            raise BeehiivAssistedSyncError(
                "publication_stats must include a nonnegative active_subscriptions count"
            )
        events.append(event)
    orchestrator = SyncOrchestrator(
        kpi_projector=build_mission_kpi_projector(str(database)),
        campaign_metric_projector=BeehiivCampaignMetricProjector(database),
    )
    outcome = orchestrator.apply_result(
        ConnectorResult(tuple(events)), connector_kind=ConnectorKind.BEEHIIV,
        brand_id=brand_id, connector_account_id=account["id"],
        stream="assisted_measurements", enqueue_continuation=False,
    )
    return {
        **outcome.as_dict(),
        "mode": "assisted_aggregate_measurement_ingest",
        "privacy": "aggregate_only_no_subscriber_records",
    }


def ingest_beehiiv_pull(
    database: str | Path, *, brand_id: str, connector_account_id: str,
    posts: Sequence[Mapping[str, Any]], publication_stats: Mapping[str, Any] | None,
    observed_at: str,
    connection: sqlite3.Connection | None = None,
    require_post_measurement: bool = False,
) -> dict[str, Any]:
    """Validate a complete assisted pull, then project safe metadata and aggregates.

    Unknown post fields (including subscriber fields) are never persisted. The
    entire batch is validated before the first write so one malformed metric
    cannot leave a misleading partial observation behind.
    """
    observed = _aware_timestamp(observed_at)
    if len(posts) > 100:
        raise BeehiivAssistedSyncError("assisted Beehiiv pulls accept at most 100 posts")
    safe_posts: list[dict[str, Any]] = []
    post_metric_events: list[ConnectorEvent] = []
    # Validate account identity before processing any provider-shaped values.
    account = (
        _assisted_account_in_connection(connection, brand_id, connector_account_id)
        if connection is not None else _assisted_account(brand_id, connector_account_id)
    )
    for raw in posts:
        safe = _safe_post_metadata(raw)
        # This validates canonical provider ID and every supplied aggregate.
        try:
            metric_event = normalize_beehiiv_post_metrics(raw, observed_at=observed)
        except ValueError as error:
            raise BeehiivAssistedSyncError(str(error)) from error
        safe_posts.append(safe)
        if metric_event is not None:
            post_metric_events.append(metric_event)
    if require_post_measurement and not post_metric_events:
        raise BeehiivAssistedSyncError(
            "assisted Beehiiv pull requires at least one per-post aggregate measurement"
        )
    if publication_stats is not None:
        event = normalize_beehiiv_publication_stats(
            account["account_key"], publication_stats, observed_at=observed,
        )
        if event is None:
            raise BeehiivAssistedSyncError(
                "publication_stats must include a nonnegative active_subscriptions count"
            )

    repository: Any = store if connection is None else _ConnectionRepository(connection)
    lifecycle = BeehiivNewsletterLifecycleProjector(database, connection=connection)
    lifecycle_events = [_lifecycle_event(post, observed) for post in safe_posts]
    # Validate the complete provider page before its first metadata/lifecycle
    # write so an invalid bound issue leaves the pull safely replayable.
    for event in lifecycle_events:
        lifecycle.validate(
            brand_id=brand_id, connector_account_id=connector_account_id, event=event,
        )
    synced = []
    lifecycle_reconciled = 0
    for post, event in zip(safe_posts, lifecycle_events, strict=True):
        bound = lifecycle.binding(
            brand_id=brand_id, connector_account_id=connector_account_id, event=event,
        )
        if bound is not None:
            stored_event = repository.record_connector_event(
                connector_account_id, "assisted_content", event.dedup_key,
                event.kind.value, {**dict(event.payload), "provider_external_id": event.external_id},
                event.occurred_at or observed,
            )
            lifecycle_reconciled += lifecycle.project(
                brand_id=brand_id, connector_account_id=connector_account_id,
                connector_event=stored_event, event=event,
            )
            # This provider snapshot is lifecycle evidence for an issue BrandMan
            # already owns.  It must not become a new authoring source.
            continue
        tags = ", ".join(post["content_tags"])
        summary = post["subtitle"] or post["seo_description"] or (
            f"Beehiiv {post['status']} post"
        )
        if post["subject_line"]:
            summary += f" · email subject: {post['subject_line']}"
        if tags:
            summary += f" · tags: {tags}"
        synced.append(repository.upsert_external_source(brand_id, {
            "title": post["title"], "url": post["editor_url"],
            "source_type": "beehiiv", "body_summary": summary,
            "lifecycle_state": post["status"],
            "scheduled_for": post["scheduled_at"],
            "external_source_id": post["id"],
        }))
    if connection is None:
        measurements = ingest_beehiiv_measurements(
            database, brand_id=brand_id, connector_account_id=connector_account_id,
            posts=posts, publication_stats=publication_stats, observed_at=observed,
        )
    else:
        events = list(post_metric_events)
        if publication_stats is not None:
            event = normalize_beehiiv_publication_stats(
                account["account_key"], publication_stats, observed_at=observed,
            )
            assert event is not None
            events.append(event)
        measurements = SyncOrchestrator(
            repository=repository,
            campaign_metric_projector=BeehiivCampaignMetricProjector(
                database, connection=connection,
            ),
        ).apply_result(
            ConnectorResult(tuple(events)), connector_kind=ConnectorKind.BEEHIIV,
            brand_id=brand_id, connector_account_id=connector_account_id,
            stream="assisted_measurements", enqueue_continuation=False,
        ).as_dict()
    measured_campaign_ids = _measured_campaign_ids(
        database, connection=connection, brand_id=brand_id,
        connector_account_id=connector_account_id, events=post_metric_events,
    )
    return {
        **measurements, "observed_at": observed, "posts_received": len(posts),
        "post_measurements_received": len(post_metric_events),
        "measured_campaign_ids": measured_campaign_ids,
        "metadata_synced": len(synced),
        "newsletter_lifecycle_reconciled": lifecycle_reconciled,
        "scope": "post_metadata_and_aggregate_measurements_only",
        "forbidden_actions": ["subscriber_data", "draft", "schedule", "send", "publish"],
    }


def _measured_campaign_ids(
    database: str | Path, *, connection: sqlite3.Connection | None,
    brand_id: str, connector_account_id: str, events: Sequence[ConnectorEvent],
) -> list[str]:
    """Return campaigns backed by an exact stored post observation in this pull."""
    active = connection or sqlite3.connect(str(database), timeout=30)
    active.row_factory = sqlite3.Row
    owned = connection is None
    campaigns: set[str] = set()
    try:
        tables = {
            row["name"] for row in active.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {
            "connector_events", "newsletter_issues", "campaign_asset_memberships",
            "campaign_asset_metric_observations",
        } <= tables:
            return []
        for event in events:
            rows = active.execute(
                """SELECT m.campaign_id FROM connector_events e
                   JOIN newsletter_issues i
                     ON i.brand_id=? AND i.beehiiv_external_id=?
                   JOIN campaign_asset_memberships m
                     ON m.asset_type='newsletter_issue' AND m.asset_id=i.id
                    AND m.active=1 AND m.channel IN ('newsletter','email')
                   JOIN campaign_asset_metric_observations o
                     ON o.membership_id=m.id
                    AND o.idempotency_key=('beehiiv-campaign-metric:' || m.id || ':' || e.id)
                   WHERE e.connector_account_id=? AND e.external_id=?
                     AND e.event_type=?""",
                (brand_id, event.external_id, connector_account_id,
                 event.dedup_key, event.kind.value),
            ).fetchall()
            for row in rows:
                campaigns.add(str(row["campaign_id"]))
    finally:
        if owned:
            active.close()
    return sorted(campaigns)


def _assisted_account(
    brand_id: str, connector_account_id: str | None = None,
) -> dict[str, Any]:
    candidates = [
        account for account in store.list_connector_accounts(brand_id)
        if account["connector_type"] == "beehiiv"
        and account["status"] in {"healthy", "connected"}
        and (account.get("configuration") or {}).get("delivery_mode")
        in {"browser_assisted", "mcp_assisted"}
    ]
    if connector_account_id is not None:
        candidates = [account for account in candidates if account["id"] == connector_account_id]
    if len(candidates) != 1:
        raise BeehiivAssistedSyncError(
            "exactly one connected assisted Beehiiv account is required"
        )
    return candidates[0]


def _assisted_account_in_connection(
    connection: sqlite3.Connection, brand_id: str, connector_account_id: str,
) -> dict[str, Any]:
    row = connection.execute(
        """SELECT a.*,COALESCE(c.configuration,'{}') configuration
           FROM connector_accounts a LEFT JOIN connector_account_configurations c
             ON c.connector_account_id=a.id
           WHERE a.id=? AND a.brand_id=? AND a.connector_type='beehiiv'
             AND a.status IN ('healthy','connected','active')""",
        (connector_account_id, brand_id),
    ).fetchone()
    if row is None:
        raise BeehiivAssistedSyncError(
            "exactly one connected assisted Beehiiv account is required"
        )
    result = dict(row)
    configuration = json.loads(result.pop("configuration") or "{}")
    if configuration.get("delivery_mode") not in {"browser_assisted", "mcp_assisted"}:
        raise BeehiivAssistedSyncError(
            "exactly one connected assisted Beehiiv account is required"
        )
    result["configuration"] = configuration
    return result


class _ConnectionRepository:
    """Small SyncOrchestrator adapter bound to its caller's writer transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @staticmethod
    def now() -> str:
        return datetime.now().astimezone().isoformat()

    def row(self, query: str, parameters=()):
        found = self.connection.execute(query, parameters).fetchone()
        return dict(found) if found is not None else None

    def rows(self, query: str, parameters=()):
        return [dict(row) for row in self.connection.execute(query, parameters).fetchall()]

    def insert(self, table: str, payload: dict[str, Any]):
        value = {"id": str(uuid4()), "created_at": self.now(), **payload}
        columns = ",".join(value); placeholders = ",".join(f":{key}" for key in value)
        self.connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", value,
        )
        return value

    def upsert_external_source(self, brand_id: str, payload: dict[str, Any]):
        existing = self.connection.execute(
            "SELECT id FROM sources WHERE brand_id=? AND external_source_id=?",
            (brand_id, payload["external_source_id"]),
        ).fetchone()
        if existing:
            self.connection.execute(
                """UPDATE sources SET title=:title,url=:url,source_type=:source_type,
                   body_summary=:body_summary,lifecycle_state=:lifecycle_state,
                   scheduled_for=:scheduled_for WHERE id=:id""",
                {"id": existing["id"], **payload},
            )
            return dict(self.connection.execute(
                "SELECT * FROM sources WHERE id=?", (existing["id"],),
            ).fetchone())
        return self.insert("sources", {"brand_id": brand_id, **payload})

    def record_connector_event(
        self, connector_account_id: str, stream: str, external_id: str,
        event_type: str, payload: dict[str, Any], observed_at: str,
    ):
        event = {
            "id": str(uuid4()), "connector_account_id": connector_account_id,
            "stream": stream, "external_id": external_id, "event_type": event_type,
            "payload": json.dumps(payload, sort_keys=True), "observed_at": observed_at,
            "created_at": self.now(),
        }
        self.connection.execute(
            """INSERT INTO connector_events VALUES
               (:id,:connector_account_id,:stream,:external_id,:event_type,:payload,
                :observed_at,:created_at)
               ON CONFLICT(connector_account_id,stream,external_id,event_type) DO NOTHING""",
            event,
        )
        found = dict(self.connection.execute(
            """SELECT * FROM connector_events WHERE connector_account_id=? AND stream=?
               AND external_id=? AND event_type=?""",
            (connector_account_id, stream, external_id, event_type),
        ).fetchone())
        found["payload"] = json.loads(found["payload"])
        return found

    def set_sync_cursor(
        self, connector_account_id: str, stream: str, cursor: str | None,
        *, watermark: str | None = None,
    ):
        timestamp = self.now()
        self.connection.execute(
            """INSERT INTO sync_cursors
               (id,connector_account_id,stream,cursor,watermark,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(connector_account_id,stream) DO UPDATE SET
               cursor=excluded.cursor,watermark=excluded.watermark,updated_at=excluded.updated_at""",
            (str(uuid4()), connector_account_id, stream, cursor, watermark, timestamp, timestamp),
        )


def _safe_post_metadata(post: Mapping[str, Any]) -> dict[str, Any]:
    external_id = str(post.get("id") or "")
    if not re.fullmatch(r"post_[A-Za-z0-9-]{1,200}", external_id):
        raise BeehiivAssistedSyncError("Beehiiv posts require a canonical post_ provider ID")
    title = str(post.get("title") or "").strip()
    if not title or len(title) > 500:
        raise BeehiivAssistedSyncError("Beehiiv post title must contain 1-500 characters")
    status = str(post.get("status") or "")
    if status not in {"draft", "scheduled", "published", "archived"}:
        raise BeehiivAssistedSyncError(
            "Beehiiv post status must be draft, scheduled, published, or archived"
        )
    scheduled_at = post.get("scheduled_at")
    if scheduled_at is not None:
        scheduled_at = _aware_timestamp(str(scheduled_at))
    editor_url = post.get("editor_url") or post.get("web_url") or post.get("url")
    if editor_url is not None:
        parsed = urlsplit(str(editor_url))
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
        ):
            raise BeehiivAssistedSyncError(
                "Beehiiv post URL must be credential-free canonical HTTPS"
            )
        editor_url = str(editor_url)
    tags = post.get("content_tags") or []
    if not isinstance(tags, list) or len(tags) > 50:
        raise BeehiivAssistedSyncError("content_tags must be a list of at most 50 tags")
    safe_tags = []
    for tag in tags:
        value = tag.get("display") if isinstance(tag, Mapping) else tag
        value = str(value or "").strip()
        if value and len(value) <= 100:
            safe_tags.append(value)
    safe: dict[str, Any] = {
        "id": external_id, "title": title, "status": status,
        "editor_url": editor_url, "scheduled_at": scheduled_at,
        "content_tags": safe_tags,
    }
    published_at = post.get("published_at") or post.get("publish_date")
    safe["published_at"] = (
        _aware_timestamp(str(published_at)) if published_at is not None else None
    )
    for key in ("subtitle", "subject_line", "seo_description"):
        value = post.get(key)
        if value is not None and len(str(value)) > 1000:
            raise BeehiivAssistedSyncError(f"{key} exceeds 1000 characters")
        safe[key] = str(value).strip() if value is not None else None
    return safe


def _lifecycle_event(post: Mapping[str, Any], observed_at: str) -> ConnectorEvent:
    external_id = str(post["id"])
    return ConnectorEvent(
        connector=ConnectorKind.BEEHIIV, kind=EventKind.SOURCE_ITEM,
        dedup_key=dedup_identity(ConnectorKind.BEEHIIV, external_id=external_id),
        occurred_at=observed_at, external_id=external_id,
        payload={
            "status": post["status"], "scheduled_for": post.get("scheduled_at"),
            "published_at": post.get("published_at"),
            "provider_observed_at": observed_at,
        },
    )


def _aware_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise BeehiivAssistedSyncError("observed_at must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise BeehiivAssistedSyncError("observed_at must include a timezone")
    return parsed.isoformat()


__all__ = [
    "BeehiivAssistedSyncError", "ingest_beehiiv_measurements", "ingest_beehiiv_pull",
]
