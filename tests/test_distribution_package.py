import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import store
from app.approval_snapshots import ApprovalSnapshotStore
from app.dispatch import (
    GovernedDispatcher, Lifecycle, RevisionMismatch, SQLiteDispatchStore,
)
from app.distribution_package import DistributionPackageError, DistributionPackageStore
from app.editorial import EditorialStore, IssueLifecycle
from app.execution_handoff import ExecutionHandoffStore
from app.execution_handoff import ExecutionHandoffError
from app.execution_agents import ExecutionAgentRegistry
from app.attribution_store import AttributionStore
from app.campaign_graph import CampaignGraphStore
from app.canonical_revalidation import CanonicalSourceRevalidationStore
from app.main import app, editorial_store


def setup_package(tmp_path, monkeypatch):
    database = tmp_path / "distribution.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    store.init_db()
    brand = store.get_brand("demo-brand")
    source = store.insert("sources", {
        "brand_id": brand["id"], "title": "Issuer offer page",
        "url": "https://issuer.test/offer", "source_type": "official",
        "body_summary": "Offer metadata", "lifecycle_state": "published",
        "scheduled_for": None, "external_source_id": "offer-1",
    })
    editorial = EditorialStore(database)
    candidate = editorial.upsert_candidate(
        brand["id"], "Explain an offer", {"relevance": 1, "confidence": 1},
        recommended_treatment="newsletter_and_social",
        supporting_sources=[{
            "source_id": source["id"], "url": source["url"],
            "authority_type": "official",
        }],
    )
    issue = editorial.create_issue(brand["id"], {
        "editorial_thesis": "Explain the sourced terms", "target_reader": "Points collectors",
        "intended_outcome": "Make an informed choice", "subject": "Offer explained",
        "preview_text": "What changed and what it means", "final_title": "Offer explained",
        "sections": [{"heading": "What changed", "body": "Source-backed summary"}],
        "claims": [], "source_provenance": [{"source_id": source["id"], "url": source["url"]}],
    }, created_by="writer", candidate_id=candidate["id"])
    dispatcher = GovernedDispatcher(SQLiteDispatchStore(database))
    packages = DistributionPackageStore(database, editorial, dispatcher)
    return brand, source, candidate, issue, editorial, dispatcher, packages


def package_input(source_id):
    return {
        "expected_revision": 1, "primary_source_id": source_id,
        "campaign_name": "Offer distribution", "objective": "Drive informed readership",
        "email": {"subject": "Offer explained", "preview_text": "What changed"},
        "web": {"title": "Offer explained", "slug": "offer-explained",
                "seo_description": "A source-backed explanation",
                "url": "https://points.test/offer-explained"},
        "distribution": {"audience": "Points collectors", "primary_cta": "Read the guide",
                         "measurement_plan": "Track attributed clicks and conversions"},
        "x_drafts": [
            {"body": "The offer changed. Here is what the official terms mean.",
             "role": "launch", "hook": "What changed", "cta": "Read the guide"},
            {"body": "Before applying, compare the sourced terms and tradeoffs.",
             "role": "follow_up", "hook": "Decision support", "cta": "Review terms"},
        ],
        "idempotency_key": "issue-1-distribution-r1", "actor": "chris",
    }


def fact_check_anchor(editorial, issue):
    editorial.transition(issue["id"], IssueLifecycle.OUTLINE)
    editorial.transition(issue["id"], IssueLifecycle.DRAFT)
    return editorial.record_fact_check(
        issue["id"], expected_revision=issue["current_revision"],
        reviewer="Chris", verdicts=[],
    )


def test_package_preserves_full_lineage_and_creates_drafts_only(tmp_path, monkeypatch):
    brand, source, candidate, issue, _, dispatcher, packages = setup_package(tmp_path, monkeypatch)

    package = packages.create(brand["id"], issue["id"], **package_input(source["id"]))

    assert package["status"] == "draft"
    assert package["source"]["id"] == source["id"]
    assert package["candidate"]["id"] == candidate["id"]
    assert package["issue"] == {"id": issue["id"], "revision": 1}
    assert package["campaign"]["source_id"] == source["id"]
    assert package["campaign"]["status"] == "draft"
    assert package["email"]["subject"] == "Offer explained"
    assert package["web"]["slug"] == "offer-explained"
    assert package["distribution"]["measurement_plan"]
    assert len(package["artifacts"]) == 2
    assert all(artifact["post_status"] == "draft" for artifact in package["artifacts"])
    assert all(artifact["scheduled_for"] is None for artifact in package["artifacts"])
    assert all(artifact["external_post_id"] is None for artifact in package["artifacts"])
    assert all(artifact["dispatch"]["status"] == "draft" for artifact in package["artifacts"])
    assert all(artifact["dispatch"]["approval"] is None for artifact in package["artifacts"])
    assert package["governance"]["approval_ready"] is True
    assert all(artifact["tracked_url"] in artifact["body"] for artifact in package["artifacts"])
    assert all(
        artifact["dispatch"]["payload"]["destination"] == artifact["tracked_url"]
        for artifact in package["artifacts"]
    )
    assert package["lineage"]["flow_labels"] == [
        "Issuer offer page", "Explain an offer", "Offer explained", "Offer distribution",
    ]
    assert packages.create(brand["id"], issue["id"], **package_input(source["id"]))["id"] == package["id"]
    assert len(dispatcher.store.list_items(brand_id=brand["id"], status=Lifecycle.DRAFT)) == 2


