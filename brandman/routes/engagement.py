"""The engagement inbox."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from brandman import store
from brandman.dispatch import dispatch_item_to_dict

from brandman.main import (
    EngagementActorInput,
    EngagementDismissInput,
    EngagementDraftInput,
    _brand_engagement,
    _engagement_operation,
    dispatcher,
    engagement_store,
)

router = APIRouter()


@router.get("/api/brands/{slug}/engagement")
def list_engagement_opportunities(
    slug: str, state: str | None = None, limit: int = 100
) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _engagement_operation(
        lambda: engagement_store.list(brand_id=brand["id"], state=state, limit=limit)
    )


@router.get("/api/brands/{slug}/engagement/{opportunity_id}")
def get_engagement_opportunity(slug: str, opportunity_id: str) -> dict:
    return _engagement_operation(lambda: _brand_engagement(slug, opportunity_id)[1])


@router.get("/api/brands/{slug}/engagement/{opportunity_id}/history")
def get_engagement_history(slug: str, opportunity_id: str) -> list[dict]:
    def operation():
        _brand_engagement(slug, opportunity_id)
        return engagement_store.history(opportunity_id)
    return _engagement_operation(operation)


@router.post("/api/brands/{slug}/engagement/{opportunity_id}/draft-action")
def draft_engagement_action(
    slug: str, opportunity_id: str, input: EngagementDraftInput, request: Request,
) -> dict:
    def operation():
        _brand_engagement(slug, opportunity_id)
        opportunity, dispatch = engagement_store.draft_action(
            opportunity_id, input.action_type, dispatcher,
            actor=request.state.principal, text=input.text,
        )
        return {"opportunity": opportunity, "dispatch_item": dispatch_item_to_dict(dispatch)}
    return _engagement_operation(operation)


@router.post("/api/brands/{slug}/engagement/{opportunity_id}/submit-action")
def submit_engagement_action(
    slug: str, opportunity_id: str, input: EngagementActorInput, request: Request,
) -> dict:
    def operation():
        _brand_engagement(slug, opportunity_id)
        opportunity, dispatch = engagement_store.submit_action_for_approval(
            opportunity_id, dispatcher, actor=request.state.principal
        )
        return {"opportunity": opportunity, "dispatch_item": dispatch_item_to_dict(dispatch)}
    return _engagement_operation(operation)


@router.post("/api/brands/{slug}/engagement/{opportunity_id}/dismiss")
def dismiss_engagement_opportunity(
    slug: str, opportunity_id: str, input: EngagementDismissInput, request: Request,
) -> dict:
    def operation():
        _brand_engagement(slug, opportunity_id)
        return engagement_store.dismiss(
            opportunity_id, actor=request.state.principal, reason=input.reason
        )
    return _engagement_operation(operation)
