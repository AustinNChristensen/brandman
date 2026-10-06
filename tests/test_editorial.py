import sqlite3

import pytest

from app.editorial import (
    ApprovalBlocked,
    EditorialStore,
    ExportConflict,
    InvalidTransition,
    IssueLifecycle,
    candidate_duplicate_identity,
    score_editorial_candidate,
)


@pytest.fixture
def store(tmp_path):
    return EditorialStore(tmp_path / "editorial.db", clock=lambda: "2026-09-01T12:00:00+00:00")


def issue_content(claims=None):
    return {
        "editorial_thesis": "A deadline makes this transfer bonus actionable.",
        "target_reader": "Readers",
        "intended_outcome": "Choose whether to transfer",
        "working_title": "Transfer bonus",
        "final_title": "The transfer bonus worth checking today",
        "subject": "This transfer bonus expires soon",
        "preview_text": "The useful math and the deadline.",
        "sections": [{"heading": "The deal", "body": "Details"}],
        "cta": {"label": "Check your account", "url": "https://example.test"},
        "seo": {"title": "Transfer bonus guide", "description": "Current details"},
        "claims": claims or [],
        "source_provenance": [{"source_id": "source-1", "url": "https://issuer.test/offer"}],
    }


def advance_to_fact_checked(store, issue_id):
    store.transition(issue_id, IssueLifecycle.OUTLINE)
    store.transition(issue_id, IssueLifecycle.DRAFT)
    issue = store.get_issue(issue_id)
    verdicts = [
        {"claim_id": str(claim.get("id") or f"claim-{index}"), "verified": True}
        for index, claim in enumerate(issue["content"]["claims"], start=1)
    ]
    store.record_fact_check(
        issue_id, expected_revision=issue["current_revision"], reviewer="Chris", verdicts=verdicts,
    )
    return store.get_issue(issue_id)


def test_candidate_identity_and_score_are_deterministic_and_upserted(store):
    first_id = candidate_duplicate_identity(
        "brand-1", "Big Offer!", source_ids=["b", "a"],
        source_urls=["HTTPS://EXAMPLE.COM/deal/?utm_source=x"],
    )
    second_id = candidate_duplicate_identity(
        "brand-1", "big offer", source_ids=["a", "b"],
        source_urls=["https://example.com/deal"],
    )
    assert first_id == second_id
    dimensions = {"relevance": 1, "urgency": .5, "reader_value": 1, "confidence": .8}
    assert score_editorial_candidate(dimensions) == score_editorial_candidate(dimensions)
    first = store.upsert_candidate("brand-1", "Big Offer", dimensions)
    second = store.upsert_candidate(
        "brand-1", "Big Offer", dimensions, duplicate_identity=first["duplicate_identity"], summary="updated"
    )
    assert second["id"] == first["id"]
    assert second["summary"] == "updated"
    assert len(store.list_candidates("brand-1")) == 1


def test_issue_revision_contains_full_content_and_edit_invalidates_approval(store):
    issue = store.create_issue("brand-1", issue_content(), created_by="Chris")
    assert issue["lifecycle"] == "idea"
    assert issue["content"]["sections"][0]["heading"] == "The deal"
    advance_to_fact_checked(store, issue["id"])
    approved = store.approve_issue(issue["id"], approver="Chris", expected_revision=1)
    assert approved["approval_valid"] is True
    revised = store.revise_issue(issue["id"], {"subject": "A better subject"}, created_by="Chris")
    assert revised["current_revision"] == 2
    assert revised["lifecycle"] == "draft"
    assert revised["approval_valid"] is False
    assert revised["approved_by"] is None
    assert [row["revision"] for row in store.list_revisions(issue["id"])] == [1, 2]


