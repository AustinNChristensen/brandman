from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from brandman import store
from brandman.main import app


def auth(password: str) -> dict[str, str]:
    value = base64.b64encode(f"operator:{password}".encode()).decode()
    return {"Authorization": f"Basic {value}"}


def setup(client: TestClient) -> tuple[dict, dict]:
    guideline = client.post("/api/brands/demo-brand/guidelines", json={
        "content_type": "newsletter", "channel": "beehiiv", "name": "House style",
        "instructions": "Write direct source-grounded decision support.", "rules": {},
        "reason": "Establish exact governed generation input", "activate": True,
    })
    assert guideline.status_code == 201
    source = client.post("/api/brands/demo-brand/sources", json={
        "title": "Official transfer terms", "source_type": "manual",
        "body_summary": "A verified 20 percent transfer bonus ends Friday.",
        "url": "https://issuer.test/terms", "lifecycle_state": "published",
    })
    assert source.status_code == 201
    return guideline.json(), source.json()


def test_preview_binds_evidence_and_guideline_then_confirmation_saves_inert_lineage(monkeypatch):
    monkeypatch.setenv("BRANDMAN_PREVIEW_PASSWORD", "proposal-test")
    with TestClient(app, headers=auth("proposal-test")) as client:
        guideline, source = setup(client)
        candidate = client.post("/api/brands/demo-brand/editorial-candidates", json={
            "title": "Reader transfer decision", "summary": "Explain fit and traps.",
            "dimensions": {"relevance": 1, "confidence": 1},
            "recommended_treatment": "newsletter_and_social",
            "supporting_sources": [{"source_id": source["id"], "url": source["url"]}],
        }).json()
        preview = client.post("/api/brands/demo-brand/operator-proposals/preview", json={
            "command": "Build a concise campaign explaining who should transfer now.",
            "source_ids": [], "candidate_ids": [candidate["id"]],
        })
        assert preview.status_code == 201
        proposal = preview.json()
        assert proposal["status"] == "previewed"
        assert proposal["guideline_fingerprint"] == guideline["active_version"]["content_fingerprint"]
        assert proposal["evidence"][0]["id"] == source["id"]
        assert proposal["preview"]["campaign"]["status"] == "draft"
        assert proposal["preview"]["x_draft"]["status"] == "draft"

        assert client.post(
            f"/api/brands/demo-brand/operator-proposals/{proposal['id']}/confirm",
            json={"confirm": True, "actor": "mallory"},
        ).status_code == 422
        confirmed = client.post(
            f"/api/brands/demo-brand/operator-proposals/{proposal['id']}/confirm",
            json={"confirm": True},
        )
        assert confirmed.status_code == 200
        result = confirmed.json()
        assert result["status"] == "confirmed" and result["confirmed_by"] == "chris"
        campaign = store.row("SELECT * FROM campaigns WHERE id=?", (result["result"]["campaign_id"],))
        post = store.row("SELECT * FROM posts WHERE id=?", (result["result"]["x_post_id"],))
        issue = store.row("SELECT * FROM newsletter_issues WHERE id=?", (result["result"]["newsletter_issue_id"],))
        assert campaign["source_id"] == source["id"] and campaign["status"] == "draft"
        assert post["campaign_id"] == campaign["id"] and post["status"] == "draft"
        assert issue["lifecycle"] == "idea"
        assert issue["candidate_id"] == candidate["id"]
        graph = client.get(f"/api/campaigns/{campaign['id']}/graph")
        assert graph.status_code == 200
        memberships = graph.json()["memberships"]
        assert [(item["asset_type"], item["asset_id"], item["role"]) for item in memberships] == [
            ("newsletter_issue", issue["id"], "anchor"),
            ("post", post["id"], "touchpoint"),
        ]
        assert graph.json()["relationships"][0]["from_membership_id"] == result["result"]["newsletter_membership_id"]
        assert graph.json()["relationships"][0]["to_membership_id"] == result["result"]["x_membership_id"]
        assert store.rows("SELECT * FROM dispatch_items") == []
        assert client.post(
            f"/api/brands/demo-brand/operator-proposals/{proposal['id']}/confirm", json={"confirm": True},
        ).status_code == 409


def test_cross_brand_and_stale_evidence_fail_closed(monkeypatch):
    monkeypatch.setenv("BRANDMAN_PREVIEW_PASSWORD", "proposal-stale-test")
    with TestClient(app, headers=auth("proposal-stale-test")) as client:
        _, source = setup(client)
        other = client.post("/api/brands", json={
            "slug": "other", "name": "Other", "mission": "M", "voice": "V",
            "compliance_rules": "C", "approval_policy": "human_approval_required",
        }).json()
        client.post("/api/brands/other/guidelines", json={
            "content_type": "newsletter", "channel": "beehiiv", "name": "Other style",
            "instructions": "Other exact active instructions", "rules": {},
            "reason": "Enable other brand preview", "activate": True,
        })
        assert client.post("/api/brands/other/operator-proposals/preview", json={
            "command": "Build content from foreign evidence", "source_ids": [source["id"]],
        }).status_code == 409
        proposal = client.post("/api/brands/demo-brand/operator-proposals/preview", json={
            "command": "Build current source-grounded content", "source_ids": [source["id"]],
        }).json()
        assert client.get(f"/api/brands/other/operator-proposals/{proposal['id']}").status_code == 404
        with store.connection() as connection:
            connection.execute("UPDATE sources SET body_summary='materially changed' WHERE id=?", (source["id"],))
        refused = client.post(
            f"/api/brands/demo-brand/operator-proposals/{proposal['id']}/confirm", json={"confirm": True},
        )
        assert refused.status_code == 409 and "changed" in refused.json()["detail"]
        assert store.rows("SELECT * FROM campaigns") == []
        assert other["id"] != proposal["brand_id"]


def test_guideline_change_invalidates_preview(monkeypatch):
    monkeypatch.setenv("BRANDMAN_PREVIEW_PASSWORD", "proposal-guideline-test")
    with TestClient(app, headers=auth("proposal-guideline-test")) as client:
        guideline, source = setup(client)
        proposal = client.post("/api/brands/demo-brand/operator-proposals/preview", json={
            "command": "Build content under the displayed active rules", "source_ids": [source["id"]],
        }).json()
        changed = client.post(f"/api/brand-guidelines/{guideline['id']}/versions", json={
            "instructions": "A newly reviewed instruction set.", "rules": {},
            "reason": "Change the exact generation input",
        }).json()
        assert client.post(f"/api/brand-guidelines/{guideline['id']}/versions/{changed['versions'][0]['version']}/activate", json={
            "reason": "Activate the newly reviewed instruction",
        }).status_code == 200
        refused = client.post(
            f"/api/brands/demo-brand/operator-proposals/{proposal['id']}/confirm", json={"confirm": True},
        )
        assert refused.status_code == 409 and "guideline changed" in refused.json()["detail"]
        assert store.rows("SELECT * FROM campaigns") == []
