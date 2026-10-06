from app.operator_workflow import build_operator_workflow


def candidate():
    return {"id": "candidate-1", "title": "Offer", "updated_at": "2026-09-02T10:00:00Z"}


def issue(*, lifecycle="draft", approved=False):
    return {
        "id": "issue-1", "candidate_id": "candidate-1", "current_revision": 2,
        "lifecycle": lifecycle, "approval_valid": approved,
        "updated_at": "2026-09-02T11:00:00Z",
        "content": {"claims": [], "source_provenance": []},
        "approval_scope": {
            "revision": 2, "resource_id": "issue-1", "action_type": "create_draft",
            "destination": "beehiiv:draft", "review_token": "review-v1:abc",
            "material_fingerprint": "sha256:abc",
        },
    }


def fact_check():
    return {
        "issue_id": "issue-1", "revision": 2, "passed": True, "reviewer": "Chris",
        "content_fingerprint": "sha256:abc", "created_at": "2026-09-02T12:00:00Z",
        "verdicts": [],
    }


def package(*, dispatch_status="draft", approval_ready=True):
    return {
        "id": "package-1", "issue_id": "issue-1", "candidate_id": "candidate-1",
        "campaign_id": "campaign-1", "status": "draft",
        "updated_at": "2026-09-02T13:00:00Z",
        "governance": {
            "approval_ready": approval_ready,
            "next_safe_action": "Bind the canonical destination.",
        },
        "artifacts": [
            {"post_id": "post-1", "dispatch_item_id": "dispatch-1",
             "dispatch": {"id": "dispatch-1", "status": dispatch_status}},
            {"post_id": "post-2", "dispatch_item_id": "dispatch-2",
             "dispatch": {"id": "dispatch-2", "status": dispatch_status}},
        ],
    }


def build(**overrides):
    values = {
        "candidates": [], "issues": [], "fact_checks": {}, "packages": [],
        "handoffs": [], "connectors": [], "performance": [],
    }
    values.update(overrides)
    return build_operator_workflow(**values)


def test_empty_loop_has_one_concrete_source_action():
    workflow = build()
    assert workflow["next_action"] == {
        "code": "ingest_source", "target": "sources-panel",
        "text": "Ingest one governed source item, then let BrandOS deduplicate and score it.",
    }
    assert workflow["progress_percent"] == 0
    assert [stage["status"] for stage in workflow["stages"]].count("current") == 1


def test_workflow_includes_newsletter_and_orders_fact_check_before_distribution():
    selected = build(candidates=[candidate()])
    assert selected["next_action"]["code"] == "draft_newsletter"

    drafted = build(candidates=[candidate()], issues=[issue()])
    assert drafted["next_action"]["code"] == "fact_check"
    assert [stage["key"] for stage in drafted["stages"]] == [
        "source", "candidate", "newsletter", "fact_check", "distribution",
        "approval", "handoff", "receipt", "measurement",
    ]

    checked = build(
        candidates=[candidate()], issues=[issue(lifecycle="fact_checked")],
        fact_checks={"issue-1": fact_check()},
    )
    assert checked["next_action"]["code"] == "create_distribution"

    persisted = fact_check()
    persisted["passed"] = 1
    checked_from_sqlite = build(
        candidates=[candidate()], issues=[issue(lifecycle="fact_checked")],
        fact_checks={"issue-1": persisted},
    )
    assert checked_from_sqlite["next_action"]["code"] == "create_distribution"


def test_focus_preserves_promoted_candidate_lineage_when_active_list_omits_it():
    workflow = build(
        candidates=[], issues=[issue(lifecycle="fact_checked")],
        fact_checks={"issue-1": fact_check()}, packages=[package()],
    )

    assert workflow["focus"]["candidate_id"] == "candidate-1"
    assert workflow["focus"]["issue_id"] == "issue-1"
    assert workflow["focus"]["package_id"] == "package-1"
    assert workflow["stages"][1]["status"] == "done"


def test_focus_orders_equivalent_iso_offsets_by_instant_not_text():
    older = {**issue(), "id": "older", "candidate_id": "older-candidate",
             "updated_at": "2026-09-02T06:21:58+00:00"}
    newer = {**issue(), "id": "newer", "candidate_id": "newer-candidate",
             "updated_at": "2026-09-02T05:35:31-06:00"}
    workflow = build(issues=[older, newer])
    assert workflow["focus"]["issue_id"] == "newer"


