from datetime import UTC, datetime, timedelta
import json

import pytest

from app.approval_snapshots import ApprovalSnapshotStore
from app.dispatch import GovernedDispatcher, Lifecycle, SQLiteDispatchStore
from app.editorial import EditorialStore, IssueLifecycle
from app.execution_handoff import ExecutionHandoffError, ExecutionHandoffStore
from app.execution_agents import ExecutionAgentRegistry
from app.readiness import LiveReadinessService


def issue_content():
    return {
        "editorial_thesis": "The approved offer is useful now.",
        "target_reader": "Readers", "intended_outcome": "Review the offer",
        "working_title": "Offer", "final_title": "The offer worth checking",
        "subject": "A current offer", "preview_text": "The facts and source.",
        "sections": [{"heading": "Details", "body": "Source-grounded details."}],
        "cta": {"label": "Read source", "url": "https://issuer.test/offer"},
        "seo": {"title": "Offer guide", "description": "Current offer details"},
        "claims": [],
        "source_provenance": [{"source_id": "source-1", "url": "https://issuer.test/offer"}],
    }


@pytest.fixture
def setup(tmp_path):
    now = [datetime(2026, 9, 2, 12, tzinfo=UTC)]
    database = tmp_path / "handoff.db"
    editorial = EditorialStore(database, clock=lambda: now[0].isoformat())
    dispatcher = GovernedDispatcher(SQLiteDispatchStore(database), clock=lambda: now[0])
    with dispatcher.store._connection() as connection:
        connection.executescript(
            """CREATE TABLE IF NOT EXISTS connector_accounts (
                 id TEXT PRIMARY KEY,brand_id TEXT NOT NULL,connector_type TEXT NOT NULL,
                 account_key TEXT NOT NULL,display_name TEXT NOT NULL,status TEXT NOT NULL,
                 scopes TEXT NOT NULL DEFAULT '[]',capabilities TEXT NOT NULL DEFAULT '[]',
                 health_checked_at TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
               );
               CREATE TABLE IF NOT EXISTS connector_account_configurations (
                 connector_account_id TEXT PRIMARY KEY,configuration TEXT NOT NULL,updated_at TEXT NOT NULL
               );"""
        )
        for account_id, provider, role, username in (
            ("bee-assisted", "beehiiv", "beehiiv_write", None),
            ("x-assisted", "x", "x_write", "demobrand"),
        ):
            connection.execute(
                """INSERT INTO connector_accounts
                   (id,brand_id,connector_type,account_key,display_name,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,'connected','now','now')""",
                (account_id, "brand-1", provider, account_id, account_id),
            )
            connection.execute(
                """INSERT INTO connector_account_configurations
                   (connector_account_id,configuration,updated_at) VALUES (?,?,'now')""",
                (account_id, json.dumps({
                    "delivery_mode": "browser_assisted", "connection_role": role,
                    **({"username": username} if username else {}),
                })),
            )
    handoffs = ExecutionHandoffStore(database, editorial, dispatcher, clock=lambda: now[0])
    agents = ExecutionAgentRegistry(database, clock=lambda: now[0])
    for agent_id, channel in (
        ("browser-agent", "browser"), ("recovery-agent", "browser"), ("mcp-agent", "mcp"),
    ):
        agents.configure("brand-1", agent_id, channel)
        agents.heartbeat("brand-1", agent_id)
    return now, editorial, dispatcher, handoffs


def approve_newsletter(editorial):
    issue = editorial.create_issue("brand-1", issue_content(), created_by="writer")
    editorial.transition(issue["id"], IssueLifecycle.OUTLINE)
    editorial.transition(issue["id"], IssueLifecycle.DRAFT)
    editorial.record_fact_check(
        issue["id"], expected_revision=1, reviewer="Chris", verdicts=[],
    )
    return editorial.approve_issue(issue["id"], approver="Chris", expected_revision=1)


def approve_x(dispatcher):
    item = dispatcher.create(
        "x", {"body": "Approved public post"},
        brand_id="brand-1",
    )
    dispatcher.submit_for_approval(item.id, actor="writer")
    return dispatcher.approve(item.id, revision=1, approver="Chris")