def test_reject_exact_fact_checked_revision_returns_fresh_draft_and_audits_reason(store):
    issue = store.create_issue("brand-1", issue_content(), created_by="writer")
    advance_to_fact_checked(store, issue["id"])

    with pytest.raises(ApprovalBlocked, match="rejection revision"):
        store.reject_issue(
            issue["id"], actor="Chris", reason="Headline overstates the offer",
            expected_revision=2,
        )
    with pytest.raises(ValueError, match="reason is required"):
        store.reject_issue(issue["id"], actor="Chris", reason="", expected_revision=1)

    rejected = store.reject_issue(
        issue["id"], actor="Chris", reason="Headline overstates the offer",
        expected_revision=1,
    )
    assert rejected["lifecycle"] == "draft"
    assert rejected["current_revision"] == 2
    assert rejected["approval_valid"] is False
    assert rejected["content"]["subject"] == issue["content"]["subject"]
    revisions = store.list_revisions(issue["id"])
    assert revisions[1]["change_note"] == "Review rejected: Headline overstates the offer"
    event = store.list_issue_history(issue["id"])[-1]
    assert (event["action"], event["from_state"], event["to_state"], event["revision"]) == (
        "rejected", "fact_checked", "draft", 1,
    )
    with pytest.raises(InvalidTransition, match="only a fact_checked issue"):
        store.reject_issue(
            issue["id"], actor="Chris", reason="duplicate rejection", expected_revision=2,
        )


def test_volatile_claim_requires_verified_primary_source(store):
    claims = [{
        "text": "The offer ends September 5.", "volatile": True,
        "citations": [{"url": "https://blog.test", "authority_class": "aggregator", "verified_at": "2026-09-01T10:00:00Z"}],
    }]
    issue = store.create_issue("brand-1", issue_content(claims), created_by="writer")
    store.transition(issue["id"], IssueLifecycle.OUTLINE)
    store.transition(issue["id"], IssueLifecycle.DRAFT)
    with pytest.raises(ApprovalBlocked, match="unknown canonical source"):
        store.record_fact_check(
            issue["id"], expected_revision=1, reviewer="Chris",
            verdicts=[{"claim_id": "claim-1", "verified": True}],
        )
    fixed = store.revise_issue(issue["id"], {"claims": [{
        **claims[0], "citations": [{
            "source_id": "issuer-1", "authority_class": "primary",
            "verified_at": "2026-09-01T11:00:00Z",
        }],
    }], "source_provenance": [{"source_id": "issuer-1", "url": "https://issuer.test/offer"}]}, created_by="writer")
    store.record_fact_check(
        issue["id"], expected_revision=fixed["current_revision"], reviewer="Chris",
        verdicts=[{"claim_id": "claim-1", "verified": True}],
    )
    approved = store.approve_issue(issue["id"], approver="Chris", expected_revision=fixed["current_revision"])
    assert approved["lifecycle"] == "approved"


def test_export_receipt_is_idempotent_and_controls_lifecycle(store):
    issue = store.create_issue("brand-1", issue_content(), created_by="writer")
    advance_to_fact_checked(store, issue["id"])
    store.approve_issue(issue["id"], approver="Chris", expected_revision=1)
    prepared = store.prepare_export(issue["id"])
    assert prepared["payload"]["subject"] == "This transfer bonus expires soon"
    assert prepared["idempotency_key"].endswith(":r1")
    receipt = store.record_export_receipt(
        issue["id"], expected_revision=1, idempotency_key="export-1",
        external_id="beehiiv-9", preview_url="https://beehiiv.test/preview/9",
        payload_fingerprint="sha256:abc",
    )
    duplicate = store.record_export_receipt(
        issue["id"], expected_revision=1, idempotency_key="export-1",
        external_id="beehiiv-9", preview_url="https://beehiiv.test/preview/9",
        payload_fingerprint="sha256:abc",
    )
    assert duplicate["id"] == receipt["id"]
    exported = store.get_issue(issue["id"])
    assert exported["lifecycle"] == "exported"
    assert exported["beehiiv_external_id"] == "beehiiv-9"
    with pytest.raises(ExportConflict):
        store.record_export_receipt(
            issue["id"], expected_revision=1, idempotency_key="export-1",
            external_id="different", preview_url=None, payload_fingerprint="sha256:abc",
        )
    scheduled = store.schedule_issue(issue["id"], "2026-09-02T15:30:00-06:00")
    assert scheduled["scheduled_for"].endswith("-06:00")
    published = store.mark_published(issue["id"], "2026-09-02T21:31:00Z")
    assert published["lifecycle"] == "published"
    assert store.list_issues("brand-1", lifecycle="published")[0]["id"] == issue["id"]