def test_package_rejects_incomplete_metadata_unowned_source_and_stale_revision(tmp_path, monkeypatch):
    brand, source, _, issue, editorial, _, packages = setup_package(tmp_path, monkeypatch)
    invalid = package_input(source["id"])
    invalid["email"] = {"subject": "Missing preview"}
    with pytest.raises(DistributionPackageError, match="email metadata is incomplete"):
        packages.create(brand["id"], issue["id"], **invalid)
    assert store.rows("SELECT id FROM campaigns") == []

    invalid = package_input("not-candidate-evidence")
    with pytest.raises(DistributionPackageError, match="not governed supporting evidence"):
        packages.create(brand["id"], issue["id"], **invalid)

    editorial.revise_issue(issue["id"], {"preview_text": "New revision"}, created_by="writer")
    with pytest.raises(DistributionPackageError, match="exact current newsletter revision"):
        packages.create(brand["id"], issue["id"], **package_input(source["id"]))


def test_exact_revision_official_evidence_is_distinct_from_drifted_discovery(
    tmp_path, monkeypatch,
):
    brand, discovery, candidate, issue, editorial, _, packages = setup_package(tmp_path, monkeypatch)
    ba = store.insert("sources", {
        "brand_id": brand["id"], "title": "British Airways official offer terms",
        "url": "https://ba.test/official-offer", "source_type": "official",
        "body_summary": "Issuer-published terms", "lifecycle_state": "published",
        "scheduled_for": None, "external_source_id": "ba-official-1",
    })
    amex = store.insert("sources", {
        "brand_id": brand["id"], "title": "American Express official terms",
        "url": "https://amex.test/official-terms", "source_type": "issuer",
        "body_summary": "Card issuer terms", "lifecycle_state": "published",
        "scheduled_for": None, "external_source_id": "amex-official-1",
    })
    revised = editorial.revise_issue(issue["id"], {
        "source_provenance": [
            {"source_id": discovery["id"], "url": discovery["url"],
             "authority_type": "third_party"},
            {"source_id": ba["id"], "url": ba["url"], "authority_type": "official"},
            {"source_id": amex["id"], "url": amex["url"], "authority_type": "issuer"},
        ],
        "preview_text": "Corrected with issuer evidence",
    }, created_by="writer", change_note="Replace drifted headline terms")
    assert revised["current_revision"] == 2

    revalidation = CanonicalSourceRevalidationStore(packages.database)
    revalidation.record(brand["id"], discovery["id"], {
        "canonical_url": discovery["url"], "observed_at": "2026-09-02T12:00:00Z",
        "snapshot_fingerprint": "sha256:" + "1" * 64,
        "status": "conflict", "confidence": "high",
        "rationale": ["canonical page no longer supports the discovery headline"],
        "idempotency_key": "discovery-conflict",
    }, actor="canonical-revalidator")

    unsafe = package_input(discovery["id"])
    unsafe["expected_revision"] = 2
    unsafe["idempotency_key"] = "corrected-r2-unsafe"
    with pytest.raises(DistributionPackageError, match="canonical drift or conflict"):
        packages.create(brand["id"], issue["id"], **unsafe)

    payload = package_input(ba["id"])
    payload["expected_revision"] = 2
    payload["idempotency_key"] = "corrected-r2"
    package = packages.create(brand["id"], issue["id"], **payload)

    assert package["source_id"] == ba["id"]
    assert package["lineage"]["primary_evidence"]["id"] == ba["id"]
    assert {item["source_id"] for item in package["discovery_sources"]} == {discovery["id"]}
    assert {item["source_id"] for item in package["evidence_sources"]} == {
        discovery["id"], ba["id"], amex["id"],
    }
    assert next(
        item for item in package["evidence_sources"] if item["source_id"] == ba["id"]
    )["role"] == "primary_evidence"
    assert next(
        item for item in package["evidence_sources"] if item["source_id"] == amex["id"]
    )["authority_type"] == "issuer"
    drifted = next(
        item for item in package["evidence_sources"] if item["source_id"] == discovery["id"]
    )
    assert drifted["canonical_status"] == "conflict"
    assert drifted["authoritative"] == 0
    assert next(
        item for item in package["evidence_sources"] if item["source_id"] == ba["id"]
    )["authoritative"] == 1
    graph = CampaignGraphStore(packages.database).get(package["campaign_id"])
    assert graph["discovery_sources"][0]["source_id"] == discovery["id"]
    assert {item["source_id"] for item in graph["evidence_sources"]} == {
        discovery["id"], ba["id"], amex["id"],
    }
    assert packages.create(brand["id"], issue["id"], **payload)["id"] == package["id"]

    conflicting = dict(payload)
    conflicting["primary_source_id"] = amex["id"]
    with pytest.raises(DistributionPackageError, match="different package input"):
        packages.create(brand["id"], issue["id"], **conflicting)

    # Current evidence health is live governance state, while the creation
    # snapshot and exact retry binding remain immutable.
    revalidation.record(brand["id"], ba["id"], {
        "canonical_url": ba["url"], "observed_at": "2026-09-02T13:00:00Z",
        "snapshot_fingerprint": "sha256:" + "2" * 64,
        "status": "conflict", "confidence": "high",
        "rationale": ["issuer page now conflicts with the drafted terms"],
        "idempotency_key": "ba-later-conflict",
    }, actor="canonical-revalidator")
    replay = packages.create(brand["id"], issue["id"], **payload)
    assert replay["id"] == package["id"]
    assert replay["lineage"]["primary_evidence"]["canonical_status"] == "conflict"
    assert replay["lineage"]["primary_evidence"]["canonical_status_at_creation"] == "not_checked"
    assert replay["lineage"]["primary_evidence"]["authoritative"] == 0
    assert replay["governance"]["approval_ready"] is False
    assert any(
        blocker["code"] == "primary_evidence_not_authoritative"
        for blocker in replay["governance"]["blockers"]
    )

    editorial.revise_issue(
        issue["id"], {"preview_text": "One more exact-revision correction"},
        created_by="writer", change_note="Advance governed anchor",
    )
    assert packages.get(package["id"])["status"] == "stale"