def confirm_x(handoffs, task):
    return handoffs.confirm_public_action(
        task["id"], actor="Chris", expected_revision=task["revision"],
        expected_material_fingerprint=task["material_fingerprint"],
        confirmation_phrase="CONFIRM PUBLIC X POST",
    )


def begin(handoffs, task, claim, actor="browser-agent"):
    return handoffs.begin_external_action(
        task["id"], actor=actor, claim_token=claim["claim_token"],
    )


def test_only_exact_approved_revisions_become_safe_unclaimed_tasks(setup):
    _, editorial, dispatcher, handoffs = setup
    unapproved = editorial.create_issue("brand-1", issue_content(), created_by="writer")
    approved_issue = approve_newsletter(editorial)
    approved_post = approve_x(dispatcher)

    tasks = handoffs.ensure_for_brand("brand-1")

    assert {task["resource_id"] for task in tasks} == {approved_issue["id"], approved_post.id}
    assert unapproved["id"] not in {task["resource_id"] for task in tasks}
    x_task = next(task for task in tasks if task["provider"] == "x")
    assert x_task["execution_payload"] == {"body": "Approved public post"}
    assert "claim_token_hash" not in repr(tasks)
    assert dispatcher.store.get(approved_post.id).status is Lifecycle.APPROVED


def test_campaign_context_flows_from_typed_asset_membership_to_receipt(setup):
    _, editorial, _, handoffs = setup
    issue = approve_newsletter(editorial)
    with handoffs._connect() as connection:
        connection.execute(
            """CREATE TABLE campaign_asset_memberships (
               id TEXT PRIMARY KEY,package_id TEXT NOT NULL,campaign_id TEXT NOT NULL,
               asset_type TEXT NOT NULL,asset_id TEXT NOT NULL,channel TEXT NOT NULL,
               role TEXT NOT NULL,weight REAL NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
               active INTEGER NOT NULL DEFAULT 1,attribution_primary INTEGER NOT NULL DEFAULT 0,
               UNIQUE(asset_type,asset_id))"""
        )
        connection.execute(
            """INSERT INTO campaign_asset_memberships
               (id,package_id,campaign_id,asset_type,asset_id,channel,role,weight,created_at,updated_at,
                active,attribution_primary) VALUES
               ('membership-1','package-1','campaign-1','newsletter_issue',?,'newsletter',
                'anchor',1.0,'2026-09-02T12:00:00Z','2026-09-02T12:00:00Z',1,1)""",
            (issue["id"],),
        )
    task = handoffs.ensure_for_brand("brand-1")[0]
    evidence = ApprovalSnapshotStore(handoffs.database).list("brand-1", active_only=True)[0]
    assert evidence["campaign_id"] == "campaign-1"
    assert evidence["asset_membership_id"] == "membership-1"
    assert task["campaign_id"] == "campaign-1"
    assert task["asset_membership_id"] == "membership-1"
    claim = handoffs.claim(task["id"], actor="browser-agent")
    begin(handoffs, task, claim)
    receipt = handoffs.submit_receipt(
        task["id"], claim_token=claim["claim_token"], external_id="post_bee-1",
        external_url="https://app.beehiiv.com/posts/post_bee-1", status="draft",
    )
    assert receipt["campaign_id"] == "campaign-1"
    assert receipt["asset_membership_id"] == "membership-1"


def test_beehiiv_private_draft_manifest_and_fingerprinted_uuid_receipt(setup, tmp_path):
    _, editorial, _, handoffs = setup
    issue = approve_newsletter(editorial)
    task = next(
        item for item in handoffs.ensure_for_brand("brand-1")
        if item["provider"] == "beehiiv" and item["resource_id"] == issue["id"]
    )
    asset = tmp_path / "approved-thumbnail.png"
    asset.write_bytes(b"exact-approved-image")
    draft_id = "32dcf1b3-8180-49c4-b50c-39da517010f6"
    manifest = handoffs.beehiiv_private_draft_manifest(
        task["id"], asset_path=asset, existing_draft_id=draft_id,
    )
    assert manifest["execution_task_id"] == task["id"]
    assert manifest["reconciliation"]["existing_draft_id"] == draft_id
    assert manifest["content_fingerprint"] == task["material_fingerprint"]
    assert manifest["forbidden_actions"] == ["schedule", "send", "publish"]

    claim = handoffs.claim(task["id"], actor="browser-agent")
    begin(handoffs, task, claim)
    receipt = handoffs.submit_receipt(
        task["id"], claim_token=claim["claim_token"], external_id=draft_id,
        external_url=f"https://app.beehiiv.com/posts/{draft_id}", status="draft",
        content_fingerprint=manifest["content_fingerprint"],
        asset_fingerprint=manifest["thumbnail"]["asset_fingerprint"],
    )
    assert receipt["receipt_content_fingerprint"] == manifest["content_fingerprint"]
    assert receipt["receipt_asset_fingerprint"] == manifest["thumbnail"]["asset_fingerprint"]


