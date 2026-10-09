"""One coherent, read-only next-action view of the DemoBrand content loop.

The dashboard and least-authority client receive many independent collections.
This composer deliberately anchors them to one newsletter/package lineage so
unrelated historical work cannot make a partly finished loop look complete.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any


TERMINAL_PACKAGE_STATES = {"stale", "cancelled", "archived", "abandoned"}
TERMINAL_ISSUE_STATES = {"abandoned", "archived"}
APPROVED_DISPATCH_STATES = {"approved", "queued", "published", "measured"}
ACTIVE_HANDOFF_STATES = {"pending", "claimed", "needs_attention", "completed"}


def _records(value: Sequence[Mapping[str, Any]] | None) -> list[Mapping[str, Any]]:
    return [item for item in (value or []) if isinstance(item, Mapping)]


def _latest(items: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    def ordering(item: Mapping[str, Any]) -> tuple[float, str]:
        raw = str(item.get("updated_at") or item.get("created_at") or "")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            timestamp = parsed.timestamp()
        except ValueError:
            timestamp = float("-inf")
        return timestamp, str(item.get("id") or "")

    return max(
        items,
        key=ordering,
        default=None,
    )


def _valid_fact_check(issue: Mapping[str, Any] | None, evidence: Any) -> bool:
    if not issue or not isinstance(evidence, Mapping):
        return False
    scope = issue.get("approval_scope")
    content = issue.get("content")
    if not isinstance(scope, Mapping) or not isinstance(content, Mapping):
        return False
    try:
        exact_revision = int(evidence["revision"]) == int(issue["current_revision"])
        exact_scope_revision = int(scope["revision"]) == int(issue["current_revision"])
    except (KeyError, TypeError, ValueError):
        return False
    claims = _records(content.get("claims"))
    provenance = _records(content.get("source_provenance"))
    verdicts = _records(evidence.get("verdicts"))
    claim_ids = [str(claim.get("id") or f"claim-{index}") for index, claim in enumerate(claims, 1)]
    verdict_ids = [str(verdict.get("claim_id") or "") for verdict in verdicts]
    source_ids = {
        str(source.get("source_id") or "") for source in provenance
        if str(source.get("source_id") or "")
    }
    ids_match = bool(
        len(claim_ids) == len(set(claim_ids))
        and len(verdict_ids) == len(set(verdict_ids))
        and set(claim_ids) == set(verdict_ids)
    )
    citations_governed = all(
        isinstance(claim.get("citations"), list)
        and bool(claim["citations"])
        and all(
            isinstance(citation, Mapping)
            and str(citation.get("source_id") or "") in source_ids
            for citation in claim["citations"]
        )
        for claim in claims
    )
    return bool(
        exact_revision
        and evidence.get("issue_id") == issue.get("id")
        # SQLite-backed fact-check records expose the persisted truth value as
        # integer 1, while in-memory fixtures commonly use boolean True.
        and bool(evidence.get("passed"))
        and str(evidence.get("reviewer") or "").strip()
        and str(evidence.get("content_fingerprint") or "").startswith("sha256:")
        and str(evidence.get("created_at") or "").strip()
        and isinstance(evidence.get("verdicts"), list)
        and ids_match and all(verdict.get("verified") is True for verdict in verdicts)
        and citations_governed
        and exact_scope_revision
        and scope.get("resource_id") == issue.get("id")
        and scope.get("action_type") == "create_draft"
        and scope.get("destination") == "beehiiv:draft"
        and str(scope.get("review_token") or "").startswith("review-v1:")
        and str(scope.get("material_fingerprint") or "").startswith("sha256:")
        and evidence.get("content_fingerprint") == scope.get("material_fingerprint")
    )


def build_operator_workflow(
    *,
    candidates: Sequence[Mapping[str, Any]] | None = None,
    issues: Sequence[Mapping[str, Any]] | None = None,
    fact_checks: Mapping[str, Any] | None = None,
    packages: Sequence[Mapping[str, Any]] | None = None,
    handoffs: Sequence[Mapping[str, Any]] | None = None,
    connectors: Sequence[Mapping[str, Any]] | None = None,
    performance: Sequence[Mapping[str, Any]] | None = None,
    assisted_pulls: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return exactly one safe next action for one internally coherent loop."""

    candidate_rows = _records(candidates)
    issue_rows = [
        item for item in _records(issues)
        if str(item.get("lifecycle") or "").casefold() not in TERMINAL_ISSUE_STATES
    ]
    package_rows = [
        item for item in _records(packages)
        if str(item.get("status") or "").casefold() not in TERMINAL_PACKAGE_STATES
    ]
    handoff_rows = _records(handoffs)
    connector_rows = _records(connectors)
    performance_rows = _records(performance)
    assisted_pull_rows = _records(assisted_pulls)
    checks = fact_checks if isinstance(fact_checks, Mapping) else {}

    # Prefer the newest actionable newsletter loop. An older active package
    # must not hide a newer draft that still needs human work.
    issue = _latest(issue_rows)
    package = _latest([
        item for item in package_rows
        if issue and str(item.get("issue_id") or "") == str(issue.get("id") or "")
    ])
    if issue is None:
        package = _latest(package_rows)
    candidate_id = str(
        (package or {}).get("candidate_id") or (issue or {}).get("candidate_id") or ""
    )
    candidate = next(
        (item for item in candidate_rows if str(item.get("id")) == candidate_id), None,
    )
    if candidate is None and issue is None and package is None:
        candidate = _latest(candidate_rows)

    # Never borrow an unrelated package for a newer issue.
    if package and issue and str(package.get("issue_id")) != str(issue.get("id")):
        package = None

    issue_lifecycle = str((issue or {}).get("lifecycle") or "").casefold()
    newsletter_drafted = bool(package) or issue_lifecycle in {
        "draft", "fact_checked", "approved", "exported", "scheduled", "published",
    }
    fact_checked = _valid_fact_check(
        issue, checks.get(str((issue or {}).get("id") or "")),
    )
    package_ready = bool(package)
    package_governance = (
        package.get("governance") if package and isinstance(package.get("governance"), Mapping)
        else {}
    )
    package_approval_ready = bool(package_ready and package_governance.get("approval_ready"))

    artifacts = _records((package or {}).get("artifacts"))
    x_dispatches = [
        artifact.get("dispatch") for artifact in artifacts
        if isinstance(artifact.get("dispatch"), Mapping)
    ]
    artifact_reconciliation_ready = bool(
        artifacts and len(x_dispatches) == len(artifacts)
        and all(artifact.get("dispatch_item_id") for artifact in artifacts)
    )
    newsletter_approved = bool(
        issue and (
            issue.get("approval_valid") is True
            or issue_lifecycle in {"approved", "exported", "scheduled", "published"}
        )
    )
    x_approved = bool(artifact_reconciliation_ready) and all(
        str(item.get("status") or "").casefold() in APPROVED_DISPATCH_STATES
        for item in x_dispatches
    )
    exact_approvals = bool(package_approval_ready and newsletter_approved and x_approved)

    required_resources: dict[str, set[str]] = {
        "beehiiv": {str((issue or {}).get("id") or "")},
        "x": {
            str(artifact.get("dispatch_item_id") or "") for artifact in artifacts
            if artifact.get("dispatch_item_id")
        },
    }
    required_resources["beehiiv"].discard("")
    matching_handoffs: dict[str, list[Mapping[str, Any]]] = {}
    for provider, resource_ids in required_resources.items():
        matching_handoffs[provider] = [
            item for item in handoff_rows
            if item.get("provider") == provider
            and str(item.get("resource_id") or "") in resource_ids
            and str(item.get("status") or "").casefold() in ACTIVE_HANDOFF_STATES
        ]
    handoff_ready = bool(exact_approvals and all(
        required_resources[provider]
        and required_resources[provider] <= {
            str(item.get("resource_id")) for item in matching_handoffs[provider]
        }
        for provider in ("beehiiv", "x")
    ))
    receipts_ready = bool(handoff_ready and all(
        all(
            item.get("status") == "completed" and item.get("receipt_external_id")
            for item in matching_handoffs[provider]
        )
        for provider in ("beehiiv", "x")
    ))
    campaign_id = str((package or {}).get("campaign_id") or "")
    post_ids = {str(artifact.get("post_id")) for artifact in artifacts if artifact.get("post_id")}
    measured = bool(receipts_ready and (
        any(
            (campaign_id and str(item.get("campaign_id") or "") == campaign_id)
            or str(item.get("post_id") or "") in post_ids
            for item in performance_rows
        )
        or any(
            item.get("status") == "completed"
            and int(item.get("post_measurements_received") or 0) >= 1
            and int(item.get("campaign_metrics_recorded") or 0) >= 1
            and campaign_id in {
                str(value) for value in item.get("measured_campaign_ids", [])
            }
            for item in assisted_pull_rows
        )
    ))

    raw_stages = [
        ("source", "1 · Source evidence", bool(candidate or issue or package)),
        ("candidate", "2 · Candidate", bool(candidate or issue or package)),
        ("newsletter", "3 · Newsletter draft", newsletter_drafted),
        ("fact_check", "4 · Fact-check", fact_checked),
        ("distribution", "5 · Distribution package", package_ready and package_approval_ready
         and artifact_reconciliation_ready),
        ("approval", "6 · Exact approvals", exact_approvals),
        ("handoff", "7 · Assisted handoffs", handoff_ready),
        ("receipt", "8 · Provider receipts", receipts_ready),
        ("measurement", "9 · Measure + learn", measured),
    ]
    stages: list[dict[str, Any]] = []
    prior_done = True
    for key, label, condition in raw_stages:
        done = bool(prior_done and condition)
        stages.append({"key": key, "label": label, "status": "done" if done else "waiting"})
        prior_done = done
    current_index = next((index for index, item in enumerate(stages) if item["status"] != "done"), -1)
    if current_index >= 0:
        stages[current_index]["status"] = "current"

    target = "measurement-panel"
    action = "Review measured results and use supported findings to shape the next candidate."
    code = "loop_complete"
    if current_index == 0:
        code, target, action = (
            "ingest_source", "sources-panel",
            "Ingest one governed source item, then let BrandMan deduplicate and score it.",
        )
    elif current_index == 1:
        code, target, action = (
            "select_candidate", "editorial-panel",
            "Select the highest-value source-backed candidate for this newsletter.",
        )
    elif current_index == 2:
        code, target, action = (
            "draft_newsletter", "editorial-panel",
            "Create and complete the newsletter draft from the selected candidate.",
        )
    elif current_index == 3:
        code, target, action = (
            "fact_check", "editorial-panel",
            f"Fact-check every claim in newsletter revision {(issue or {}).get('current_revision', '?')} against its recorded evidence.",
        )
    elif current_index == 4:
        if package and not artifact_reconciliation_ready:
            action = (
                "Reconcile the package’s X artifacts with their current dispatch records; "
                "approval stays blocked until every artifact loads."
            )
            code = "reconcile_distribution_artifacts"
        elif package and not package_approval_ready:
            action = str(
                package_governance.get("next_safe_action")
                or "Resolve the package evidence or destination blocker, then refresh."
            )
            code = "resolve_distribution_blocker"
        else:
            action = "Create the email, web, and X distribution package from this exact fact-checked revision."
            code = "create_distribution"
        target = "distribution-panel"
    elif current_index == 5:
        code, target = "review_exact_approval", "approvals-panel"
        if not newsletter_approved:
            action = "Review and approve or reject the exact newsletter revision shown below."
        else:
            missing = sum(
                str(item.get("status") or "").casefold() not in APPROVED_DISPATCH_STATES
                for item in x_dispatches
            )
            action = f"Review and approve or reject the next exact X draft ({missing} remaining)."
    elif current_index == 6:
        ready_providers = {
            str(item.get("connector_type") or item.get("provider") or "")
            for item in connector_rows
            if item.get("status") in {"connected", "healthy", "active"}
            and (item.get("configuration") or {}).get("delivery_mode")
            in {"browser_assisted", "mcp_assisted"}
        }
        missing_provider = next(
            (provider for provider in ("beehiiv", "x") if provider not in ready_providers), None,
        )
        if missing_provider:
            code, target, action = (
                "setup_assisted_connection", "connection",
                f"Set up the {missing_provider.title()} browser-assisted workflow; no API token is required.",
            )
        else:
            code, target, action = (
                "start_execution_helper", "handoffs-panel",
                "Start the assisted execution helper so it can materialize the already-approved handoffs.",
            )
    elif current_index == 7:
        pending = next((
            item for provider in ("beehiiv", "x") for item in matching_handoffs[provider]
            if item.get("status") != "completed" or not item.get("receipt_external_id")
        ), None)
        provider = str((pending or {}).get("provider") or "provider")
        operator_action = (
            pending.get("operator_next_action")
            if pending and isinstance(pending.get("operator_next_action"), Mapping) else {}
        )
        code, target, action = (
            str(operator_action.get("code") or "complete_provider_handoff"),
            "handoffs-panel",
            str(operator_action.get("text") or (
                f"Complete the approved {provider.title()} action in its provider UI and record the receipt."
            )),
        )
    elif current_index == 8:
        pending_pull = next((
            item for item in assisted_pull_rows
            if str(item.get("status") or "").casefold() in {"pending", "claimed"}
        ), None)
        failed_pull = next((
            item for item in reversed(assisted_pull_rows)
            if str(item.get("status") or "").casefold() == "failed"
        ), None)
        if pending_pull:
            code, target, action = (
                "complete_beehiiv_measurement_pull", "measurement-panel",
                "Complete the due read-only Beehiiv metadata and aggregate measurement pull; no subscriber data or provider changes are allowed.",
            )
        elif failed_pull:
            code, target, action = (
                "recover_beehiiv_measurement_pull", "measurement-panel",
                "Restore the Beehiiv read helper, then let the scheduler create a fresh aggregate-only pull. Do not enter subscriber data.",
            )
        else:
            code, target, action = (
                "await_measurement_pull", "measurement-panel",
                "Wait for the next scheduled aggregate Beehiiv pull, then review attributed campaign results before accepting any learning.",
            )

    focus = {
        # The candidate collection is intentionally an active-work view, so a
        # promoted candidate may no longer be present even though the focused
        # newsletter/package still retains its immutable lineage.  Preserve
        # that lineage identifier instead of making the workflow contradict
        # its own completed candidate stage.
        "candidate_id": candidate_id or None,
        "issue_id": (issue or {}).get("id"),
        "issue_revision": (issue or {}).get("current_revision"),
        "package_id": (package or {}).get("id"),
        "campaign_id": campaign_id or None,
    }
    completed = sum(item["status"] == "done" for item in stages)
    return {
        "schema_version": 1,
        "focus": focus,
        "stages": stages,
        "completed_steps": completed,
        "total_steps": len(stages),
        "progress_percent": round(100 * completed / len(stages)),
        "next_action": {"code": code, "text": action, "target": target},
        "assisted_measurement": {
            "pending": sum(item.get("status") in {"pending", "claimed"} for item in assisted_pull_rows),
            "completed": sum(item.get("status") == "completed" for item in assisted_pull_rows),
            "failed": sum(item.get("status") == "failed" for item in assisted_pull_rows),
            "read_only": True,
        },
        "safety": "This view can draft and guide review; it cannot approve or publish on your behalf.",
    }


__all__ = ["build_operator_workflow"]