def test_revision_discovery_role_stays_discovery_and_citation_authority_wins(tmp_path, monkeypatch):
    brand, tpg, _, issue, editorial, _, packages = setup_package(tmp_path, monkeypatch)
    official = store.insert("sources", {
        "brand_id": brand["id"], "title": "Amex offer terms",
        "url": "https://amex.test/offer", "source_type": "manual",
        "body_summary": "Primary terms", "lifecycle_state": "published",
        "scheduled_for": None, "external_source_id": "amex-manual-import",
    })
    revised = editorial.revise_issue(issue["id"], {
        "claims": [{
            "id": "ratio", "text": "The transfer ratio is 1:1.", "volatile": False,
            "citations": [{"source_id": official["id"], "authority_class": "issuer"}],
        }],
        "source_provenance": [
            {"source_id": tpg["id"], "url": tpg["url"], "role": "discovery"},
            {"source_id": official["id"], "url": official["url"]},
        ],
    }, created_by="writer")
    payload = package_input(official["id"])
    payload["expected_revision"] = revised["current_revision"]
    payload["idempotency_key"] = "manual-import-with-issuer-citation"

    created = packages.create(brand["id"], issue["id"], **payload)

    assert {row["source_id"] for row in created["discovery_sources"]} == {tpg["id"]}
    assert tpg["id"] not in {row["source_id"] for row in created["evidence_sources"]}
    exact = next(
        row for row in created["evidence_sources"] if row["source_id"] == official["id"]
    )
    assert exact["role"] == "primary_evidence"
    assert exact["authority_type"] == "issuer"


def test_revision_evidence_rejects_foreign_missing_and_unreferenced_sources(
    tmp_path, monkeypatch,
):
    brand, source, _, issue, editorial, _, packages = setup_package(tmp_path, monkeypatch)
    unreferenced = store.insert("sources", {
        "brand_id": brand["id"], "title": "Unreferenced official source",
        "url": "https://issuer.test/unreferenced", "source_type": "official",
        "body_summary": "Not cited", "lifecycle_state": "published",
        "scheduled_for": None, "external_source_id": "unreferenced",
    })
    other_brand = store.get_brand("demo-personal")
    foreign = store.insert("sources", {
        "brand_id": other_brand["id"], "title": "Foreign issuer terms",
        "url": "https://foreign.test/terms", "source_type": "official",
        "body_summary": "Other tenant", "lifecycle_state": "published",
        "scheduled_for": None, "external_source_id": "foreign",
    })

    with pytest.raises(DistributionPackageError, match="not governed supporting evidence"):
        packages.create(brand["id"], issue["id"], **package_input(unreferenced["id"]))

    revised = editorial.revise_issue(issue["id"], {
        "source_provenance": [
            {"source_id": source["id"], "url": source["url"]},
            {"source_id": foreign["id"], "url": foreign["url"]},
        ],
    }, created_by="writer")
    foreign_payload = package_input(foreign["id"])
    foreign_payload["expected_revision"] = revised["current_revision"]
    foreign_payload["idempotency_key"] = "foreign-r2"
    with pytest.raises(DistributionPackageError, match="does not belong to this brand"):
        packages.create(brand["id"], issue["id"], **foreign_payload)

    missing = editorial.revise_issue(issue["id"], {
        "source_provenance": [
            {"source_id": source["id"], "url": source["url"]},
            {"source_id": "missing-source", "url": "https://missing.test/terms"},
        ],
    }, created_by="writer")
    missing_payload = package_input("missing-source")
    missing_payload["expected_revision"] = missing["current_revision"]
    missing_payload["idempotency_key"] = "missing-r3"
    with pytest.raises(DistributionPackageError, match="does not belong to this brand"):
        packages.create(brand["id"], issue["id"], **missing_payload)


def test_unavailable_exact_revision_source_cannot_be_selected_as_primary(
    tmp_path, monkeypatch,
):
    brand, source, _, issue, editorial, _, packages = setup_package(tmp_path, monkeypatch)
    official = store.insert("sources", {
        "brand_id": brand["id"], "title": "Temporarily unavailable issuer page",
        "url": "https://issuer.test/unavailable", "source_type": "official",
        "body_summary": "Issuer terms", "lifecycle_state": "published",
        "scheduled_for": None, "external_source_id": "unavailable-official",
    })
    revised = editorial.revise_issue(issue["id"], {
        "source_provenance": [
            {"source_id": source["id"], "url": source["url"]},
            {"source_id": official["id"], "url": official["url"],
             "authority_type": "official"},
        ],
    }, created_by="writer")
    CanonicalSourceRevalidationStore(packages.database).record(
        brand["id"], official["id"], {
            "canonical_url": official["url"], "observed_at": "2026-09-02T12:00:00Z",
            "snapshot_fingerprint": "sha256:" + "3" * 64,
            "status": "unavailable", "confidence": "low",
            "rationale": ["issuer page could not be retrieved"],
            "idempotency_key": "official-unavailable",
        }, actor="canonical-revalidator",
    )
    payload = package_input(official["id"])
    payload["expected_revision"] = revised["current_revision"]
    payload["idempotency_key"] = "unavailable-r2"
    with pytest.raises(DistributionPackageError, match="canonical source is unavailable"):
        packages.create(brand["id"], issue["id"], **payload)
    assert store.rows("SELECT id FROM newsletter_distribution_packages") == []