def test_fingerprinted_receipt_rejects_material_drift(setup, tmp_path):
    _, editorial, _, handoffs = setup
    approve_newsletter(editorial)
    task = next(item for item in handoffs.ensure_for_brand("brand-1") if item["provider"] == "beehiiv")
    claim = handoffs.claim(task["id"], actor="browser-agent")
    begin(handoffs, task, claim)
    with pytest.raises(ExecutionHandoffError, match="exact approved material"):
        handoffs.submit_receipt(
            task["id"], claim_token=claim["claim_token"],
            external_id="32dcf1b3-8180-49c4-b50c-39da517010f6",
            external_url=("https://app.beehiiv.com/posts/"
                          "32dcf1b3-8180-49c4-b50c-39da517010f6"),
            status="draft", content_fingerprint="sha256:" + "0" * 64,
            asset_fingerprint="sha256:" + "1" * 64,
        )


def test_claim_is_single_use_expires_and_never_confers_approval(setup):
    now, editorial, dispatcher, handoffs = setup
    post = approve_x(dispatcher)
    task = next(task for task in handoffs.ensure_for_brand("brand-1") if task["provider"] == "x")
    claimed = handoffs.claim(task["id"], actor="browser-agent", lease_seconds=60)
    assert claimed["claim_token"]
    assert "claim_token_hash" not in claimed
    assert dispatcher.store.get(post.id).status is Lifecycle.APPROVED
    with pytest.raises(ExecutionHandoffError, match="not available"):
        handoffs.claim(task["id"], actor="other-agent", lease_seconds=60)

    now[0] += timedelta(seconds=61)
    recovered = handoffs.claim(task["id"], actor="recovery-agent", lease_seconds=60)
    assert recovered["claim_token"] != claimed["claim_token"]
    with pytest.raises(ExecutionHandoffError, match="valid active claim"):
        handoffs.submit_receipt(
            task["id"], claim_token=claimed["claim_token"], external_id="1",
            external_url="https://x.com/demobrand/status/1", status="posted",
        )
    assert [event["action"] for event in handoffs.audit(task["id"])] == [
        "created", "claimed", "claim_expired", "claimed",
    ]
    assert "claim_token" not in repr(handoffs.audit(task["id"]))


def test_stale_or_cross_brand_execution_agent_cannot_claim(setup):
    now, _, dispatcher, handoffs = setup
    approve_x(dispatcher)
    task = next(task for task in handoffs.ensure_for_brand("brand-1") if task["provider"] == "x")
    registry = ExecutionAgentRegistry(handoffs.database, clock=lambda: now[0])
    registry.configure("other-brand", "foreign-agent", "browser")
    registry.heartbeat("other-brand", "foreign-agent")
    with pytest.raises(ExecutionHandoffError, match="enabled brand execution agent"):
        handoffs.claim(task["id"], actor="foreign-agent")
    now[0] += timedelta(seconds=901)
    with pytest.raises(ExecutionHandoffError, match="fresh heartbeat"):
        handoffs.claim(task["id"], actor="browser-agent")


