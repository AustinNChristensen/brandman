from pathlib import Path

import pytest

from app import store
from app.connectors import ConnectorEvent, ConnectorKind, ConnectorResult, EventKind, SyncCursor
from app.runtime import BrandOSRuntime, build_runtime
from app.sync import enqueue_sync_job
from app.website_metrics import WebsiteCampaignMetricProjector


class StubConnector:
    kind = ConnectorKind.RSS

    def __init__(self):
        self.calls = []

    def sync(self, cursor=None):
        self.calls.append(cursor)
        event = ConnectorEvent(
            self.kind,
            EventKind.SOURCE_ITEM,
            "rss:item-1",
            "2026-09-01T12:00:00+00:00",
            "item-1",
            {"title": "New offer", "summary": "Details", "url": "https://example.test/offer"},
        )
        return ConnectorResult((event,), SyncCursor("item-1"))


def setup_data(tmp_path: Path):
    store.DATA_PATH = tmp_path / "runtime.db"
    store.init_db()
    brand = store.get_brand("demo-brand")
    account = store.upsert_connector_account(
        brand["id"], "rss", "primary", "Primary feed", status="healthy"
    )
    return brand, account


def enqueue(brand, account, cycle):
    return enqueue_sync_job(
        brand_id=brand["id"],
        connector_account_id=account["id"],
        stream="feed",
        idempotency_key=cycle,
    )


def test_run_once_composes_worker_and_sync_orchestrator(tmp_path):
    brand, account = setup_data(tmp_path)
    connector = StubConnector()
    enqueue(brand, account, "cycle-1")
    runtime = build_runtime("runtime-test", {account["id"]: connector})

    completed = runtime.run_once()

    assert completed["status"] == "completed"
    assert completed["result"]["sources_upserted"] == 1
    assert connector.calls == [None]
    assert runtime.run_once() is None


def test_run_until_idle_is_bounded_and_summarizes_results(tmp_path):
    brand, account = setup_data(tmp_path)
    connector = StubConnector()
    for number in range(3):
        enqueue(brand, account, f"cycle-{number}")
    runtime = BrandOSRuntime("runtime-test", {account["id"]: connector})

    first = runtime.run_until_idle(max_jobs=2)
    second = runtime.run_until_idle(max_jobs=2)

    assert first.processed == 2
    assert first.completed == 2
    assert first.reached_limit is True
    assert second.processed == 1
    assert second.completed == 1
    assert second.reached_limit is False
    assert len(first.as_dict()["jobs"]) == 2


def test_run_until_idle_rejects_unbounded_values(tmp_path):
    setup_data(tmp_path)
    runtime = BrandOSRuntime("runtime-test")
    with pytest.raises(ValueError, match="at least 1"):
        runtime.run_until_idle(max_jobs=0)


def test_missing_connector_creates_safe_deduplicated_feedback(tmp_path):
    brand, account = setup_data(tmp_path)
    enqueue(brand, account, "cycle-1")
    runtime = BrandOSRuntime("runtime-test", retry_base_seconds=0)

    result = runtime.run_until_idle(max_jobs=3, as_of="9999-12-31T23:59:59+00:00")

    assert result.processed == 3
    assert result.needs_attention == 1
    feedback = store.rows(
        "SELECT * FROM product_feedback WHERE fingerprint=?",
        (f"runtime-missing-connector:{account['id']}",),
    )
    assert len(feedback) == 1
    assert feedback[0]["occurrence_count"] == 3
    assert "credential" not in feedback[0]["details"].lower()


def test_register_connector_allows_retry_to_succeed(tmp_path):
    brand, account = setup_data(tmp_path)
    job = enqueue(brand, account, "cycle-1")
    runtime = BrandOSRuntime("runtime-test", retry_base_seconds=0)
    failed = runtime.run_once()
    assert failed["status"] == "retry"
    assert failed["last_error"] == f"no connector registered for account {account['id']}"

    connector = StubConnector()
    runtime.register_connector(account["id"], connector)
    completed = runtime.run_once(as_of="9999-12-31T23:59:59+00:00")
    assert completed["status"] == "completed"
    assert completed["last_error"] is None
    attempts = store.rows(
        "SELECT status,error,result FROM job_attempts WHERE job_id=? ORDER BY attempt_number",
        (job["id"],),
    )
    assert [attempt["status"] for attempt in attempts] == ["failed", "completed"]
    assert attempts[0]["error"] == f"no connector registered for account {account['id']}"
    assert attempts[1]["error"] is None
    assert attempts[1]["result"] is not None


def test_recover_stale_job_then_processes_it(tmp_path):
    brand, account = setup_data(tmp_path)
    connector = StubConnector()
    enqueue(brand, account, "cycle-1")
    claimed = store.claim_next_job("dead-worker")
    assert claimed["status"] == "running"

    runtime = BrandOSRuntime("replacement", {account["id"]: connector})
    completed = runtime.run_once(
        recover_stale_before="9999-12-31T23:59:59+00:00",
        as_of="9999-12-31T23:59:59+00:00",
    )

    assert completed["status"] == "completed"
    attempts = store.rows("SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt_number", (claimed["id"],))
    assert [attempt["status"] for attempt in attempts] == ["failed", "completed"]
    assert attempts[0]["error"] == "Worker lease expired"


def test_runtime_reports_normalized_website_deployment_observability(tmp_path):
    store.DATA_PATH = tmp_path / "website-runtime.db"
    store.init_db()
    brand = store.get_brand("demo-brand")
    account = store.upsert_connector_account(
        brand["id"], "website", "first-party", "Website analytics", status="healthy",
    )

    class WebsiteDeploymentConnector:
        kind = ConnectorKind.WEBSITE

        def sync(self, cursor=None):
            return ConnectorResult((ConnectorEvent(
                self.kind, EventKind.DEPLOYMENT_CHANGED, "website:deploy:abc",
                "2026-09-02T12:00:00+00:00", "deploy-abc", {
                    "event_type": "deployment_changed", "deployment_id": "production-site",
                    "deployment_revision": "git-abc", "environment": "production",
                },
            ),))

    enqueue_sync_job(
        brand_id=brand["id"], connector_account_id=account["id"], stream="events",
        idempotency_key="website-deployment-cycle",
    )
    completed = BrandOSRuntime(
        "website-runtime", {account["id"]: WebsiteDeploymentConnector()},
        campaign_metric_projector=WebsiteCampaignMetricProjector(store.DATA_PATH),
    ).run_once()

    assert completed["status"] == "completed"
    assert completed["result"]["website_deployments_recorded"] == 1
    assert completed["result"]["performance_recorded"] == 0
    connector_event = store.row("SELECT event_type FROM connector_events")
    assert connector_event["event_type"] == EventKind.DEPLOYMENT_CHANGED.value
    observation = store.row("SELECT * FROM website_event_observations")
    assert observation["deployment_revision"] == "git-abc"