def test_legacy_package_without_lineage_is_explicitly_non_authoritative_and_blocked(
    tmp_path, monkeypatch,
):
    brand, source, _, issue, _, _, packages = setup_package(tmp_path, monkeypatch)
    package = packages.create(brand["id"], issue["id"], **package_input(source["id"]))
    with packages._connect() as connection:
        connection.execute(
            "DELETE FROM newsletter_distribution_source_lineage WHERE package_id=?",
            (package["id"],),
        )

    legacy = packages.get(package["id"])

    assert legacy["source_lineage"] == []
    assert legacy["lineage"]["primary_evidence"] == {
        "id": source["id"], "label": source["title"], "authoritative": False,
        "canonical_status": "unknown", "legacy_pointer": True,
    }
    assert legacy["lineage"]["source"]["legacy_pointer"] is True
    assert legacy["lineage"]["source"]["authoritative"] is False
    assert legacy["governance"]["approval_ready"] is False
    assert legacy["governance"]["blockers"][0]["code"] == "primary_evidence_lineage_unknown"
    assert "Regenerate from the exact current newsletter revision" in legacy["governance"][
        "next_safe_action"
    ]
    graph = CampaignGraphStore(packages.database).get(package["campaign_id"])
    assert graph["source_lineage_status"] == "unknown_legacy_package"
    assert graph["source_id_authoritative"] is False


def test_x_artifact_edits_invalidate_approval_and_approval_is_revision_exact(tmp_path, monkeypatch):
    brand, source, _, issue, editorial, dispatcher, packages = setup_package(tmp_path, monkeypatch)
    package = packages.create(brand["id"], issue["id"], **package_input(source["id"]))
    fact_check_anchor(editorial, issue)
    item_id = package["artifacts"][0]["dispatch_item_id"]
    dispatcher.submit_for_approval(item_id, actor="writer")
    dispatcher.approve(item_id, revision=1, approver="chris")

    edited = dispatcher.edit(item_id, {"body": "A corrected, source-backed X draft."}, actor="writer")

    assert edited.revision == 2
    assert edited.status == Lifecycle.DRAFT
    assert edited.approval is None
    dispatcher.submit_for_approval(item_id, actor="writer")
    with pytest.raises(ValueError, match="revision is no longer current"):
        dispatcher.approve(item_id, revision=1, approver="chris")


def test_distribution_rest_and_mcp_surfaces_are_draft_only(tmp_path, monkeypatch):
    database = tmp_path / "distribution-api.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "test-password")
    with TestClient(app, headers={"Authorization": "Basic b3BlcmF0b3I6dGVzdC1wYXNzd29yZA=="}) as client:
        brand = store.get_brand("demo-brand")
        source = store.insert("sources", {
            "brand_id": brand["id"], "title": "Official terms", "url": "https://issuer.test",
            "source_type": "official", "body_summary": "Terms", "lifecycle_state": "published",
            "scheduled_for": None, "external_source_id": "terms-1",
        })
        candidate = editorial_store.upsert_candidate(
            brand["id"], "Explain terms", {"relevance": 1},
            supporting_sources=[{"source_id": source["id"], "url": source["url"]}],
        )
        issue = editorial_store.create_issue(
            brand["id"], {"subject": "Terms", "preview_text": "What to know",
                          "final_title": "Terms explained", "sections": [{"body": "Draft"}]},
            created_by="writer", candidate_id=candidate["id"],
        )
        payload = package_input(source["id"])
        payload.pop("actor")
        created = client.post(
            f"/api/newsletter-issues/{issue['id']}/distribution-package", json=payload,
        )
        assert created.status_code == 201
        package = created.json()
        assert package["created_by"] == "chris"
        assert all(item["dispatch"]["status"] == "draft" for item in package["artifacts"])
        assert package["lineage"]["primary_evidence"]["id"] == source["id"]
        assert package["discovery_sources"][0]["source_id"] == source["id"]
        assert client.get(f"/api/distribution-packages/{package['id']}").json()["id"] == package["id"]
        assert client.get("/api/brands/demo-brand/distribution-packages").json()[0]["id"] == package["id"]
        measurement = client.get(
            f"/api/distribution-packages/{package['id']}/measurement"
        ).json()
        assert measurement["campaign_id"] == package["campaign_id"]
        assert client.get(
            f"/api/distribution-packages/{package['id']}/membership-audit"
        ).json()

        from app.mcp_server import (
            get_distribution_campaign_measurement,
            get_distribution_membership_audit,
            get_newsletter_distribution_package,
            mcp,
        )
        mcp_package = get_newsletter_distribution_package(package["id"])
        assert mcp_package["id"] == package["id"]
        assert mcp_package["lineage"]["primary_evidence"]["id"] == source["id"]
        assert get_distribution_campaign_measurement(package["id"])["campaign_id"] == package["campaign_id"]
        assert get_distribution_membership_audit(package["id"])
        tools = set(mcp._tool_manager._tools)
        assert "create_newsletter_distribution_package" in tools
        assert not any(name.startswith("approve_distribution") or name.startswith("publish_distribution") for name in tools)

        # A migrated package lacking durable lineage fails closed on every read
        # surface; the historical source pointer is never upgraded to authority.
        with sqlite3.connect(database) as connection:
            connection.execute(
                "DELETE FROM newsletter_distribution_source_lineage WHERE package_id=?",
                (package["id"],),
            )
        rest_legacy = client.get(f"/api/distribution-packages/{package['id']}").json()
        assert rest_legacy["governance"]["approval_ready"] is False
        assert rest_legacy["lineage"]["primary_evidence"]["legacy_pointer"] is True
        mcp_legacy = get_newsletter_distribution_package(package["id"])
        assert mcp_legacy["governance"]["blockers"][0]["code"] == (
            "primary_evidence_lineage_unknown"
        )