def test_global_and_provider_controls_block_new_claims_with_immutable_audit(setup):
    _, editorial, dispatcher, handoffs = setup
    approve_newsletter(editorial)
    approve_x(dispatcher)
    tasks = handoffs.ensure_for_brand("brand-1")
    bee = next(task for task in tasks if task["provider"] == "beehiiv")
    x_task = next(task for task in tasks if task["provider"] == "x")

    disabled_x = handoffs.set_control("brand-1", "x", enabled=False, actor="preview-operator")
    assert disabled_x["effective_enabled"] is False
    with pytest.raises(ExecutionHandoffError, match="x assisted execution is disabled"):
        handoffs.claim(x_task["id"], actor="browser-agent")
    # The provider-specific switch does not interrupt the other read/write lane.
    assert handoffs.claim(bee["id"], actor="browser-agent")["status"] == "claimed"

    handoffs.set_control("brand-1", "x", enabled=True, actor="preview-operator")
    handoffs.set_control("brand-1", "all", enabled=False, actor="preview-operator")
    with pytest.raises(ExecutionHandoffError, match="x assisted execution is disabled"):
        handoffs.claim(x_task["id"], actor="browser-agent")
    handoffs.set_control("brand-1", "all", enabled=True, actor="preview-operator")
    assert handoffs.claim(x_task["id"], actor="browser-agent")["status"] == "claimed"
    assert [(row["provider"], row["enabled"]) for row in handoffs.control_audit("brand-1")] == [
        ("x", False), ("x", True), ("all", False), ("all", True),
    ]


def test_revision_change_after_begin_preserves_exact_receipt_without_overwriting_new_revision(setup):
    _, _, dispatcher, handoffs = setup
    post = approve_x(dispatcher)
    task = next(task for task in handoffs.ensure_for_brand("brand-1") if task["provider"] == "x")
    claimed = handoffs.claim(task["id"], actor="browser-agent")
    confirm_x(handoffs, task)
    begin(handoffs, task, claimed)
    dispatcher.edit(post.id, {"body": "Changed after approval"}, actor="writer")

    ambiguous = handoffs.get(task["id"])
    assert ambiguous["status"] == "needs_attention"
    assert ambiguous["external_action_snapshot_fingerprint"].startswith("sha256:")
    completed = handoffs.submit_receipt(
        task["id"], claim_token=claimed["claim_token"], external_id="1",
        external_url="https://x.com/demobrand/status/1", status="posted",
    )
    assert completed["status"] == "completed"
    assert completed["receipt_reconciliation_state"] == "immutable_begin_snapshot"
    current = dispatcher.store.get(post.id)
    assert current.revision == 2
    assert current.status is Lifecycle.DRAFT
    assert current.external_id is None


def test_immutable_evidence_invalidation_blocks_claim_even_if_canonical_row_is_unchanged(setup):
    _, _, dispatcher, handoffs = setup
    post = approve_x(dispatcher)
    task = next(task for task in handoffs.ensure_for_brand("brand-1") if task["provider"] == "x")
    snapshots = ApprovalSnapshotStore(handoffs.database)
    assert snapshots.invalidate_resource(
        post.id, actor="governance-audit", reason="material approval withdrawn",
    ) == 1

    with pytest.raises(ExecutionHandoffError, match="exact approved revision"):
        handoffs.claim(task["id"], actor="browser-agent")
    assert handoffs.get(task["id"])["status"] == "stale"


def test_evidence_drift_after_begin_preserves_original_capability_for_truthful_receipt(setup):
    _, _, dispatcher, handoffs = setup
    post = approve_x(dispatcher)
    task = next(task for task in handoffs.ensure_for_brand("brand-1") if task["provider"] == "x")
    claim = handoffs.claim(task["id"], actor="browser-agent")
    confirm_x(handoffs, task)
    begin(handoffs, task, claim)
    ApprovalSnapshotStore(handoffs.database).invalidate_resource(
        post.id, actor="governance-audit", reason="approval withdrawn after claim",
    )

    assert handoffs.get(task["id"])["status"] == "needs_attention"
    completed = handoffs.submit_receipt(
        task["id"], claim_token=claim["claim_token"], external_id="1",
        external_url="https://x.com/demobrand/status/1", status="posted",
    )
    assert completed["receipt_reconciliation_state"] == "immutable_begin_snapshot"
    assert dispatcher.store.get(post.id).external_id is None


