from pathlib import Path

import pytest

from brandman import store
from brandman.connectors import (
    ConnectorError,
    ConnectorEvent,
    ConnectorKind,
    ConnectorResult,
    EventKind,
    SyncCursor,
)
from brandman.jobs import JobWorker
from brandman.sync import SYNC_JOB_TYPE, SyncOrchestrator, enqueue_sync_job, make_sync_job_handler


def setup_account(tmp_path: Path, connector_type: str = "beehiiv"):
    store.DATA_PATH = tmp_path / "sync.db"
    store.init_db()
    brand = store.get_brand("demo-brand")
    account = store.upsert_connector_account(
        brand["id"], connector_type, "primary", "Primary", status="healthy"
    )
    return brand, account


class StubConnector:
    def __init__(self, kind, result=None, error=None):
        self.kind = kind
        self.result = result
        self.error = error
        self.cursors = []

    def sync(self, cursor=None):
        self.cursors.append(cursor)
        if self.error:
            raise self.error
        return self.result


def source_event(kind, key="one", external_id="remote-1"):
    return ConnectorEvent(
        connector=kind,
        kind=EventKind.SOURCE_ITEM,
        dedup_key=f"{kind.value}:{key}",
        occurred_at="2026-09-01T12:00:00+00:00",
        external_id=external_id,
        payload={
            "title": "A useful offer",
            "url": "https://example.test/offer",
            "summary": "Offer details",
            "status": "confirmed",
        },
    )


def test_beehiiv_batch_is_idempotent_and_advances_cursor_last(tmp_path):
    brand, account = setup_account(tmp_path)
    connector = StubConnector(
        ConnectorKind.BEEHIIV,
        ConnectorResult((source_event(ConnectorKind.BEEHIIV),), SyncCursor("page-2"), True),
    )
    orchestrator = SyncOrchestrator()

    first = orchestrator.sync_once(
        connector, brand_id=brand["id"], connector_account_id=account["id"], stream="posts"
    )
    second = orchestrator.apply_result(
        connector.result,
        connector_kind=ConnectorKind.BEEHIIV,
        brand_id=brand["id"],
        connector_account_id=account["id"],
        stream="posts",
    )

    assert first.recorded == 1
    assert second.recorded == 0
    assert len(store.rows("SELECT * FROM sources")) == 1
    assert len(store.rows("SELECT * FROM connector_events")) == 1
    assert store.get_sync_cursor(account["id"], "posts")["cursor"] == "page-2"
    jobs = store.rows("SELECT * FROM durable_jobs WHERE job_type=?", (SYNC_JOB_TYPE,))
    assert len(jobs) == 1
    assert first.continuation_job_id == jobs[0]["id"]


def test_rss_source_without_provider_id_uses_normalized_identity(tmp_path):
    brand, account = setup_account(tmp_path, "rss")
    event = source_event(ConnectorKind.RSS, key="fingerprint", external_id=None)
    result = ConnectorResult((event,), SyncCursor(event.dedup_key))

    outcome = SyncOrchestrator().apply_result(
        result,
        connector_kind=ConnectorKind.RSS,
        brand_id=brand["id"],
        connector_account_id=account["id"],
        stream="feed",
    )

    source = store.row("SELECT * FROM sources")
    assert outcome.sources_upserted == 1
    assert source["source_type"] == "rss"
    assert source["external_source_id"] == f"{account['id']}:{event.dedup_key}"
    assert source["lifecycle_state"] == "published"


def test_metric_projection_is_idempotent(tmp_path):
    brand, account = setup_account(tmp_path, "website")
    event = ConnectorEvent(
        ConnectorKind.WEBSITE,
        EventKind.METRIC_OBSERVED,
        "website:analytics-row-1",
        "2026-09-01T15:00:00+00:00",
        "analytics-row-1",
        {"metric": "newsletter_conversion", "value": 1},
    )
    result = ConnectorResult((event,))
    orchestrator = SyncOrchestrator()
    for _ in range(2):
        orchestrator.apply_result(
            result,
            connector_kind=ConnectorKind.WEBSITE,
            brand_id=brand["id"],
            connector_account_id=account["id"],
            stream="metrics",
        )

    records = store.rows("SELECT * FROM performance_records")
    assert len(records) == 1
    assert records[0]["conversions"] == 1


def test_failure_keeps_cursor_and_creates_deduplicated_feedback(tmp_path):
    brand, account = setup_account(tmp_path)
    store.set_sync_cursor(account["id"], "posts", "page-4")
    connector = StubConnector(
        ConnectorKind.BEEHIIV,
        error=ConnectorError(ConnectorKind.BEEHIIV, "post sync", 403),
    )
    orchestrator = SyncOrchestrator()
    for _ in range(2):
        with pytest.raises(ConnectorError):
            orchestrator.sync_once(
                connector, brand_id=brand["id"], connector_account_id=account["id"], stream="posts"
            )

    assert connector.cursors == [SyncCursor("page-4"), SyncCursor("page-4")]
    assert store.get_sync_cursor(account["id"], "posts")["cursor"] == "page-4"
    feedback = store.rows("SELECT * FROM product_feedback")
    assert len(feedback) == 1
    assert feedback[0]["occurrence_count"] == 2
    assert feedback[0]["component"] == "beehiiv.posts.sync"


def test_cursor_does_not_advance_when_projection_fails(tmp_path):
    brand, account = setup_account(tmp_path, "website")
    store.set_sync_cursor(account["id"], "metrics", "old")
    bad_metric = ConnectorEvent(
        ConnectorKind.WEBSITE,
        EventKind.METRIC_OBSERVED,
        "website:bad",
        "2026-09-01T15:00:00+00:00",
        "bad",
        {"metric": "clicks", "value": "not-a-number"},
    )
    connector = StubConnector(
        ConnectorKind.WEBSITE, ConnectorResult((bad_metric,), SyncCursor("new"))
    )
    with pytest.raises(ValueError):
        SyncOrchestrator().sync_once(
            connector, brand_id=brand["id"], connector_account_id=account["id"], stream="metrics"
        )
    assert store.get_sync_cursor(account["id"], "metrics")["cursor"] == "old"
    assert store.rows("SELECT * FROM product_feedback")


def test_durable_job_handler_runs_injected_connector(tmp_path):
    brand, account = setup_account(tmp_path, "rss")
    connector = StubConnector(
        ConnectorKind.RSS,
        ConnectorResult((source_event(ConnectorKind.RSS),), SyncCursor("latest")),
    )
    first = enqueue_sync_job(
        brand_id=brand["id"],
        connector_account_id=account["id"],
        stream="feed",
        idempotency_key="rss:feed:cycle-1",
    )
    duplicate = enqueue_sync_job(
        brand_id=brand["id"],
        connector_account_id=account["id"],
        stream="feed",
        idempotency_key="rss:feed:cycle-1",
    )
    worker = JobWorker("sync-test")
    worker.register(SYNC_JOB_TYPE, make_sync_job_handler({account["id"]: connector}))
    completed = worker.run_once()

    assert duplicate["id"] == first["id"]
    assert completed["status"] == "completed"
    assert completed["result"]["sources_upserted"] == 1