def test_newsletter_completion_api_fails_closed_after_anchor_revision_changes(
    tmp_path, monkeypatch,
):
    brand, source, _, issue, editorial, _, packages = setup_package(tmp_path, monkeypatch)
    package = packages.create(brand["id"], issue["id"], **package_input(source["id"]))
    fact_check_anchor(editorial, issue)
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "completion-contract")
    auth = ("operator", "completion-contract")

    with TestClient(app) as client:
        shown = next(item for item in client.get(
            "/api/brands/demo-brand/newsletter-issues", auth=auth,
        ).json() if item["id"] == issue["id"])
        approved = client.post(
            f"/api/newsletter-issues/{issue['id']}/approve", auth=auth,
            json={"revision": 1, "review_token": shown["approval_scope"]["review_token"]},
        )
        assert approved.status_code == 200
        assert client.get(
            f"/api/newsletter-issues/{issue['id']}/export-preview", auth=auth,
        ).status_code == 200

        revised = client.patch(
            f"/api/newsletter-issues/{issue['id']}", auth=auth,
            json={"changes": {"preview_text": "Corrected source-backed preview"},
                  "change_note": "Correct the anchor"},
        )
        assert revised.status_code == 200
        assert revised.json()["current_revision"] == 2
        assert revised.json()["content"]["created_by"] == "chris"
        stale = client.get(f"/api/distribution-packages/{package['id']}", auth=auth).json()
        assert stale["status"] == "stale"
        assert stale["governance"]["blockers"][0]["code"] == "anchor_revision_stale"
        assert all(member["active"] == 0 for member in stale["memberships"])

        preview = client.get(
            f"/api/newsletter-issues/{issue['id']}/export-preview", auth=auth,
        )
        queued = client.post(
            f"/api/newsletter-issues/{issue['id']}/export-draft", auth=auth, json={},
        )
        assert preview.status_code == queued.status_code == 409
        assert "currently approved revision" in preview.json()["detail"]
        assert "credential" not in queued.json()["detail"].casefold()

        stale_policy = client.post(
            f"/api/newsletter-issues/{issue['id']}/policy-review", auth=auth,
            json={"revision": 1, "checklist": {}},
        )
        stale_quick_hit = client.post(
            f"/api/newsletter-issues/{issue['id']}/quick-hit-authorization", auth=auth,
            json={"revision": 1,
                  "reason": "A time-sensitive alert needs concise treatment."},
        )
        assert stale_policy.status_code == stale_quick_hit.status_code == 409
        assert "current revision" in stale_policy.json()["detail"]

        assert client.post(
            f"/api/newsletter-issues/{issue['id']}/archive", auth=auth,
            json={"reason": "Cannot skip abandonment"},
        ).status_code == 409
        assert client.post(
            f"/api/newsletter-issues/{issue['id']}/abandon", auth=auth,
            json={"reason": "Superseded after correction"},
        ).json()["lifecycle"] == "abandoned"
        assert client.post(
            f"/api/newsletter-issues/{issue['id']}/archive", auth=auth,
            json={"reason": "Cleanup reviewed"},
        ).json()["lifecycle"] == "archived"


def test_campaign_memberships_flight_and_measurement_are_explicit(tmp_path, monkeypatch):
    brand, source, _, issue, _, _, packages = setup_package(tmp_path, monkeypatch)
    payload = package_input(source["id"])
    payload.update({
        "flight_name": "launch-week",
        "flight_start": "2026-09-02T12:00:00+00:00",
        "flight_end": "2026-09-05T12:00:00+00:00",
    })
    payload["distribution"]["baseline"] = {"click_through_rate": 0.02}
    package = packages.create(brand["id"], issue["id"], **payload)

    assert package["anchor"]["asset_id"] == issue["id"]
    assert package["anchor"]["role"] == "anchor"
    assert package["anchor"]["attribution_primary"] == 1
    assert {item["channel"] for item in package["touchpoints"]} == {"email", "web", "x"}
    assert {item["phase"] for item in package["touchpoints"] if item["channel"] == "x"} == {
        "launch", "follow_up",
    }
    assert package["flights"][0]["name"] == "launch-week"
    assert len(package["relationships"]) == len(package["memberships"]) - 1

    post_id = package["artifacts"][0]["post_id"]
    for record_id, observed_at, impressions, clicks, conversions in (
        ("metric-1", "2026-09-03T12:00:00+00:00", 100, 10, 2),
        ("metric-duplicate", "2026-09-03T12:00:00+00:00", 100, 10, 2),
        ("outside-flight", "2026-09-08T12:00:00+00:00", 1000, 900, 50),
    ):
        store.insert("performance_records", {
            "id": record_id, "brand_id": brand["id"], "post_id": post_id,
            "source_id": None, "channel": "x", "observed_at": observed_at,
            "impressions": impressions, "clicks": clicks, "engagements": clicks + 2,
            "conversions": conversions, "revenue_cents": conversions * 500,
            "notes": "test observation",
        })
    AttributionStore(packages.database).create_tracked_link(
        brand_id=brand["id"], campaign_id=package["campaign_id"], artifact_id=post_id,
        cta_id="read", source="x", medium="organic-social",
        destination="https://points.test/offer", actor="tester",
    )

    measurement = packages.measurement(package["id"])

    assert measurement["campaign_id"] == package["campaign_id"]
    assert measurement["rollup"]["impressions"] == 100
    assert measurement["rollup"]["clicks"] == 10
    assert measurement["rollup"]["rates"]["click_through"] == {
        "value": 0.1, "numerator": 10, "denominator": 100,
    }
    assert measurement["rollup"]["aggregation_method"] == "ratio_of_additive_totals"
    assert measurement["deduplication"]["records_seen"] == 2
    assert measurement["deduplication"]["unique_records"] == 1
    assert measurement["conversion_confidence"] == "reported_not_independently_verified"
    assert measurement["baseline"] == {"click_through_rate": 0.02}
    assert measurement["tracked_links"][0]["campaign_id"] == package["campaign_id"]


