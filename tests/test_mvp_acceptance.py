"""Deterministic acceptance proof for the governed DemoBrand MVP loop.

No provider credential or live endpoint is used here.  The test crosses the same
SQLite scheduler, durable-job, connector, editorial, delivery, and KPI boundaries
as production so a green result is meaningful beyond isolated unit behavior.
"""

from __future__ import annotations

import json

import pytest

from brandman import store
from brandman.attribution_store import AttributionStore
from brandman.beehiiv_delivery import BeehiivDraftDelivery
from brandman.beehiiv_runtime import NewsletterExportJobError, enqueue_newsletter_export, register_beehiiv_newsletter_export
from brandman.connectors import ConnectorEvent, ConnectorKind, ConnectorResult, EventKind, HttpResponse
from brandman.editorial import EditorialStore, IssueLifecycle
from brandman.kpi_projection import MissionKpiProjector
from brandman.runtime import BrandOSRuntime
from brandman.scheduler import CONNECTOR_SYNC, PeriodicOrchestrator
from brandman.source_campaign import SourceCampaignOperator


class FakeReadConnector:
    def __init__(self, kind: ConnectorKind, result: ConnectorResult):
        self.kind = kind
        self.result = result
        self.cursors = []

    def sync(self, cursor=None):
        self.cursors.append(cursor)
        return self.result


class FakeBeehiivTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return HttpResponse(201, json.dumps({"data": {
            "id": "beehiiv-draft-acceptance", "status": "draft",
            "preview_url": "https://app.beehiiv.test/posts/acceptance",
        }}).encode())