def test_invalid_transition_and_schema_initialization_are_safe(store):
    issue = store.create_issue("brand-1", issue_content(), created_by="writer")
    with pytest.raises(InvalidTransition):
        store.transition(issue["id"], IssueLifecycle.DRAFT)
    store.init_schema()
    store.init_schema()
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("SELECT count(*) FROM editorial_schema_migrations").fetchone()[0] == 3


def test_candidate_cleanup_is_governed_audited_and_hidden_by_default(store):
    candidate = store.upsert_candidate(
        "brand-1", "Old bonus rumor", {"relevance": .2, "confidence": .1},
    )
    with pytest.raises(ValueError, match="reason is required"):
        store.abandon_candidate(candidate["id"], actor="Chris", reason="")

    abandoned = store.abandon_candidate(
        candidate["id"], actor="Chris", reason="Primary source disproved it",
    )
    assert abandoned["status"] == "abandoned"
    assert store.list_candidates("brand-1") == []
    assert store.list_candidates("brand-1", status=None)[0]["id"] == candidate["id"]

    archived = store.archive_candidate(
        candidate["id"], actor="Chris", reason="Cleanup reviewed",
    )
    assert archived["status"] == "archived"
    history = store.list_candidate_history(candidate["id"])
    assert [(event["from_state"], event["to_state"], event["actor"]) for event in history] == [
        ("open", "abandoned", "Chris"), ("abandoned", "archived", "Chris"),
    ]
    # Periodic source sync may encounter the same identity again, but it cannot
    # silently revive or mutate a governed terminal decision.
    repeated = store.upsert_candidate(
        "brand-1", "Old bonus rumor", {"relevance": 1},
        duplicate_identity=candidate["duplicate_identity"], summary="revived",
    )
    assert repeated["status"] == "archived"
    assert repeated["summary"] == ""


def test_issue_abandon_then_archive_preserves_revisions_and_audit(store):
    issue = store.create_issue("brand-1", issue_content(), created_by="writer")
    store.transition(issue["id"], IssueLifecycle.OUTLINE)
    store.transition(issue["id"], IssueLifecycle.DRAFT)
    with pytest.raises(InvalidTransition, match="draft to archived"):
        store.archive_issue(issue["id"], actor="Chris", reason="Too early")

    abandoned = store.abandon_issue(
        issue["id"], actor="Chris", reason="Issuer withdrew the offer",
    )
    assert abandoned["lifecycle"] == "abandoned"
    assert store.list_issues("brand-1") == []
    with pytest.raises(InvalidTransition, match="cannot be revised"):
        store.revise_issue(issue["id"], {"subject": "revive"}, created_by="writer")

    archived = store.archive_issue(
        issue["id"], actor="Chris", reason="Postmortem complete",
    )
    assert archived["lifecycle"] == "archived"
    assert len(store.list_revisions(issue["id"])) == 1
    assert store.list_issues("brand-1", include_inactive=True)[0]["id"] == issue["id"]
    history = store.list_issue_history(issue["id"])
    assert [(event["action"], event["revision"]) for event in history] == [
        ("abandoned", 1), ("archived", 1),
    ]


def test_active_delivery_states_cannot_be_abandoned_or_archived(store):
    approved = store.create_issue("brand-1", issue_content(), created_by="writer")
    advance_to_fact_checked(store, approved["id"])
    store.approve_issue(approved["id"], approver="Chris", expected_revision=1)
    with pytest.raises(InvalidTransition, match="approved to abandoned"):
        store.abandon_issue(approved["id"], actor="Chris", reason="Unsafe cleanup")
    with pytest.raises(InvalidTransition, match="approved to archived"):
        store.archive_issue(approved["id"], actor="Chris", reason="Unsafe cleanup")

    store.record_export_receipt(
        approved["id"], expected_revision=1, idempotency_key="cleanup-export",
        external_id="beehiiv-cleanup", preview_url=None, payload_fingerprint="sha256:cleanup",
    )
    store.schedule_issue(approved["id"], "2026-09-03T12:00:00Z")
    with pytest.raises(InvalidTransition, match="scheduled to archived"):
        store.archive_issue(approved["id"], actor="Chris", reason="Would hide live work")