def test_membership_moves_are_same_brand_and_audited(tmp_path, monkeypatch):
    brand, source, _, issue, editorial, dispatcher, packages = setup_package(tmp_path, monkeypatch)
    first = packages.create(brand["id"], issue["id"], **package_input(source["id"]))
    candidate = editorial.upsert_candidate(
        brand["id"], "Second sourced candidate", {"relevance": 0.8},
        supporting_sources=[{"source_id": source["id"], "url": source["url"]}],
    )
    second_issue = editorial.create_issue(
        brand["id"], {"subject": "Second", "preview_text": "Second issue",
                      "final_title": "Second issue", "sections": [{"body": "Draft"}]},
        created_by="writer", candidate_id=candidate["id"],
    )
    second_input = package_input(source["id"])
    second_input["idempotency_key"] = "second-r1"
    second = packages.create(brand["id"], second_issue["id"], **second_input)
    member = next(item for item in first["memberships"] if item["asset_type"] == "x_post")
    dispatch_id = next(
        artifact["dispatch_item_id"] for artifact in first["artifacts"]
        if artifact["post_id"] == member["asset_id"]
    )
    fact_check_anchor(editorial, issue)
    dispatcher.submit_for_approval(dispatch_id, actor="writer")
    approved = dispatcher.approve(dispatch_id, revision=1, approver="Chris")
    snapshots = ApprovalSnapshotStore(packages.database)
    before = snapshots.capture_dispatch(approved)
    assert before["campaign_id"] == first["campaign_id"]
    handoffs = ExecutionHandoffStore(packages.database, editorial, dispatcher)
    handoff = handoffs.ensure_for_brand(brand["id"])[0]

    moved = packages.move_membership(
        member["id"], second["id"], actor="chris", reason="Use in the follow-up campaign",
    )

    assert moved["campaign_id"] == second["campaign_id"]
    audit = packages.membership_audit(second["id"])
    assert audit[-1]["action"] == "moved"
    assert audit[-1]["actor"] == "chris"
    assert audit[-1]["reason"] == "Use in the follow-up campaign"
    assert not any(
        edge["from_membership_id"] == member["id"] or edge["to_membership_id"] == member["id"]
        for edge in packages.get(first["id"])["relationships"]
    )
    assert dispatcher.store.get(dispatch_id).status is Lifecycle.AWAITING_APPROVAL
    assert dispatcher.store.get(dispatch_id).approval is None
    assert snapshots.get(before["id"])["active"] is False
    assert handoffs.get(handoff["id"])["status"] == "stale"
    assert handoffs.audit(handoff["id"])[-1]["action"] == "invalidated"

    # Moving the touchpoint away from the package that produced it severs the
    # governed anchor. A fresh human click cannot repair that provenance gap.
    with pytest.raises(ValueError, match="no longer belongs to its governed package"):
        dispatcher.approve(dispatch_id, revision=1, approver="Chris")

    with pytest.raises(DistributionPackageError, match="already has a primary anchor"):
        packages.move_membership(
            first["anchor"]["id"], second["id"], actor="chris",
            reason="This would create two primary anchors",
        )


def test_distribution_package_requires_separate_exact_approvals_before_handoffs(
    tmp_path, monkeypatch,
):
    brand, source, _, issue, editorial, dispatcher, packages = setup_package(tmp_path, monkeypatch)
    package = packages.create(brand["id"], issue["id"], **package_input(source["id"]))
    handoffs = ExecutionHandoffStore(packages.database, editorial, dispatcher)
    assert handoffs.ensure_for_brand(brand["id"]) == []

    editorial.transition(issue["id"], IssueLifecycle.OUTLINE)
    editorial.transition(issue["id"], IssueLifecycle.DRAFT)
    editorial.record_fact_check(issue["id"], expected_revision=1, reviewer="Chris", verdicts=[])
    editorial.approve_issue(issue["id"], approver="Chris", expected_revision=1)
    first_dispatch = package["artifacts"][0]["dispatch_item_id"]
    dispatcher.submit_for_approval(first_dispatch, actor="writer")
    dispatcher.approve(first_dispatch, revision=1, approver="Chris")

    tasks = handoffs.ensure_for_brand(brand["id"])

    assert {task["provider"] for task in tasks} == {"beehiiv", "x"}
    assert {task["resource_id"] for task in tasks} == {issue["id"], first_dispatch}
    assert all(task.get("campaign_id") == package["campaign_id"] for task in tasks)
    assert package["artifacts"][1]["dispatch_item_id"] not in {
        task["resource_id"] for task in tasks
    }


def test_package_x_requires_current_anchor_fact_check_and_exact_review(
    tmp_path, monkeypatch,
):
    brand, source, _, issue, editorial, dispatcher, packages = setup_package(
        tmp_path, monkeypatch,
    )
    package = packages.create(brand["id"], issue["id"], **package_input(source["id"]))
    dispatch_id = package["artifacts"][0]["dispatch_item_id"]

    with pytest.raises(ValueError, match="lacks a current governed fact check"):
        dispatcher.submit_for_approval(dispatch_id, actor="writer")
    assert dispatcher.store.get(dispatch_id).status is Lifecycle.DRAFT

    fact_check_anchor(editorial, issue)
    awaiting = dispatcher.submit_for_approval(dispatch_id, actor="writer")
    snapshots = ApprovalSnapshotStore(packages.database)
    proposed = snapshots.proposed_dispatch(awaiting)
    approved, evidence = snapshots.approve_dispatch(
        dispatch_id, revision=awaiting.revision,
        review_token=proposed["review_token"], approver="Chris",
    )
    assert approved.status is Lifecycle.APPROVED
    assert evidence["active"] is True