def test_handoff_rejects_secret_shaped_audit_and_receipt_fields(setup):
    _, _, dispatcher, handoffs = setup
    approve_x(dispatcher)
    task = next(task for task in handoffs.ensure_for_brand("brand-1") if task["provider"] == "x")
    with pytest.raises(ExecutionHandoffError, match="safe operator identifier"):
        handoffs.claim(task["id"], actor="operator secret=value")
    with pytest.raises(ExecutionHandoffError, match="enabled brand execution agent"):
        handoffs.claim(task["id"], actor="unregistered-agent")
    claimed = handoffs.claim(task["id"], actor="browser-agent")
    confirm_x(handoffs, task)
    begin(handoffs, task, claimed)
    with pytest.raises(ExecutionHandoffError, match="safe provider identifier"):
        handoffs.submit_receipt(
            task["id"], claim_token=claimed["claim_token"], external_id="token=secret value",
            external_url=None, status="posted",
        )
    with pytest.raises(ExecutionHandoffError, match="query parameters"):
        handoffs.submit_receipt(
            task["id"], claim_token=claimed["claim_token"], external_id="1",
            external_url="https://x.com/status/1?access_token=secret", status="posted",
        )
    assert "secret" not in repr(handoffs.audit(task["id"]))


@pytest.mark.parametrize(
    "external_id,url,error",
    [
        ("1", None, "external_url is required"),
        ("1", "http://x.com/demobrand/status/1", "canonical HTTPS"),
        ("1", "https://attacker.test/demo/status/1", "canonical x.com"),
        ("2", "https://x.com/demobrand/status/1", "matching external_id"),
        ("1", "https://x.com/demobrand/status/1?token=secret", "query parameters"),
    ],
)
def test_x_receipt_requires_provider_specific_url_identity(setup, external_id, url, error):
    _, _, dispatcher, handoffs = setup
    approve_x(dispatcher)
    task = next(task for task in handoffs.ensure_for_brand("brand-1") if task["provider"] == "x")
    claim = handoffs.claim(task["id"], actor="browser-agent")
    confirm_x(handoffs, task)
    begin(handoffs, task, claim)
    with pytest.raises(ExecutionHandoffError, match=error):
        handoffs.submit_receipt(
            task["id"], claim_token=claim["claim_token"], external_id=external_id,
            external_url=url, status="posted",
        )
    assert handoffs.get(task["id"])["status"] == "claimed"


def test_beehiiv_receipt_url_must_match_provider_and_external_id(setup):
    _, editorial, _, handoffs = setup
    approve_newsletter(editorial)
    task = next(task for task in handoffs.ensure_for_brand("brand-1") if task["provider"] == "beehiiv")
    claim = handoffs.claim(task["id"], actor="browser-agent")
    begin(handoffs, task, claim)
    with pytest.raises(ExecutionHandoffError, match="Beehiiv receipt"):
        handoffs.submit_receipt(
            task["id"], claim_token=claim["claim_token"], external_id="post_bee-1",
            external_url="https://app.beehiiv.com/posts/post_bee-OTHER", status="draft",
        )
    with pytest.raises(ExecutionHandoffError, match="Beehiiv receipt"):
        handoffs.submit_receipt(
            task["id"], claim_token=claim["claim_token"],
            external_id="post_invalid_underscore",
            external_url="https://app.beehiiv.com/posts/post_invalid_underscore",
            status="draft",
        )