def test_periodic_source_to_approved_draft_to_evidence_backed_trajectory(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "now", lambda: "2026-09-02T12:00:00+00:00")
    database = tmp_path / "mvp-acceptance.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    store.init_db()
    brand = store.get_brand("demo-brand")
    mission = store.ensure_growth_mission("demo-brand")
    editorial = EditorialStore(database, clock=lambda: "2026-09-02T12:00:00+00:00")
    source_operator = SourceCampaignOperator(editorial)
    attribution = AttributionStore(database, clock=lambda: "2026-09-02T12:00:00+00:00")

    rss_account = store.upsert_connector_account(
        brand["id"], "rss", "https://newsletter.test/feed", "Example Newsletter",
        status="healthy", capabilities=["content.read"],
    )
    source_event = ConnectorEvent(
        ConnectorKind.RSS, EventKind.SOURCE_ITEM, "rss:story-1",
        "2026-09-02T11:55:00+00:00", "story-1",
        {
            "title": "A pricing change was announced",
            "summary": "The source reports an 80,000-point offer.",
            "url": "https://newsletter.test/story",
            "published_at": "2026-09-02T11:45:00+00:00",
            "content_fingerprint": "source-fingerprint-1",
        },
    )
    source_connector = FakeReadConnector(ConnectorKind.RSS, ConnectorResult((source_event,)))
    runtime = BrandOSRuntime(
        "mvp-acceptance", {rss_account["id"]: source_connector}, retry_base_seconds=0,
        kpi_projector=MissionKpiProjector(attribution),
        source_campaign_operator=source_operator,
    )
    scheduler = PeriodicOrchestrator(database)
    scheduler.ensure_schedule(
        "acceptance:source", name="Fetch source", action_type=CONNECTOR_SYNC,
        interval_seconds=900,
        payload={"brand_id": brand["id"], "connector_account_id": rss_account["id"], "stream": "content"},
        brand_id=brand["id"], connector_account_id=rss_account["id"],
        next_run_at="2026-09-02T12:00:00+00:00",
    )

    tick = scheduler.tick(as_of="2026-09-02T12:00:00+00:00")
    sync_run = runtime.run_until_idle(max_jobs=5, as_of="2026-09-02T12:00:01+00:00")
    assert tick.jobs_enqueued == 1
    assert sync_run.completed == 1
    assert sync_run.jobs[0]["result"]["editorial_candidates_projected"] == 1
    candidate = editorial.list_candidates(brand["id"])[0]
    source = store.row("SELECT * FROM sources WHERE brand_id=?", (brand["id"],))
    campaign = store.row("SELECT * FROM campaigns WHERE brand_id=?", (brand["id"],))
    x_post = store.row("SELECT * FROM posts WHERE campaign_id=?", (campaign["id"],))
    assert candidate["supporting_sources"][0]["connector_event_id"]
    assert campaign["status"] == "draft"
    assert x_post["status"] == "draft"  # Periodic work never approves a public action.

    claim = {
        "id": "claim-1", "text": source_event.payload["summary"], "volatile": False,
        "citations": [{"source_id": source["id"], "url": source_event.payload["url"]}],
    }
    issue = editorial.create_issue(brand["id"], {
        "editorial_thesis": "Explain the sourced offer without adding facts.",
        "target_reader": "Readers", "intended_outcome": "Review the offer",
        "final_title": source_event.payload["title"], "subject": source_event.payload["title"],
        "preview_text": source_event.payload["summary"],
        "sections": [{"heading": source_event.payload["title"], "body": source_event.payload["summary"]}],
        "claims": [claim],
        "source_provenance": [{"source_id": source["id"], "url": source_event.payload["url"], "authority_type": "third_party"}],
    }, created_by="brand-os", candidate_id=candidate["id"])
    editorial.transition(issue["id"], IssueLifecycle.OUTLINE)
    editorial.transition(issue["id"], IssueLifecycle.DRAFT)
    with pytest.raises(NewsletterExportJobError, match="approved"):
        enqueue_newsletter_export(editorial, issue["id"])
    fact_check = editorial.record_fact_check(
        issue["id"], expected_revision=1, reviewer="Chris",
        verdicts=[{"claim_id": "claim-1", "verified": True}],
    )
    approved = editorial.approve_issue(issue["id"], approver="Chris", expected_revision=1)
    assert fact_check["reviewer"] == "Chris"
    assert approved["approved_revision"] == approved["current_revision"] == 1

    beehiiv_account = store.upsert_connector_account(
        brand["id"], "beehiiv", "publication-1", "DemoBrand newsletter", status="healthy",
        scopes=["posts.write"],
    )
    transport = FakeBeehiivTransport()
    delivery = BeehiivDraftDelivery(
        editorial, "publication-1", transport, lambda: "Bearer fake-not-a-secret",
        lambda _key: None, lambda **_fields: None, wait=lambda _seconds: None,
    )
    register_beehiiv_newsletter_export(runtime, editorial, delivery)
    enqueue_newsletter_export(
        editorial, issue["id"], brand_id=brand["id"], connector_account_id=beehiiv_account["id"],
    )
    export_run = runtime.run_once(as_of="2026-09-02T12:01:00+00:00")
    exported = editorial.get_issue(issue["id"])
    assert export_run["status"] == "completed"
    assert exported["lifecycle"] == "exported"
    assert exported["beehiiv_external_id"] == "beehiiv-draft-acceptance"
    assert transport.calls[0][2]["json_body"]["status"] == "draft"
    assert x_post["status"] == "draft"  # Newsletter approval cannot approve the X draft.

    x_account = store.upsert_connector_account(
        brand["id"], "x", "demobrand", "DemoBrand X", status="healthy",
        scopes=["users.read"],
    )
    metric_event = ConnectorEvent(
        ConnectorKind.X, EventKind.METRIC_OBSERVED, "x:followers:20",
        "2026-09-02T13:00:00+00:00", "demobrand",
        {"metric": "x_followers", "value": 20, "evidence_type": "x_profile_metrics"},
    )
    runtime.register_connector(
        x_account["id"], FakeReadConnector(ConnectorKind.X, ConnectorResult((metric_event,))),
    )
    scheduler.ensure_schedule(
        "acceptance:metrics", name="Measure mission", action_type=CONNECTOR_SYNC,
        interval_seconds=900,
        payload={"brand_id": brand["id"], "connector_account_id": x_account["id"], "stream": "metrics"},
        brand_id=brand["id"], connector_account_id=x_account["id"],
        next_run_at="2026-09-02T13:00:00+00:00",
    )
    metric_tick = scheduler.tick(as_of="2026-09-02T13:00:00+00:00")
    metric_run = runtime.run_until_idle(max_jobs=5, as_of="2026-09-02T13:00:01+00:00")
    progress = store.mission_progress(mission["id"], as_of="2026-09-02T13:00:00+00:00")
    x_goal = next(goal for goal in progress["goals"] if goal["metric"] == "x_followers")
    canonical = attribution.get_canonical_kpi(mission["id"], "x_followers")
    # The source cadence is also due again; its cursor replay is harmless while
    # the newly due metrics schedule projects exactly one KPI observation.
    assert metric_tick.jobs_enqueued == 2
    metric_job = next(
        job for job in metric_run.jobs
        if job["payload"].get("connector_account_id") == x_account["id"]
    )
    assert metric_job["result"]["kpis_projected"] == 1
    assert canonical["connector_event_id"] == store.row(
        "SELECT id FROM connector_events WHERE connector_account_id=? AND stream='metrics'", (x_account["id"],)
    )["id"]
    assert x_goal["current"] == 20
    assert x_goal["trajectory_status"] == "ahead"
    assert x_goal["trajectory_amount"] > 0