def test_current_primary_conflict_invalidates_exact_approval_and_pending_claim(
    tmp_path, monkeypatch,
):
    brand, source, _, issue, editorial, dispatcher, packages = setup_package(
        tmp_path, monkeypatch,
    )
    package = packages.create(brand["id"], issue["id"], **package_input(source["id"]))
    fact_check_anchor(editorial, issue)
    dispatch_id = package["artifacts"][0]["dispatch_item_id"]
    awaiting = dispatcher.submit_for_approval(dispatch_id, actor="writer")
    snapshots = ApprovalSnapshotStore(packages.database)
    scope = snapshots.proposed_dispatch(awaiting)
    _, evidence = snapshots.approve_dispatch(
        dispatch_id, revision=1, review_token=scope["review_token"], approver="Chris",
    )
    handoffs = ExecutionHandoffStore(packages.database, editorial, dispatcher)
    task = next(
        item for item in handoffs.ensure_for_brand(brand["id"])
        if item["resource_id"] == dispatch_id
    )
    agents = ExecutionAgentRegistry(packages.database)
    agents.configure(brand["id"], "browser-agent", "browser")
    agents.heartbeat(brand["id"], "browser-agent")

    CanonicalSourceRevalidationStore(packages.database).record(
        brand["id"], source["id"], {
            "canonical_url": source["url"], "observed_at": "2026-09-02T12:00:00Z",
            "snapshot_fingerprint": "sha256:" + "9" * 64,
            "status": "conflict", "confidence": "high",
            "rationale": ["current issuer terms contradict the package claim"],
            "idempotency_key": "primary-conflict",
        }, actor="canonical-revalidator",
    )

    with pytest.raises(ExecutionHandoffError, match="no longer current"):
        handoffs.claim(task["id"], actor="browser-agent")
    assert snapshots.get(evidence["id"])["active"] is False
    assert handoffs.get(task["id"])["status"] == "stale"


def test_package_batch_approval_is_atomic_when_primary_health_drifts(
    tmp_path, monkeypatch,
):
    brand, source, _, issue, editorial, dispatcher, packages = setup_package(
        tmp_path, monkeypatch,
    )
    package = packages.create(brand["id"], issue["id"], **package_input(source["id"]))
    fact_check_anchor(editorial, issue)
    snapshots = ApprovalSnapshotStore(packages.database)
    members = []
    for artifact in package["artifacts"]:
        awaiting = dispatcher.submit_for_approval(
            artifact["dispatch_item_id"], actor="writer",
        )
        members.append({
            "id": awaiting.id, "revision": awaiting.revision,
            "review_token": snapshots.proposed_dispatch(awaiting)["review_token"],
        })
    CanonicalSourceRevalidationStore(packages.database).record(
        brand["id"], source["id"], {
            "canonical_url": source["url"], "observed_at": "2026-09-02T12:00:00Z",
            "snapshot_fingerprint": "sha256:" + "8" * 64,
            "status": "drift", "confidence": "high",
            "rationale": ["canonical terms changed after review was opened"],
            "idempotency_key": "batch-drift",
        }, actor="canonical-revalidator",
    )

    with pytest.raises(ValueError, match="currently drift"):
        snapshots.approve_dispatch_batch(members, approver="Chris", batch_id="batch-1")
    assert all(
        dispatcher.store.get(member["id"]).status is Lifecycle.AWAITING_APPROVAL
        for member in members
    )


def test_traffic_package_is_blocked_until_destination_is_bound(tmp_path, monkeypatch):
    brand, source, _, issue, _, dispatcher, packages = setup_package(tmp_path, monkeypatch)
    payload = package_input(source["id"])
    payload["web"].pop("url")
    package = packages.create(brand["id"], issue["id"], **payload)
    dispatch_id = package["artifacts"][0]["dispatch_item_id"]

    assert package["governance"]["approval_ready"] is False
    assert package["governance"]["blockers"][0]["code"] == "x_destination_required"
    with pytest.raises(Exception, match="governed destination"):
        dispatcher.submit_for_approval(dispatch_id, actor="writer")

    bound = packages.bind_destination(
        package["id"], "https://points.test/guide", actor="writer",
    )
    assert bound["governance"]["approval_ready"] is True
    assert all(item["tracked_url"] in item["body"] for item in bound["artifacts"])
    revisions = [item["dispatch"]["revision"] for item in bound["artifacts"]]
    replay = packages.bind_destination(
        package["id"], "https://points.test/guide", actor="writer",
    )
    assert [item["dispatch"]["revision"] for item in replay["artifacts"]] == revisions


def test_new_anchor_revision_atomically_stales_package_dispatches_and_handoffs(
    tmp_path, monkeypatch,
):
    brand, source, _, issue, editorial, dispatcher, packages = setup_package(tmp_path, monkeypatch)
    package = packages.create(brand["id"], issue["id"], **package_input(source["id"]))
    dispatch_id = package["artifacts"][0]["dispatch_item_id"]
    fact_check_anchor(editorial, issue)
    dispatcher.submit_for_approval(dispatch_id, actor="writer")
    dispatcher.approve(dispatch_id, revision=1, approver="Chris")
    snapshots = ApprovalSnapshotStore(packages.database)
    snapshot = snapshots.capture_dispatch(dispatcher.store.get(dispatch_id))
    approved_issue = editorial.approve_issue(
        issue["id"], approver="Chris", expected_revision=1,
    )
    newsletter_snapshot = snapshots.capture_newsletter(approved_issue)
    handoffs = ExecutionHandoffStore(packages.database, editorial, dispatcher)
    task = next(
        item for item in handoffs.ensure_for_brand(brand["id"])
        if item["resource_id"] == dispatch_id
    )

    revised = editorial.revise_issue(
        issue["id"], {"preview_text": "Corrected source-backed preview"},
        created_by="writer", change_note="Correct the anchor",
    )

    assert revised["current_revision"] == 2
    stale = packages.get(package["id"])
    assert stale["status"] == "stale"
    assert stale["governance"]["blockers"][0]["code"] == "anchor_revision_stale"
    assert dispatcher.store.get(dispatch_id).status is Lifecycle.CANCELLED
    assert snapshots.get(snapshot["id"])["active"] is False
    assert snapshots.get(newsletter_snapshot["id"])["active"] is False
    assert handoffs.get(task["id"])["status"] == "stale"
    assert stale["memberships"]
    assert all(member["active"] == 0 for member in stale["memberships"])
    assert any(
        entry["action"] == "deactivated"
        for entry in packages.membership_audit(package["id"])
    )
    with pytest.raises(DistributionPackageError, match="must be regenerated"):
        packages.bind_destination(package["id"], "https://points.test/new", actor="writer")

    r2_input = package_input(source["id"])
    r2_input["expected_revision"] = 2
    r2_input["idempotency_key"] = "issue-1-distribution-r2"
    r2_input["email"] = {
        "subject": "Offer explained, corrected",
        "preview_text": "Corrected source-backed preview",
    }
    r2 = packages.create(brand["id"], issue["id"], **r2_input)

    assert r2["issue"] == {"id": issue["id"], "revision": 2}
    assert r2["campaign_id"] != package["campaign_id"]
    assert r2["anchor"]["active"] == 1
    assert r2["anchor"]["attribution_primary"] == 1
    assert sum(
        member["active"] and member["attribution_primary"]
        for member in [*stale["memberships"], *r2["memberships"]]
    ) == 1