def test_provider_receipts_are_restricted_idempotent_and_canonical(setup):
    _, editorial, dispatcher, handoffs = setup
    issue = approve_newsletter(editorial)
    post = approve_x(dispatcher)
    tasks = handoffs.ensure_for_brand("brand-1")
    bee = next(task for task in tasks if task["provider"] == "beehiiv")
    x_task = next(task for task in tasks if task["provider"] == "x")

    bee_claim = handoffs.claim(bee["id"], actor="browser-agent")
    begin(handoffs, bee, bee_claim)
    with pytest.raises(ExecutionHandoffError, match="only a draft"):
        handoffs.submit_receipt(
            bee["id"], claim_token=bee_claim["claim_token"], external_id="post_bee-1",
            external_url="https://app.beehiiv.com/posts/post_bee-1", status="posted",
        )
    receipt = handoffs.submit_receipt(
        bee["id"], claim_token=bee_claim["claim_token"], external_id="post_bee-1",
        external_url="https://app.beehiiv.com/posts/post_bee-1", status="draft",
    )
    assert receipt["status"] == "completed"
    assert editorial.get_issue(issue["id"])["lifecycle"] == "exported"
    assert handoffs.submit_receipt(
        bee["id"], claim_token="retry-does-not-need-capability-token",
        external_id="post_bee-1", external_url="https://app.beehiiv.com/posts/post_bee-1", status="draft",
    ) == receipt
    with pytest.raises(ExecutionHandoffError, match="different receipt"):
        handoffs.submit_receipt(
            bee["id"], claim_token="different", external_id="post_bee-2",
            external_url=None, status="draft",
        )

    x_claim = handoffs.claim(x_task["id"], actor="mcp-agent")
    confirm_x(handoffs, x_task)
    begin(handoffs, x_task, x_claim, actor="mcp-agent")
    with pytest.raises(ExecutionHandoffError, match="posted receipt"):
        handoffs.submit_receipt(
            x_task["id"], claim_token=x_claim["claim_token"], external_id="1",
            external_url="https://x.com/demobrand/status/1", status="draft",
        )
    handoffs.submit_receipt(
        x_task["id"], claim_token=x_claim["claim_token"], external_id="1",
        external_url="https://x.com/demobrand/status/1", status="posted",
    )
    canonical = dispatcher.store.get(post.id)
    assert canonical.status is Lifecycle.PUBLISHED
    assert canonical.external_id == "1"

    registry = ExecutionAgentRegistry(handoffs.database, clock=lambda: NOW_FOR_READINESS)
    registry.configure("brand-1", "demobrand-agent", "browser")
    registry.heartbeat("brand-1", "demobrand-agent")
    readiness = LiveReadinessService(handoffs.database, clock=lambda: NOW_FOR_READINESS)
    with readiness._connect() as connection:
        state = readiness._execution_state(connection, "brand-1")
    check = readiness._assisted_execution_check(state)
    assert check["status"] == "ready"
    assert check["api_connected"] is False
    assert check["provider_receipts_proven"] == ["beehiiv", "x"]

    # A real legacy post remains canonical, but it is not sufficient evidence
    # for the stricter current assisted-delivery contract.
    with handoffs._connect() as connection:
        connection.execute(
            """UPDATE execution_tasks SET public_action_confirmed_by=NULL,
               public_action_confirmed_at=NULL,
               public_action_confirmation_fingerprint=NULL
               WHERE id=?""", (x_task["id"],),
        )
    with readiness._connect() as connection:
        legacy_state = readiness._execution_state(connection, "brand-1")
    legacy_check = readiness._assisted_execution_check(legacy_state)
    assert legacy_check["status"] == "unhealthy"
    assert legacy_check["provider_receipts_proven"] == ["beehiiv"]
    assert legacy_check["missing_provider_receipts"] == ["x"]


def test_public_x_receipt_requires_separate_exact_action_time_confirmation(setup):
    now, _, dispatcher, handoffs = setup
    approve_x(dispatcher)
    task = next(task for task in handoffs.ensure_for_brand("brand-1") if task["provider"] == "x")
    claim = handoffs.claim(task["id"], actor="browser-agent")

    with pytest.raises(ExecutionHandoffError, match="action-time confirmation"):
        begin(handoffs, task, claim)
    with pytest.raises(ExecutionHandoffError, match="exact claimed revision"):
        handoffs.confirm_public_action(
            task["id"], actor="Chris", expected_revision=2,
            expected_material_fingerprint=task["material_fingerprint"],
            confirmation_phrase="CONFIRM PUBLIC X POST",
        )
    with pytest.raises(ExecutionHandoffError, match="confirmation_phrase"):
        handoffs.confirm_public_action(
            task["id"], actor="Chris", expected_revision=1,
            expected_material_fingerprint=task["material_fingerprint"],
            confirmation_phrase="yes",
        )

    confirmed = confirm_x(handoffs, task)
    assert confirmed["public_action_confirmed_by"] == "Chris"
    assert [event["action"] for event in handoffs.audit(task["id"])][-1] == "public_action_confirmed"

    begin(handoffs, task, claim)

    now[0] += timedelta(seconds=301)
    # Reconciliation is allowed after confirmation expiry because authorization
    # was current at the durable pre-click boundary.
    assert handoffs.submit_receipt(
        task["id"], claim_token=claim["claim_token"], external_id="1",
        external_url="https://x.com/demobrand/status/1", status="posted",
    )["status"] == "completed"


