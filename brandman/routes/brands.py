"""Brands, brand context, settings, guidelines and operator workflow."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from brandman import store
from brandman.beehiiv_assisted_pull import BeehiivAssistedPullError
from brandman.brand_guidelines import BrandGuidelineError
from brandman.brand_settings import BrandSettingsError, BrandSettingsStore
from brandman.distribution_package import DistributionPackageError
from brandman.editorial import EditorialError
from brandman.execution_handoff import ExecutionHandoffError
from brandman.operator_workflow import build_operator_workflow
from brandman.readiness import LiveReadinessService

from brandman.main import (
    BrandGuidelineCreateInput,
    BrandGuidelineVersionInput,
    BrandInput,
    BrandSettingsUpdateInput,
    GuidelineActionInput,
    OperatorProposalConfirmInput,
    OperatorProposalPreviewInput,
    _distribution_packages,
    _operator_proposal_operation,
    approval_snapshot_store,
    beehiiv_assisted_pull_store,
    brand_guideline_store,
    editorial_store,
    execution_handoff_store,
    operator_proposal_store,
    periodic_orchestrator,
    provider_usage_ledger,
)

router = APIRouter()


@router.get("/api/brands/{slug}/readiness")
def get_live_readiness(slug: str) -> dict:
    """Return a provider-safe, read-only launch preflight for one brand."""
    try:
        return LiveReadinessService(store.DATA_PATH).inspect(slug)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Brand not found") from error


@router.get("/api/brands/{slug}/settings")
def get_brand_settings(slug: str) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    settings = BrandSettingsStore(store.DATA_PATH)
    return {
        "brand": brand,
        "audit": settings.audit(brand["id"]),
        "rate_cards": provider_usage_ledger.list_prices(brand["id"]),
        "orchestration": periodic_orchestrator.status(brand_id=brand["id"]),
        "schedules": periodic_orchestrator.list_schedules(brand_id=brand["id"]),
    }


@router.patch("/api/brands/{slug}/settings")
def update_brand_settings(slug: str, input: BrandSettingsUpdateInput, request: Request) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    changes = input.model_dump(exclude={"reason"}, exclude_none=True)
    try:
        return BrandSettingsStore(store.DATA_PATH).update(
            brand["id"], changes, actor=request.state.principal, reason=input.reason,
        )
    except BrandSettingsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/brands")
def list_brands() -> list[dict]:
    return store.rows("SELECT * FROM brands ORDER BY name")


@router.post("/api/brands", status_code=201)
def create_brand(input: BrandInput) -> dict:
    try:
        return store.create_brand(input.model_dump())
    except Exception as error:
        raise HTTPException(status_code=409, detail="Brand slug already exists") from error


@router.get("/api/brands/{slug}/context")
def get_brand_context(slug: str) -> dict:
    context = store.brand_context(slug)
    if not context:
        raise HTTPException(status_code=404, detail="Brand not found")
    context["active_guidelines"] = [
        item for item in brand_guideline_store.list(context["id"])
        if item.get("status") == "active"
    ]
    return context


@router.get("/api/brands/{slug}/guidelines")
def list_brand_guidelines(slug: str, include_archived: bool = False) -> list[dict[str, Any]]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return brand_guideline_store.list(brand["id"], include_archived=include_archived)


@router.get("/api/brands/{slug}/guidelines/active")
def get_active_brand_guideline(slug: str, content_type: str, channel: str) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    result = brand_guideline_store.resolve(brand["id"], content_type, channel)
    if result is None:
        raise HTTPException(status_code=404, detail="No active guideline applies to this scope")
    return result


@router.post("/api/brands/{slug}/guidelines", status_code=201)
def create_brand_guideline(
    slug: str, input: BrandGuidelineCreateInput, request: Request,
) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return brand_guideline_store.create(
            brand_id=brand["id"], actor=request.state.principal, **input.model_dump(),
        )
    except BrandGuidelineError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/brand-guidelines/{guideline_id}/versions", status_code=201)
def create_brand_guideline_version(
    guideline_id: str, input: BrandGuidelineVersionInput, request: Request,
) -> dict[str, Any]:
    try:
        return brand_guideline_store.create_version(
            guideline_id, actor=request.state.principal, **input.model_dump(),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except BrandGuidelineError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/brand-guidelines/{guideline_id}/versions/{version}/activate")
def activate_brand_guideline_version(
    guideline_id: str, version: int, input: GuidelineActionInput, request: Request,
) -> dict[str, Any]:
    try:
        return brand_guideline_store.activate(
            guideline_id, version, actor=request.state.principal, reason=input.reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except BrandGuidelineError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/brand-guidelines/{guideline_id}/audit")
def get_brand_guideline_audit(guideline_id: str) -> list[dict[str, Any]]:
    try:
        return brand_guideline_store.audit(guideline_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.delete("/api/brand-guidelines/{guideline_id}")
def archive_brand_guideline(
    guideline_id: str, input: GuidelineActionInput, request: Request,
) -> dict[str, Any]:
    try:
        return brand_guideline_store.archive(
            guideline_id, actor=request.state.principal, reason=input.reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except BrandGuidelineError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/brands/{slug}/calendar")
def get_content_calendar(slug: str) -> list[dict]:
    if not store.get_brand(slug):
        raise HTTPException(status_code=404, detail="Brand not found")
    return store.content_calendar(slug)


@router.get("/api/brands/{slug}/operator-workflow")
def get_operator_workflow(slug: str) -> dict:
    """Return the one authoritative, coherent nine-step operator journey."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    issues = [
        {**issue, "approval_scope": approval_snapshot_store.proposed_newsletter(issue)}
        for issue in editorial_store.list_issues(brand["id"])
    ]
    fact_checks = {
        issue["id"]: editorial_store.get_fact_check(
            issue["id"], int(issue["current_revision"]),
        )
        for issue in issues
        if issue.get("id") and issue.get("current_revision") is not None
    }
    performance = store.rows(
        """SELECT * FROM performance_records WHERE brand_id=? AND NOT EXISTS (
            SELECT 1 FROM fixture_quarantine_registry q
            WHERE q.table_name='performance_records'
              AND q.record_key_json=json_array(performance_records.id))
            ORDER BY observed_at DESC""",
        (brand["id"],),
    )
    # Beehiiv aggregate records intentionally have no canonical ``posts`` row:
    # the governed asset is the newsletter issue.  Derive campaign attribution
    # from the exact completed draft receipt and its matching provider metric
    # event so the operator loop can recognize real measurement evidence.
    performance.extend(store.rows(
        """SELECT DISTINCT t.campaign_id,NULL AS post_id,e.observed_at
           FROM connector_events e
           JOIN connector_accounts a ON a.id=e.connector_account_id
           JOIN execution_tasks t ON t.brand_id=a.brand_id
             AND t.provider='beehiiv' AND t.status='completed'
             AND t.receipt_external_id=json_extract(e.payload,'$.provider_external_id')
           WHERE a.brand_id=? AND e.event_type='metric_observed'
             AND t.campaign_id IS NOT NULL""",
        (brand["id"],),
    ))
    try:
        return build_operator_workflow(
            candidates=editorial_store.list_candidates(brand["id"]),
            issues=issues,
            fact_checks=fact_checks,
            packages=_distribution_packages().list(brand["id"]),
            handoffs=execution_handoff_store.operator_view(brand["id"]),
            connectors=store.list_connector_accounts(brand["id"]),
            performance=performance,
            assisted_pulls=beehiiv_assisted_pull_store.list(brand["id"]),
        )
    except (
        EditorialError, DistributionPackageError, ExecutionHandoffError,
        BeehiivAssistedPullError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/brands/{slug}/operator-proposals/preview", status_code=201)
def preview_operator_proposal(
    slug: str, input: OperatorProposalPreviewInput, request: Request,
) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _operator_proposal_operation(lambda: operator_proposal_store.preview(
        brand_id=brand["id"], actor=request.state.principal, **input.model_dump(),
    ))


@router.get("/api/brands/{slug}/operator-proposals/{proposal_id}")
def get_operator_proposal(slug: str, proposal_id: str) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _operator_proposal_operation(
        lambda: operator_proposal_store.get(proposal_id, brand_id=brand["id"])
    )


@router.post("/api/brands/{slug}/operator-proposals/{proposal_id}/confirm")
def confirm_operator_proposal(
    slug: str, proposal_id: str, input: OperatorProposalConfirmInput, request: Request,
) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _operator_proposal_operation(lambda: operator_proposal_store.confirm(
        proposal_id, brand_id=brand["id"], actor=request.state.principal,
    ))