def test_active_export_job_blocks_cleanup_even_if_issue_state_is_inconsistent(store):
    issue = store.create_issue("brand-1", issue_content(), created_by="writer")
    store.transition(issue["id"], IssueLifecycle.OUTLINE)
    store.transition(issue["id"], IssueLifecycle.DRAFT)
    with sqlite3.connect(store.database) as connection:
        connection.execute(
            "CREATE TABLE durable_jobs (job_type TEXT, status TEXT, payload TEXT)",
        )
        connection.execute(
            "INSERT INTO durable_jobs VALUES (?,?,?)",
            ("beehiiv.newsletter.export_draft", "queued", '{"issue_id":"%s"}' % issue["id"]),
        )
    with pytest.raises(InvalidTransition, match="active newsletter export job"):
        store.abandon_issue(issue["id"], actor="Chris", reason="Must cancel delivery first")


def test_fact_check_is_revision_bound_complete_and_reviewable(store):
    claim = {
        "id": "deadline", "text": "Offer ends soon", "volatile": False,
        "citations": [{"source_id": "source-1", "url": "https://issuer.test/offer"}],
    }
    issue = store.create_issue("brand-1", issue_content([claim]), created_by="writer")
    store.transition(issue["id"], IssueLifecycle.OUTLINE)
    store.transition(issue["id"], IssueLifecycle.DRAFT)
    with pytest.raises(InvalidTransition, match="record_fact_check"):
        store.transition(issue["id"], IssueLifecycle.FACT_CHECKED)
    with pytest.raises(ApprovalBlocked, match="positive reviewer verdict"):
        store.record_fact_check(issue["id"], expected_revision=1, reviewer="Chris")
    checked = store.record_fact_check(
        issue["id"], expected_revision=1, reviewer="Chris",
        verdicts=[{"claim_id": "deadline", "verified": True, "notes": "Matched source"}],
    )
    assert checked["passed"] is True
    assert store.get_issue(issue["id"])["lifecycle"] == "fact_checked"
    assert store.get_fact_check(issue["id"], 1)["reviewer"] == "Chris"


def test_fact_check_rejects_unknown_source_identity(store):
    claim = {"id": "amount", "text": "100 credits", "citations": [{"source_id": "loose-source"}]}
    issue = store.create_issue("brand-1", issue_content([claim]), created_by="writer")
    store.transition(issue["id"], IssueLifecycle.OUTLINE)
    store.transition(issue["id"], IssueLifecycle.DRAFT)
    with pytest.raises(ApprovalBlocked, match="unknown canonical source"):
        store.record_fact_check(
            issue["id"], expected_revision=1, reviewer="Chris",
            verdicts=[{"claim_id": "amount", "verified": True}],
        )


def test_source_less_issue_requires_an_explicit_original_or_opinion_basis(store):
    content = issue_content()
    content["source_provenance"] = []
    issue = store.create_issue("brand-1", content, created_by="writer")
    store.transition(issue["id"], IssueLifecycle.OUTLINE)
    store.transition(issue["id"], IssueLifecycle.DRAFT)

    governance = store.get_issue(issue["id"])["governance"]
    assert governance["reviewable"] is False
    assert any("declare content_basis" in item["message"] for item in governance["blockers"])
    with pytest.raises(ApprovalBlocked, match="declare content_basis"):
        store.record_fact_check(issue["id"], expected_revision=1, reviewer="Chris")

    revised = store.revise_issue(issue["id"], {
        "content_basis": {
            "kind": "original_analysis",
            "statement": "DemoBrand's own redemption-value decision framework.",
        },
    }, created_by="writer")
    checked = store.record_fact_check(
        issue["id"], expected_revision=revised["current_revision"], reviewer="Chris",
    )
    assert checked["passed"] is True