def test_beehiiv_draft_never_accepts_public_action_confirmation(setup):
    _, editorial, _, handoffs = setup
    approve_newsletter(editorial)
    task = next(task for task in handoffs.ensure_for_brand("brand-1") if task["provider"] == "beehiiv")
    handoffs.claim(task["id"], actor="browser-agent")
    with pytest.raises(ExecutionHandoffError, match="only to public X"):
        confirm_x(handoffs, task)


def test_receipt_requires_durable_pre_provider_action_boundary(setup):
    _, editorial, _, handoffs = setup
    approve_newsletter(editorial)
    task = next(task for task in handoffs.ensure_for_brand("brand-1") if task["provider"] == "beehiiv")
    claim = handoffs.claim(task["id"], actor="browser-agent", lease_seconds=60)

    with pytest.raises(ExecutionHandoffError, match="mark the exact external action started"):
        handoffs.submit_receipt(
            task["id"], claim_token=claim["claim_token"], external_id="post_bee-1",
            external_url="https://app.beehiiv.com/posts/post_bee-1", status="draft",
        )


def test_expired_started_action_is_never_reclaimed_and_accepts_exact_late_receipt(setup):
    now, _, dispatcher, handoffs = setup
    approve_x(dispatcher)
    task = next(task for task in handoffs.ensure_for_brand("brand-1") if task["provider"] == "x")
    claim = handoffs.claim(task["id"], actor="browser-agent", lease_seconds=60)
    confirm_x(handoffs, task)
    started = begin(handoffs, task, claim)
    assert started["external_action_started_fingerprint"] == task["material_fingerprint"]

    now[0] += timedelta(seconds=61)
    assert handoffs.get(task["id"])["status"] == "needs_attention"
    with pytest.raises(ExecutionHandoffError, match="not available"):
        handoffs.claim(task["id"], actor="recovery-agent", lease_seconds=60)

    completed = handoffs.submit_receipt(
        task["id"], claim_token=claim["claim_token"], external_id="1",
        external_url="https://x.com/demobrand/status/1", status="posted",
    )
    assert completed["status"] == "completed"
    assert [event["action"] for event in handoffs.audit(task["id"])] == [
        "created", "claimed", "public_action_confirmed", "external_action_started",
        "external_outcome_ambiguous", "receipt_recorded",
    ]


def test_begun_x_receipt_is_bound_to_snapshotted_assisted_account(setup):
    _, _, dispatcher, handoffs = setup
    approve_x(dispatcher)
    task = next(task for task in handoffs.ensure_for_brand("brand-1") if task["provider"] == "x")
    claim = handoffs.claim(task["id"], actor="browser-agent")
    confirm_x(handoffs, task)
    begin(handoffs, task, claim)

    with pytest.raises(ExecutionHandoffError, match="account does not match"):
        handoffs.submit_receipt(
            task["id"], claim_token=claim["claim_token"], external_id="1",
            external_url="https://x.com/notdemobrand/status/1", status="posted",
        )
    assert handoffs.submit_receipt(
        task["id"], claim_token=claim["claim_token"], external_id="1",
        external_url="https://x.com/demobrand/status/1", status="posted",
    )["receipt_reconciliation_state"] == "canonical_projected"