def test_newsletter_rejection_invalidates_same_loop_package_and_x_approval(
    tmp_path, monkeypatch,
):
    brand, source, _, issue, editorial, dispatcher, packages = setup_package(tmp_path, monkeypatch)
    package = packages.create(brand["id"], issue["id"], **package_input(source["id"]))
    dispatch_id = package["artifacts"][0]["dispatch_item_id"]
    fact_check_anchor(editorial, issue)
    dispatcher.submit_for_approval(dispatch_id, actor="writer")
    dispatcher.approve(dispatch_id, revision=1, approver="Chris")
    snapshots = ApprovalSnapshotStore(packages.database)
    x_snapshot = snapshots.capture_dispatch(dispatcher.store.get(dispatch_id))

    rejected = editorial.reject_issue(
        issue["id"], actor="Chris", reason="The framing needs another pass",
        expected_revision=1,
    )

    assert rejected["lifecycle"] == "draft"
    assert rejected["current_revision"] == 2
    stale = packages.get(package["id"])
    assert stale["status"] == "stale"
    assert dispatcher.store.get(dispatch_id).status is Lifecycle.CANCELLED
    assert snapshots.get(x_snapshot["id"])["active"] is False
    assert all(member["active"] == 0 for member in stale["memberships"])


def test_r2_creation_reconciles_pre_fix_stale_package_with_orphan_active_memberships(
    tmp_path, monkeypatch,
):
    brand, source, _, issue, editorial, dispatcher, packages = setup_package(tmp_path, monkeypatch)
    r1 = packages.create(brand["id"], issue["id"], **package_input(source["id"]))
    dispatch_id = r1["artifacts"][0]["dispatch_item_id"]
    fact_check_anchor(editorial, issue)
    dispatcher.submit_for_approval(dispatch_id, actor="writer")
    approved = dispatcher.approve(dispatch_id, revision=1, approver="Chris")
    snapshots = ApprovalSnapshotStore(packages.database)
    snapshot = snapshots.capture_dispatch(approved)
    handoffs = ExecutionHandoffStore(packages.database, editorial, dispatcher)
    task = next(
        item for item in handoffs.ensure_for_brand(brand["id"])
        if item["resource_id"] == dispatch_id
    )
    editorial.revise_issue(
        issue["id"], {"preview_text": "Corrected r2 preview"},
        created_by="writer", change_note="Advance anchor",
    )

    # Recreate the exact pre-fix operating condition: package status was stale,
    # but its memberships and membership-bound execution evidence remained live.
    with packages._connect() as connection:
        connection.execute(
            "UPDATE campaign_asset_memberships SET active=1 WHERE package_id=?",
            (r1["id"],),
        )
        connection.execute(
            """INSERT INTO approval_snapshots
               (id,brand_id,account_ref,campaign_id,asset_membership_id,resource_type,
                resource_id,action_type,destination,intended_schedule,revision,approver,
                approved_at,material_fingerprint,material_json,created_at)
               SELECT 'pre-fix-orphan-snapshot',brand_id,account_ref,campaign_id,
                      asset_membership_id,resource_type,resource_id,action_type,destination,
                      intended_schedule,revision,approver,approved_at,'sha256:pre-fix-orphan',
                      material_json,created_at FROM approval_snapshots WHERE id=?""",
            (snapshot["id"],),
        )
        connection.execute(
            """UPDATE execution_tasks SET status='pending',updated_at=? WHERE id=?""",
            (store.now(), task["id"]),
        )
    assert packages.get(r1["id"])["status"] == "stale"
    assert any(member["active"] for member in packages.get(r1["id"])["memberships"])
    with packages._connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM approval_snapshot_invalidations WHERE snapshot_id=?",
            ("pre-fix-orphan-snapshot",),
        ).fetchone() is None
    assert handoffs.get(task["id"])["status"] == "pending"

    r2_input = package_input(source["id"])
    r2_input.update({
        "expected_revision": 2,
        "idempotency_key": "legacy-orphan-reconciled-r2",
    })
    r2 = packages.create(brand["id"], issue["id"], **r2_input)

    old = packages.get(r1["id"])
    assert all(member["active"] == 0 for member in old["memberships"])
    assert snapshots.get("pre-fix-orphan-snapshot")["active"] is False
    assert handoffs.get(task["id"])["status"] == "stale"
    assert r2["anchor"]["active"] == 1
    assert r2["anchor"]["attribution_primary"] == 1
    assert sum(
        member["active"] and member["attribution_primary"]
        for member in [*old["memberships"], *r2["memberships"]]
    ) == 1