def test_distribution_governance_blocker_is_the_only_next_action():
    workflow = build(
        candidates=[candidate()], issues=[issue(lifecycle="fact_checked")],
        fact_checks={"issue-1": fact_check()}, packages=[package(approval_ready=False)],
    )
    assert workflow["next_action"] == {
        "code": "resolve_distribution_blocker", "target": "distribution-panel",
        "text": "Bind the canonical destination.",
    }
    assert workflow["stages"][4]["status"] == "current"


def test_exact_approval_requires_newsletter_and_every_x_draft():
    base = {
        "candidates": [candidate()], "fact_checks": {"issue-1": fact_check()},
    }
    newsletter_first = build(
        **base, issues=[issue(lifecycle="fact_checked")],
        packages=[package(dispatch_status="approved")],
    )
    assert newsletter_first["next_action"]["text"].startswith("Review and approve or reject the exact newsletter")

    partly_approved = package(dispatch_status="approved")
    partly_approved["artifacts"][1]["dispatch"]["status"] = "awaiting_approval"
    x_next = build(
        **base, issues=[issue(lifecycle="approved", approved=True)],
        packages=[partly_approved],
    )
    assert x_next["next_action"]["code"] == "review_exact_approval"
    assert "(1 remaining)" in x_next["next_action"]["text"]


def test_handoff_and_receipt_coverage_is_exact_to_focused_package():
    approved_package = package(dispatch_status="approved")
    inputs = {
        "candidates": [candidate()], "issues": [issue(lifecycle="approved", approved=True)],
        "fact_checks": {"issue-1": fact_check()}, "packages": [approved_package],
        "connectors": [
            {"connector_type": provider, "status": "connected",
             "configuration": {"delivery_mode": "browser_assisted"}}
            for provider in ("beehiiv", "x")
        ],
    }
    unrelated = build(**inputs, handoffs=[
        {"provider": "beehiiv", "resource_id": "different-issue", "status": "completed",
         "receipt_external_id": "bee-old"},
        {"provider": "x", "resource_id": "different-dispatch", "status": "completed",
         "receipt_external_id": "x-old"},
    ])
    assert unrelated["next_action"]["code"] == "start_execution_helper"

    tasks = [
        {"provider": "beehiiv", "resource_id": "issue-1", "status": "completed",
         "receipt_external_id": "bee-1"},
        {"provider": "x", "resource_id": "dispatch-1", "status": "completed",
         "receipt_external_id": "x-1"},
        {"provider": "x", "resource_id": "dispatch-2", "status": "pending"},
    ]
    pending = build(**inputs, handoffs=tasks)
    assert pending["next_action"]["code"] == "complete_provider_handoff"

    ambiguous = [dict(item) for item in tasks]
    ambiguous[0] = {**ambiguous[0], "status": "needs_attention"}
    attention = build(**inputs, handoffs=ambiguous)
    assert attention["stages"][6]["status"] == "done"
    assert attention["next_action"]["code"] == "complete_provider_handoff"
    assert "X" in pending["next_action"]["text"]

    tasks[-1].update(status="completed", receipt_external_id="x-2")
    receipt_complete = build(**inputs, handoffs=tasks)
    assert receipt_complete["next_action"]["code"] == "await_measurement_pull"

    measured = build(
        **inputs, handoffs=tasks,
        performance=[{"campaign_id": "campaign-1", "post_id": "post-1"}],
    )
    assert measured["progress_percent"] == 100
    assert measured["next_action"]["code"] == "loop_complete"


