from __future__ import annotations

import base64
import os

os.environ["BRAND_OS_PREVIEW_PASSWORD"] = "test-only-password"

from fastapi.testclient import TestClient

from app import store
from app.main import app


def _headers() -> dict[str, str]:
    token = base64.b64encode(b"operator:test-only-password").decode()
    return {"Authorization": f"Basic {token}"}


def test_sources_and_performance_are_brand_scoped_and_read_only_first():
    with TestClient(app, headers=_headers()) as client:
        points = store.get_brand("demo-brand")
        demo_other = store.get_brand("demo-personal")

        configured = client.post("/api/brands/demo-brand/third-party-sources", json={
            "publisher_name": "Example Newsletter",
            "feed_url": "https://newsletter.example.test/feed.xml",
            "feed_format": "rss",
            "polling_interval_seconds": 1800,
            "reason": "Track relevant public reporting",
        })
        assert configured.status_code == 201
        assert configured.json()["content_policy"] == "syndicated_title_summary_link_only"
        assert client.get("/api/brands/demo-brand/third-party-sources").json()
        assert client.get("/api/brands/demo-personal/third-party-sources").json() == []
        # Onboarding records configuration and a future schedule; it does not
        # synchronously fetch or project provider content.
        assert store.rows(
            "SELECT id FROM connector_events WHERE connector_account_id=?",
            (configured.json()["connector_account_id"],),
        ) == []
        assert store.rows("SELECT id FROM sources WHERE brand_id=?", (points["id"],)) == []

        canonical = client.post("/api/brands/demo-brand/sources", json={
            "title": "Issuer terms update", "source_type": "manual",
            "body_summary": "Review the public terms page.",
            "url": "https://issuer.example.test/terms", "lifecycle_state": "draft",
        })
        assert canonical.status_code == 201
        assert canonical.json()["brand_id"] == points["id"]

        beehiiv = store.upsert_connector_account(
            points["id"], "beehiiv", "pub_demo", "Demo newsletter",
            status="connected", scopes=["posts.read", "metrics.read"],
            capabilities=["posts.read", "metrics.read"],
            configuration={"delivery_mode": "browser_assisted", "connection_role": "beehiiv_read"},
        )
        pull = client.post("/api/brands/demo-brand/beehiiv-assisted-pulls", json={
            "connector_account_id": beehiiv["id"],
            "scheduled_for": "2026-09-03T18:00:00Z",
        })
        assert pull.status_code == 202
        assert pull.json()["allowed_data"] == "post_metadata_and_aggregate_measurements"
        assert pull.json()["forbidden_actions"] == [
            "subscriber_data", "draft", "schedule", "send", "publish",
        ]
        assert store.row(
            "SELECT actor FROM beehiiv_assisted_pull_audit WHERE task_id=? AND action='requested'",
            (pull.json()["id"],),
        )["actor"] == "chris"

        store.insert("performance_records", {
            "brand_id": points["id"], "channel": "x",
            "observed_at": "2026-09-03T15:00:00+00:00",
            "impressions": 100, "clicks": 12, "engagements": 20,
            "conversions": 2, "revenue_cents": 1200, "notes": "Aggregate only",
        })
        store.insert("performance_records", {
            "brand_id": demo_other["id"], "channel": "web",
            "observed_at": "2026-09-03T15:00:00+00:00",
            "impressions": 50, "clicks": 4, "engagements": 4,
            "conversions": 1, "revenue_cents": 0, "notes": "Other tenant",
        })
        records = client.get("/api/brands/demo-brand/performance").json()
        assert [(item["channel"], item["clicks"]) for item in records] == [("x", 12)]

        plan = client.get(
            "/api/brands/demo-brand/performance-planning",
            params={"stage": "portfolio", "channel": "x"},
        )
        assert plan.status_code == 200
        assert plan.json()["policy"]["role"] == "bounded_prior_not_formula"
        assert "publish" in " ".join(plan.json()["policy"]["protected_invariants"]).lower()