def test_multiple_assisted_x_accounts_fail_closed_until_exact_binding_and_late_receipt(setup):
    now, _, dispatcher, handoffs = setup
    with handoffs._connect() as connection:
        connection.execute(
            """INSERT INTO connector_accounts
               (id,brand_id,connector_type,account_key,display_name,status,created_at,updated_at)
               VALUES ('x-other','brand-1','x','otherbrand','Other X','connected','now','now')"""
        )
        connection.execute(
            """INSERT INTO connector_account_configurations
               (connector_account_id,configuration,updated_at) VALUES ('x-other',?,'now')""",
            (json.dumps({
                "delivery_mode": "browser_assisted", "connection_role": "x_write",
                "username": "otherbrand",
            }),),
        )
    approve_x(dispatcher)
    task = next(task for task in handoffs.ensure_for_brand("brand-1") if task["provider"] == "x")
    assert task["connector_account_id"] is None
    operator_task = next(
        item for item in handoffs.operator_view("brand-1") if item["id"] == task["id"]
    )
    assert operator_task["destination_binding_required"] is True
    assert operator_task["operator_next_action"]["code"] == "bind_destination_account"
    assert [account["id"] for account in operator_task["destination_options"]] == [
        "x-assisted", "x-other",
    ]
    assert operator_task["destination_options"][0]["username"] == "demobrand"
    with pytest.raises(ExecutionHandoffError, match="multiple eligible destination accounts"):
        handoffs.claim(task["id"], actor="browser-agent")

    bound = handoffs.bind_destination_account(
        task["id"], connector_account_id="x-assisted", actor="Chris",
    )
    assert bound["connector_account_id"] == "x-assisted"
    bound_view = next(item for item in handoffs.operator_view("brand-1") if item["id"] == task["id"])
    assert bound_view["operator_next_action"]["code"] == "claim_in_execution_helper"
    claim = handoffs.claim(task["id"], actor="browser-agent", lease_seconds=60)
    claimed_view = next(item for item in handoffs.operator_view("brand-1") if item["id"] == task["id"])
    assert claimed_view["operator_next_action"]["code"] == "confirm_exact_x_action"
    confirm_x(handoffs, task)
    confirmed_view = next(item for item in handoffs.operator_view("brand-1") if item["id"] == task["id"])
    assert confirmed_view["operator_next_action"]["code"] == "begin_external_action"
    begun = begin(handoffs, task, claim)
    assert begun["provider_account_binding"] == [{
        "id": "x-assisted", "account_key": "x-assisted",
        "display_name": "x-assisted", "username": "demobrand",
    }]
    begun_view = next(item for item in handoffs.operator_view("brand-1") if item["id"] == task["id"])
    assert begun_view["operator_next_action"]["code"] == "record_provider_receipt"

    now[0] += timedelta(seconds=61)
    assert handoffs.get(task["id"])["status"] == "needs_attention"
    attention_view = next(item for item in handoffs.operator_view("brand-1") if item["id"] == task["id"])
    assert attention_view["operator_next_action"]["code"] == "record_provider_receipt"
    assert attention_view["operator_next_action"]["text"].startswith("Do not repeat")
    with pytest.raises(ExecutionHandoffError, match="account does not match"):
        handoffs.submit_receipt(
            task["id"], claim_token=claim["claim_token"], external_id="1",
            external_url="https://x.com/otherbrand/status/1", status="posted",
        )
    completed = handoffs.submit_receipt(
        task["id"], claim_token=claim["claim_token"], external_id="1",
        external_url="https://x.com/demobrand/status/1", status="posted",
    )
    assert completed["status"] == "completed"
    assert completed["connector_account_id"] == "x-assisted"


def test_one_provider_receipt_cannot_complete_two_different_x_tasks(setup):
    _, _, dispatcher, handoffs = setup
    first = approve_x(dispatcher)
    second = approve_x(dispatcher)
    tasks = {
        task["resource_id"]: task for task in handoffs.ensure_for_brand("brand-1")
        if task["provider"] == "x"
    }
    claims = {}
    for item in (first, second):
        task = tasks[item.id]
        claims[item.id] = handoffs.claim(task["id"], actor="browser-agent")
        confirm_x(handoffs, task)
        begin(handoffs, task, claims[item.id])

    external_id = "1900000000000000099"
    external_url = f"https://x.com/demobrand/status/{external_id}"
    assert handoffs.submit_receipt(
        tasks[first.id]["id"], claim_token=claims[first.id]["claim_token"],
        external_id=external_id, external_url=external_url, status="posted",
    )["status"] == "completed"
    with pytest.raises(ExecutionHandoffError, match="different execution task"):
        handoffs.submit_receipt(
            tasks[second.id]["id"], claim_token=claims[second.id]["claim_token"],
            external_id=external_id, external_url=external_url, status="posted",
        )
    assert dispatcher.store.get(second.id).status is Lifecycle.APPROVED
    assert dispatcher.store.get(second.id).external_id is None


NOW_FOR_READINESS = datetime(2026, 9, 2, 12, tzinfo=UTC)
