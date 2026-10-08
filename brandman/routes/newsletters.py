"""Newsletter issues, revisions, exports and approval snapshots."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from brandman import store
from brandman.beehiiv_runtime import (
    NewsletterExportJobError,
    enqueue_newsletter_export,
    get_newsletter_export_job,
    list_newsletter_export_jobs,
    resolve_beehiiv_export_account,
)
from brandman.brand_guidelines import BrandGuidelineError
from brandman.editorial import EditorialError
from brandman.operational_feedback import report_approval_dead_end

from brandman.main import (
    DistributionPackageInput,
    EditorialCleanupInput,
    NewsletterApprovalInput,
    NewsletterExportInput,
    NewsletterFactCheckInput,
    NewsletterIssueInput,
    NewsletterPolicyReviewInput,
    NewsletterQuickHitInput,
    NewsletterRejectionInput,
    NewsletterRevisionInput,
    NewsletterTransitionInput,
    _credential_store,
    _distribution_operation,
    _distribution_packages,
    approval_snapshot_store,
    beehiiv_lifecycle_projector,
    brand_guideline_store,
    editorial_store,
)

router = APIRouter()


@router.post("/api/brands/{slug}/newsletter-issues", status_code=201)
def create_newsletter_issue(slug: str, input: NewsletterIssueInput, request: Request) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return editorial_store.create_issue(
            brand["id"], input.content, created_by=request.state.principal,
            candidate_id=input.candidate_id,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Editorial candidate not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/brands/{slug}/newsletter-issues")
def list_newsletter_issues(slug: str, include_inactive: bool = False) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return [
        {**issue, "approval_scope": approval_snapshot_store.proposed_newsletter(issue)}
        for issue in editorial_store.list_issues(brand["id"], include_inactive=include_inactive)
    ]


@router.post("/api/newsletter-issues/{issue_id}/policy-review", status_code=201)
def record_newsletter_policy_review(
    issue_id: str, input: NewsletterPolicyReviewInput, request: Request,
) -> dict[str, Any]:
    try:
        result = brand_guideline_store.record_policy_review(
            issue_id=issue_id, revision=input.revision,
            reviewer=request.state.principal, checklist=input.checklist,
        )
        return {**result, "governance": editorial_store.get_issue(issue_id)["governance"]}
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except BrandGuidelineError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/newsletter-issues/{issue_id}/quick-hit-authorization", status_code=201)
def authorize_newsletter_quick_hit(
    issue_id: str, input: NewsletterQuickHitInput, request: Request,
) -> dict[str, Any]:
    try:
        result = brand_guideline_store.authorize_quick_hit(
            issue_id=issue_id, revision=input.revision,
            actor=request.state.principal, reason=input.reason,
        )
        return {**result, "governance": editorial_store.get_issue(issue_id)["governance"]}
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except BrandGuidelineError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/newsletter-issues/{issue_id}")
def get_newsletter_issue(issue_id: str) -> dict:
    try:
        return editorial_store.get_issue(issue_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error


@router.post("/api/newsletter-issues/{issue_id}/distribution-package", status_code=201)
def create_distribution_package(
    issue_id: str, input: DistributionPackageInput, request: Request,
) -> dict:
    """Create draft-only email, web, and X distribution artifacts."""
    try:
        issue = editorial_store.get_issue(issue_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    payload = input.model_dump(mode="json")
    return _distribution_operation(lambda: _distribution_packages().create(
        issue["brand_id"], issue_id, actor=request.state.principal, **payload,
    ))


@router.get("/api/newsletter-issues/{issue_id}/revisions")
def list_newsletter_revisions(issue_id: str) -> list[dict]:
    try:
        editorial_store.get_issue(issue_id)
        return editorial_store.list_revisions(issue_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error


@router.post("/api/newsletter-issues/{issue_id}/abandon")
def abandon_newsletter_issue(
    issue_id: str, input: EditorialCleanupInput, request: Request,
) -> dict:
    try:
        return editorial_store.abandon_issue(
            issue_id, actor=request.state.principal, reason=input.reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/newsletter-issues/{issue_id}/archive")
def archive_newsletter_issue(
    issue_id: str, input: EditorialCleanupInput, request: Request,
) -> dict:
    try:
        return editorial_store.archive_issue(
            issue_id, actor=request.state.principal, reason=input.reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/newsletter-issues/{issue_id}/history")
def get_newsletter_issue_history(issue_id: str) -> list[dict]:
    try:
        return editorial_store.list_issue_history(issue_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error


@router.get("/api/newsletter-issues/{issue_id}/provider-reconciliations")
def get_newsletter_provider_reconciliations(issue_id: str) -> list[dict]:
    try:
        editorial_store.get_issue(issue_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    return beehiiv_lifecycle_projector.list(issue_id)


@router.get("/api/newsletter-issues/{issue_id}/fact-check")
def get_newsletter_fact_check(issue_id: str, revision: int | None = None) -> dict | None:
    try:
        return editorial_store.get_fact_check(issue_id, revision)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error


@router.patch("/api/newsletter-issues/{issue_id}")
def revise_newsletter_issue(
    issue_id: str, input: NewsletterRevisionInput, request: Request,
) -> dict:
    try:
        revised = editorial_store.revise_issue(
            issue_id, input.changes, created_by=request.state.principal,
            change_note=input.change_note,
        )
        approval_snapshot_store.invalidate_resource(
            issue_id, actor=request.state.principal,
            reason="newsletter revised; approval invalidated",
        )
        return revised
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/newsletter-issues/{issue_id}/transition")
def transition_newsletter_issue(issue_id: str, input: NewsletterTransitionInput) -> dict:
    try:
        return editorial_store.transition(issue_id, input.target)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except EditorialError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/newsletter-issues/{issue_id}/fact-check")
def fact_check_newsletter_issue(
    issue_id: str, input: NewsletterFactCheckInput, request: Request,
) -> dict:
    try:
        return editorial_store.record_fact_check(
            issue_id, expected_revision=input.revision, reviewer=request.state.principal,
            verdicts=[verdict.model_dump() for verdict in input.verdicts], notes=input.notes,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/newsletter-issues/{issue_id}/approve")
def approve_newsletter_issue(issue_id: str, input: NewsletterApprovalInput, request: Request) -> dict:
    try:
        approved, snapshot = approval_snapshot_store.approve_newsletter(
            issue_id, approver=request.state.principal, revision=input.revision,
            review_token=input.review_token,
        )
        return {**approved, "approval_snapshot": snapshot}
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/newsletter-issues/{issue_id}/reject")
def reject_newsletter_issue(
    issue_id: str, input: NewsletterRejectionInput, request: Request,
) -> dict:
    """Reject one displayed revision and return a fresh working copy for changes."""
    try:
        rejected = editorial_store.reject_issue(
            issue_id, actor=request.state.principal, reason=input.reason,
            expected_revision=input.revision,
        )
        approval_snapshot_store.invalidate_resource(
            issue_id, actor=request.state.principal,
            reason=f"newsletter revision {input.revision} rejected: {input.reason}",
        )
        return rejected
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/newsletter-issues/{issue_id}/export-preview")
def prepare_newsletter_export(issue_id: str) -> dict:
    try:
        return editorial_store.prepare_export(issue_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except EditorialError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/newsletter-issues/{issue_id}/export-draft", status_code=202)
def enqueue_newsletter_draft_export(
    issue_id: str, input: NewsletterExportInput
) -> dict:
    """Queue exact approved content for Beehiiv draft creation, never publication."""

    try:
        # Editorial governance is the first boundary.  Reject stale or
        # unapproved content before consulting connector or secret state.
        editorial_store.prepare_export(issue_id)
        issue = editorial_store.get_issue(issue_id)
        account = resolve_beehiiv_export_account(
            issue["brand_id"], input.connector_account_id,
            credentials=_credential_store(),
        )
        return enqueue_newsletter_export(
            editorial_store,
            issue_id,
            brand_id=issue["brand_id"],
            connector_account_id=account["id"],
            run_after=input.run_after.isoformat() if input.run_after else None,
            priority=input.priority,
            max_attempts=input.max_attempts,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    except (NewsletterExportJobError, EditorialError) as error:
        try:
            issue = editorial_store.get_issue(issue_id)
            report_approval_dead_end(
                brand_id=issue["brand_id"], resource_id=issue_id,
                resource_type="newsletter_issue", operation="export-draft",
            )
        except Exception:
            pass
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/newsletter-issues/{issue_id}/export-jobs")
def get_newsletter_issue_export_jobs(issue_id: str) -> list[dict]:
    try:
        editorial_store.get_issue(issue_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter issue not found") from error
    return list_newsletter_export_jobs(issue_id)


@router.get("/api/newsletter-export-jobs/{job_id}")
def get_newsletter_draft_export_job(job_id: str) -> dict:
    try:
        return get_newsletter_export_job(job_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Newsletter export job not found") from error


@router.get("/api/brands/{slug}/approval-snapshots")
def list_approval_snapshots(slug: str, active_only: bool = False) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return approval_snapshot_store.list(brand["id"], active_only=active_only)


@router.get("/api/approval-snapshots/{snapshot_id}")
def get_approval_snapshot(snapshot_id: str) -> dict:
    try:
        return approval_snapshot_store.get(snapshot_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Approval snapshot not found") from error