def test_operator_handoff_guidance_and_failed_pull_recovery_are_authoritative():
    approved_package = package(dispatch_status="approved")
    values = {
        "candidates": [candidate()], "issues": [issue(lifecycle="approved", approved=True)],
        "fact_checks": {"issue-1": fact_check()}, "packages": [approved_package],
        "connectors": [
            {"connector_type": provider, "status": "connected",
             "configuration": {"delivery_mode": "browser_assisted"}}
            for provider in ("beehiiv", "x")
        ],
        "handoffs": [
            {"provider": "beehiiv", "resource_id": "issue-1", "status": "pending",
             "operator_next_action": {
                 "code": "bind_destination_account", "text": "Select one Beehiiv account.",
             }},
            {"provider": "x", "resource_id": "dispatch-1", "status": "completed",
             "receipt_external_id": "x-1"},
            {"provider": "x", "resource_id": "dispatch-2", "status": "completed",
             "receipt_external_id": "x-2"},
        ],
    }
    handoff = build(**values)
    assert handoff["next_action"] == {
        "code": "bind_destination_account", "target": "handoffs-panel",
        "text": "Select one Beehiiv account.",
    }

    values["handoffs"][0].update(status="completed", receipt_external_id="bee-1")
    failed = build(**values, assisted_pulls=[{"status": "failed"}])
    assert failed["next_action"]["code"] == "recover_beehiiv_measurement_pull"
    assert failed["assisted_measurement"]["failed"] == 1

    publication_only = build(**values, assisted_pulls=[{
        "status": "completed", "post_measurements_received": 0,
        "campaign_metrics_recorded": 0,
    }])
    assert publication_only["next_action"]["code"] == "await_measurement_pull"
    post_but_unattributed = build(**values, assisted_pulls=[{
        "status": "completed", "post_measurements_received": 1,
        "campaign_metrics_recorded": 0,
    }])
    assert post_but_unattributed["next_action"]["code"] == "await_measurement_pull"
    unrelated_campaign = build(**values, assisted_pulls=[{
        "status": "completed", "post_measurements_received": 1,
        "campaign_metrics_recorded": 1,
        "measured_campaign_ids": ["campaign-unrelated"],
    }])
    assert unrelated_campaign["next_action"]["code"] == "await_measurement_pull"
    assert unrelated_campaign["progress_percent"] < 100
    campaign_evidence = build(**values, assisted_pulls=[{
        "status": "completed", "post_measurements_received": 1,
        "campaign_metrics_recorded": 1,
        "measured_campaign_ids": ["campaign-1"],
    }])
    assert campaign_evidence["next_action"]["code"] == "loop_complete"


def test_stale_package_and_archived_issue_cannot_complete_a_new_loop():
    old_package = package(dispatch_status="approved")
    old_package["status"] = "stale"
    workflow = build(
        candidates=[candidate()], issues=[{**issue(), "lifecycle": "archived"}],
        packages=[old_package], fact_checks={"issue-1": fact_check()},
    )
    assert workflow["next_action"]["code"] == "draft_newsletter"
    assert workflow["focus"]["package_id"] is None


def test_missing_dispatch_fails_closed_as_a_reconciliation_action():
    broken = package(dispatch_status="approved")
    broken["artifacts"][1]["dispatch"] = None
    workflow = build(
        candidates=[candidate()], issues=[issue(lifecycle="approved", approved=True)],
        fact_checks={"issue-1": fact_check()}, packages=[broken],
    )
    assert workflow["next_action"]["code"] == "reconcile_distribution_artifacts"
    assert workflow["stages"][4]["status"] == "current"
    assert workflow["stages"][5]["status"] == "waiting"


def test_newer_actionable_issue_is_not_hijacked_by_older_active_package():
    old = issue(lifecycle="approved", approved=True)
    old["id"] = "old-issue"
    old["updated_at"] = "2026-09-02T09:00:00Z"
    old_package = package(dispatch_status="approved")
    old_package["issue_id"] = "old-issue"
    old_package["updated_at"] = "2026-09-02T09:30:00Z"
    newer = {
        **issue(lifecycle="draft"), "id": "new-issue",
        "updated_at": "2026-09-02T15:00:00Z",
    }
    workflow = build(
        candidates=[candidate()], issues=[old, newer], packages=[old_package],
        fact_checks={"old-issue": {**fact_check(), "issue_id": "old-issue"}},
    )
    assert workflow["focus"]["issue_id"] == "new-issue"
    assert workflow["focus"]["package_id"] is None
    assert workflow["next_action"]["code"] == "fact_check"


def test_server_composer_fails_closed_when_fact_check_is_not_bound_to_review_scope():
    current = issue(lifecycle="fact_checked")
    mismatched = {**fact_check(), "content_fingerprint": "sha256:different"}
    workflow = build(
        candidates=[candidate()], issues=[current],
        fact_checks={"issue-1": mismatched},
    )
    assert workflow["next_action"]["code"] == "fact_check"
    assert workflow["stages"][3]["status"] == "current"

    current["approval_scope"] = {
        **current["approval_scope"], "resource_id": "different-issue",
    }
    workflow = build(
        candidates=[candidate()], issues=[current],
        fact_checks={"issue-1": fact_check()},
    )
    assert workflow["next_action"]["code"] == "fact_check"
