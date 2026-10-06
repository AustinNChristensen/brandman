import re
import shutil
import subprocess
from pathlib import Path


DASHBOARD = Path(__file__).parents[1] / "app" / "static" / "legacy_operator_dashboard.html"


def dashboard_source() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def test_dashboard_script_has_valid_javascript_syntax():
    source = dashboard_source()
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", source, re.DOTALL | re.IGNORECASE)
    assert len(scripts) == 1
    node = shutil.which("node")
    assert node, "Node.js is required to syntax-check the dependency-free dashboard"
    result = subprocess.run(
        [node, "--check", "-"], input=scripts[0], text=True,
        capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_dashboard_distinguishes_discovery_from_governed_primary_evidence():
    source = dashboard_source()
    assert "Discovered via:" in source
    assert "Primary evidence:" in source
    assert "Exact revision evidence:" in source
    assert "Governed evidence:" in source
    assert "legacy pointer — not authoritative" in source
    assert "Legacy source pointer — not authoritative" in source
    assert "unknown — regenerate package" in source


def test_dashboard_covers_every_operator_read_path():
    source = dashboard_source()
    expected_paths = {
        "/api/brands/${BRAND}/context",
        "/api/brands/${BRAND}/mission",
        "/api/brands/${BRAND}/mission/morning-plan",
        "/api/brands/${BRAND}/mission/scorecard",
        "/api/brands/${BRAND}/readiness",
        "/api/brands/${BRAND}/third-party-sources",
        "/api/brands/${BRAND}/connections",
        "/api/brands/${BRAND}/connection-onboarding",
        "/api/brands/${BRAND}/provider-usage",
        "/api/brands/${BRAND}/connectors",
        "/api/brands/${BRAND}/dispatch-items",
        "/api/brands/${BRAND}/execution-tasks",
        "/api/brands/${BRAND}/execution-console",
        "/api/brands/${BRAND}/editorial-candidates",
        "/api/brands/${BRAND}/newsletter-issues",
        "/api/brands/${BRAND}/operator-workflow",
        "/api/brands/${BRAND}/experiments",
        "/api/brands/${BRAND}/distribution-packages",
        "/api/brands/${BRAND}/campaign-graphs",
        "/api/brands/${BRAND}/performance-planning",
        "/api/brands/${BRAND}/tracked-links",
        "/api/brands/${BRAND}/product-feedback",
    }
    assert expected_paths <= {match for match in re.findall(r"/api/[A-Za-z0-9_/${}?=&.-]+", source)}
    for heading in (
        "Mission trajectory", "Connections", "Morning priorities", "Today’s scorecard",
        "Approval queue", "Prioritized candidates", "Newsletter issues", "Tracked links",
        "Product gaps", "Live readiness", "Third-party sources", "Approved handoffs",
        "Campaign graphs",
        "Performance planning",
        "Experiment measurement",
        "Assisted workflow · next safe action",
    ):
        assert heading in source


def test_review_is_confirmation_bound_exact_revision_and_has_no_publish_control():
    source = dashboard_source()
    assert "window.confirm" in source
    assert "exact revision ${revision}" in source
    assert "review_token:button.dataset.reviewToken" in source
    for field in ("account_ref", "action_type", "destination", "intended_schedule",
                  "campaign_id", "asset_membership_id", "material_fingerprint"):
        assert field in source
    assert "/api/dispatch-items/${encodeURIComponent(id)}/${action}" in source
    assert 'data-action="approve"' in source
    assert 'data-action="reject"' in source
    assert 'data-action="publish"' not in source
    assert "/publish" not in source
    assert "actor:'chris'" not in source
    assert 'actor:"chris"' not in source


def test_external_content_escaping_and_resilient_states_are_present():
    source = dashboard_source()
    assert "replace(/[&<>\"']/g" in source
    assert "&quot;" in source and "&#39;" in source
    assert "encodeURIComponent(id)" in source
    assert 'class="loading"' in source
    assert 'class="empty"' in source
    assert 'class="error"' in source
    assert "Promise.all" in source
    assert "try{return{ok:true" in source
    assert "No public publish controls" in source


def test_readiness_and_source_controls_are_safe_and_confirmation_gated():
    source = dashboard_source()
    for contract_field in (
        "code_ready_percent", "live_ready_percent", "checks_ready", "checks_total",
        "worker_schedules", "heartbeat_at", "heartbeat_age_seconds",
        "polling_interval_seconds", "content_policy", "running_sync_jobs",
    ):
        assert contract_field in source
    assert "renderReadiness(data.readiness)" in source
    assert "renderThirdPartySources(data.sources)" in source
    assert "window.confirm(`Confirm you want to ${action} polling for ${name}?`)" in source
    assert "window.prompt(`Why are you ${action}ing this source?`)" in source
    assert "/api/third-party-sources/${encodeURIComponent(id)}/${action}" in source
    assert "body:JSON.stringify({reason:reason.trim()})" in source
    assert 'class="control source-control"' in source
    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "indexedDB" not in source
    assert "document.cookie" not in source
    assert 'data-action="publish"' not in source


def test_connection_wizard_is_least_privilege_one_way_and_never_test_writes():
    source = dashboard_source()
    assert "Beehiiv — read insights" in source
    assert "Beehiiv — create approved drafts" in source
    assert 'beehiiv_read:{provider:"beehiiv",label:"Beehiiv insights",scopes:["posts.read"]' in source
    assert 'beehiiv_write:{provider:"beehiiv",label:"Beehiiv draft creation",scopes:["posts.write"]' in source
    assert 'x_read:{provider:"x",label:"X insights",scopes:["tweet.read","users.read","offline.access"]' in source
    assert 'x_write:{provider:"x",label:"X approved-post delivery",scopes:["tweet.read","tweet.write","users.read","offline.access"]' in source
    assert 'website:{provider:"website",label:"Website analytics",scopes:["analytics.read"]' in source
    assert 'type="password"' in source
    assert 'autocomplete="new-password"' in source
    assert 'secretInput.value=""' in source
    assert 'refreshInput.value=""' in source
    assert 'clientIdInput.value=""' in source
    assert 'expiresInput.value=""' in source
    assert 'credentialPayload.credentials={};secret=""' in source
    assert "credentials.refresh_token=refreshToken" in source
    assert "credentials.client_id=clientId" in source
    assert "credentials.expires_at=expiresAt" in source
    assert "${secret}" not in source
    assert "esc(secret)" not in source
    assert "/api/brands/${BRAND}/connectors" in source
    assert "/api/brands/${BRAND}/connections/${encodeURIComponent(definition.provider)}/${encodeURIComponent(accountId)}" in source
    assert "/api/brands/${BRAND}/connector-health-checks" in source
    assert "role!==\"x_write\"" in source
    assert "Writer was not test-posted." in source
    assert 'readinessChange(before,after,role,assisted)' in source
    assert "No API token or OAuth scopes" in source
    assert "execution-agent heartbeat and provider receipt" in source
    assert "Credential encryption is ready" in source
    assert "BrandOS never bundles or guesses vendor prices" in source
    assert "Add a price from my provider agreement" in source
    assert 'id="rate-price"' in source
    assert 'operation:$("rate-operation").value' in source
    assert "/api/brands/${BRAND}/provider-rate-cards" in source
    assert "Enter your contracted price" in source


def test_execution_handoffs_expose_guided_helper_controls_without_publish_controls():
    source = dashboard_source()
    assert "renderHandoffs(data.handoffs)" in source
    assert "renderApprovalEvidence(data.approvalSnapshots)" in source
    assert "/approval-snapshots" in source
    assert "Approval evidence" in source
    assert "material_fingerprint" in source
    for field in (
        "resource_id", "revision", "claimed_by",
        "receipt_external_id", "receipt_external_url", "receipt_status",
    ):
        assert field in source
    assert "/api/brands/${BRAND}/execution-console" in source
    assert "/api/execution-tasks/${encodeURIComponent(button.dataset.id)}/claim" in source
    assert "/api/execution-tasks/${encodeURIComponent(button.dataset.id)}/begin-external-action" in source
    assert "/api/execution-tasks/${encodeURIComponent(id)}/receipt" in source
    assert "handoffClaimTokens=new Map()" in source
    assert "localStorage" not in source and "sessionStorage" not in source
    assert "None of those controls sends or publishes content" in source
    assert 'data-action="publish"' not in source


def test_handoff_destination_picker_is_operator_confirmed_and_account_scoped():
    source = dashboard_source()
    assert "destination_options" in source
    assert 'class="destination-choice"' in source
    assert "option.display_name" in source
    assert "option.username" in source
    assert "option.account_key" in source
    assert "Bind this exact handoff to ${option.textContent}?" in source
    assert "/api/execution-tasks/${encodeURIComponent(button.dataset.id)}/destination" in source
    assert "connector_account_id:select.value" in source
    assert 'method:"PUT"' in source


def test_beehiiv_aggregate_pull_card_is_read_only_and_helper_scoped():
    source = dashboard_source()
    assert "Measurement and Beehiiv aggregate pulls" in source
    assert "post metadata and aggregate measurements only" in source
    assert "subscriber data, drafts, schedules, sends, and publishing" in source
    assert "/api/beehiiv-assisted-pulls/${encodeURIComponent(button.dataset.id)}/claim" in source
    assert "/api/beehiiv-assisted-pulls/${encodeURIComponent(button.dataset.id)}/heartbeat" in source
    assert "/api/beehiiv-assisted-pulls/${encodeURIComponent(form.dataset.id)}/receipt" in source
    assert "/api/beehiiv-assisted-pulls/${encodeURIComponent(button.dataset.id)}/failure" in source
    assert "measurements_recorded" in source
    assert "post_measurements_received" in source
    assert "campaign_metrics_recorded" in source
    assert "measured_campaign_ids" in source
    assert "Measured campaign IDs:" in source
    for field in (
        "post_id", "post_title", "post_status", "post_time", "post_url",
        "delivered", "unique_opens", "unique_clicks", "unsubscribes",
    ):
        assert f'name="{field}"' in source
    assert "Add another post" in source
    assert "at most 100 post aggregates" in source
    assert "posts,publication_stats:publicationStats" in source
    assert "posts:[]" not in source
    assert "Never enter subscriber names, emails, or records" in source


def test_first_run_workflow_exposes_safe_next_step_and_exact_newsletter_approval():
    source = dashboard_source()
    for label in (
        "1 · Source evidence", "2 · Candidate", "3 · Newsletter draft", "4 · Fact-check",
        "5 · Distribution package", "6 · Exact approvals", "7 · Assisted handoffs",
        "8 · Provider receipts", "9 · Measure + learn",
    ):
        assert label in source
    assert "renderWorkflow(data)" in source
    assert "renderWorkflowContract" in source
    assert "/api/brands/${BRAND}/operator-workflow" in source
    assert "Authoritative workflow guidance is unavailable" in source
    assert "Assisted delivery never grants approval and cannot publish a different revision." in source
    assert "/api/newsletter-issues/${encodeURIComponent(issue.id)}/fact-check?revision=${encodeURIComponent(issue.current_revision)}" in source
    assert "validFactCheck(issue,factChecks[issue?.id])" in source
    assert "/api/newsletter-issues/${encodeURIComponent(id)}/approve" in source
    assert "Confirm newsletter approval for exact revision ${revision}?" in source
    assert "Any edit creates a new revision and invalidates this approval." in source
    assert "review_token:button.dataset.reviewToken" in source
    assert "renderCampaignGraphs(data.campaignGraphs)" in source
    assert "Reusable anchors, touchpoints, supporting assets" in source
    assert "Every asset remains draft-only until separately approved." in source
    assert "/api/campaigns/${encodeURIComponent(item.id)}/measurement" in source
    assert "cross-channel reach and CTR are not summed." in source
    assert 'templates:"/api/campaign-templates"' in source
    assert "Preview campaign graph" in source
    assert "accepted learnings are priors, not formulas" in source
    assert "No campaign or content was created." in source


def test_newsletter_approval_card_shows_exact_material_and_draft_only_intent():
    source = dashboard_source()
    assert "Create an unpublished Beehiiv draft" in source
    assert "No scheduling, publishing, or sending" in source
    assert "Review exact revision material and fact-check evidence" in source
    assert "Newsletter body" in source
    assert "Claims, verdicts, and citations" in source
    assert "Reviewer notes:" in source
    assert "evidence.reviewer" in source
    assert "evidence.created_at" in source
    assert "claim.citations" in source
    assert "source_provenance" in source
    assert 'target="_blank" rel="noopener noreferrer"' in source
    assert "safeHttpUrl" in source
    assert "newsletterReviewReady(issue,evidence)" in source
    assert 'scope.resource_id===issue.id' in source
    assert 'scope.action_type==="create_draft"' in source
    assert 'scope.destination==="beehiiv:draft"' in source
    assert 'String(scope.review_token||"").startsWith("review-v1:")' in source
    assert "claimIds.length===verdictIds.length" in source
    assert "verdicts.every(verdict=>verdict.verified===true)" in source
    assert "citationsGoverned" in source
    assert "sourceIds.has(String(citation.source_id" in source
    assert "evidence.content_fingerprint===scope.material_fingerprint" in source
    assert 'parsed.protocol==="https:"' in source
    assert "non-HTTPS link blocked" in source
    assert '${ready?"":"disabled"}' in source
    assert 'document.querySelectorAll(".newsletter-review")' in source


def test_newsletter_review_requires_acknowledgement_and_supports_safe_rejection():
    source = dashboard_source()
    assert "newsletter-review-ack" in source
    assert "I reviewed the complete newsletter, every claim verdict, every citation" in source
    assert 'class="approve newsletter-review"' in source
    assert "button.disabled=!check.checked" in source
    assert "Reject revision and return for changes" in source
    assert "/api/newsletter-issues/${encodeURIComponent(id)}/reject" in source
    assert "A rejection reason is required; nothing changed." in source
    assert "invalidate its distribution package" in source


def test_handoff_view_exposes_exact_material_and_human_x_confirmation_not_publish():
    source = dashboard_source()
    assert "Exact approved X post" in source
    assert "Exact approved Beehiiv draft" in source
    assert "item.execution_payload" in source
    assert "item.material_fingerprint" in source
    assert "Confirm this exact X post for 5 minutes" in source
    assert "Type CONFIRM PUBLIC X POST" in source
    assert "/api/execution-tasks/${encodeURIComponent(button.dataset.id)}/confirm-public-action" in source
    assert "expected_material_fingerprint:button.dataset.fingerprint" in source
    assert "validity_seconds:300" in source
    assert "Confirmation does not post" in source


def test_reconnect_and_disconnect_require_confirmation():
    source = dashboard_source()
    assert "Replace the stored credential for ${displayName}?" in source
    assert "Disconnect ${name}?" in source
    assert "/api/brands/${BRAND}/connections/${encodeURIComponent(provider)}/${encodeURIComponent(id)}/disconnect" in source
    assert 'method:"POST"' in source


def test_dashboard_is_dependency_free_and_responsive():
    source = dashboard_source()
    assert "@media(max-width:900px)" in source
    assert "@media(max-width:620px)" in source
    assert "<script src=" not in source
    assert "<link rel=" not in source
    assert "https://" not in source


def test_dashboard_explains_bounded_performance_prior_and_safety():
    source = dashboard_source()
    assert "renderPerformancePlan(data.performancePlan)" in source
    assert "time-decayed and confidence-aware prior—not an instruction" in source
    assert "Same-channel denominators only" in source
    assert "Protected invariants and deterministic exploration never change" in source
    assert "Performance cannot change deterministic exploration, approval, or publishing safety" in source
    assert "score_adjustment_points" in source
    assert "matching_campaigns_last_30_days" in source
