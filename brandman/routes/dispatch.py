"""Governed dispatch items and the publishing planner."""
from __future__ import annotations

from typing import Any
import secrets

from fastapi import APIRouter, HTTPException, Request

from brandman import store
from brandman.dispatch import DispatchError, Lifecycle, audit_event_to_dict, dispatch_item_to_dict
from brandman.operational_feedback import report_approval_dead_end
from brandman.publishing_planner import PublishingPlannerError

from brandman.main import (
    DispatchActorInput,
    DispatchApprovalInput,
    DispatchBatchApprovalInput,
    DispatchCreateInput,
    DispatchEditInput,
    DispatchRevisionInput,
    PublishingPlanItemInput,
    PublishingPlanSettingsInput,
    PublishingReflowCommitInput,
    PublishingReflowPreviewInput,
    PublishingReflowUndoInput,
    SYSTEM_QUEUE_ACTOR,
    _dispatch_operation,
    approval_snapshot_store,
    dispatch_store,
    dispatcher,
    publishing_planner,
)

router = APIRouter()


@router.get("/api/brands/{slug}/publishing-plan")
def get_publishing_plan(slug: str) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return publishing_planner.view(brand["id"])


@router.put("/api/brands/{slug}/publishing-plan/settings")
def update_publishing_plan_settings(
    slug: str, input: PublishingPlanSettingsInput, request: Request,
) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        payload = input.model_dump()
        return publishing_planner.update_settings(
            brand["id"], actor=request.state.principal, **payload,
        )
    except PublishingPlannerError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.put("/api/brands/{slug}/publishing-plan/items/{item_type}/{item_id}")
def update_publishing_plan_item(
    slug: str, item_type: str, item_id: str,
    input: PublishingPlanItemInput, request: Request,
) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return publishing_planner.update_item(
            brand["id"], item_type, item_id, actor=request.state.principal,
            **input.model_dump(mode="json"),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Planner item not found") from error
    except PublishingPlannerError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/brands/{slug}/publishing-plan/reflow/preview")
def preview_publishing_plan_reflow(
    slug: str, input: PublishingReflowPreviewInput, request: Request,
) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return publishing_planner.preview_reflow(
            brand["id"], start_at=input.start_at.isoformat(), actor=request.state.principal,
        )
    except PublishingPlannerError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/brands/{slug}/publishing-plan/reflow/commit")
def commit_publishing_plan_reflow(
    slug: str, input: PublishingReflowCommitInput, request: Request,
) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return publishing_planner.commit_reflow(
            brand["id"], input.preview_id, actor=request.state.principal,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Reflow preview not found") from error
    except PublishingPlannerError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/brands/{slug}/publishing-plan/reflow/undo")
def undo_publishing_plan_reflow(
    slug: str, input: PublishingReflowUndoInput, request: Request,
) -> dict[str, Any]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return publishing_planner.undo_reflow(
            brand["id"], input.commit_id, actor=request.state.principal,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Reflow commit not found") from error
    except PublishingPlannerError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/dispatch-items/{item_id}/validation")
def validate_dispatch_item(item_id: str) -> dict:
    return _dispatch_operation(lambda: dispatcher.validate(item_id).as_dict())


@router.get("/api/brands/{slug}/dispatch-items")
def list_dispatch_items(slug: str, status: Lifecycle | None = None) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return [
        {**dispatch_item_to_dict(item), "approval_scope": approval_snapshot_store.proposed_dispatch(item)}
        for item in dispatch_store.list_items(brand_id=brand["id"], status=status)
    ]


@router.post("/api/brands/{slug}/dispatch-items", status_code=201)
def create_dispatch_item(slug: str, input: DispatchCreateInput, request: Request) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return dispatch_item_to_dict(dispatcher.create(
        input.connector, input.payload, brand_id=brand["id"], actor=request.state.principal,
    ))


@router.get("/api/dispatch-items/{item_id}")
def get_dispatch_item(item_id: str) -> dict:
    return _dispatch_operation(lambda: dispatch_item_to_dict(dispatch_store.get(item_id)))


@router.get("/api/dispatch-items/{item_id}/revisions/{revision}")
def get_dispatch_revision(item_id: str, revision: int) -> dict:
    try:
        return dispatch_store.get_revision(item_id, revision)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Dispatch revision not found") from error


@router.patch("/api/dispatch-items/{item_id}")
def edit_dispatch_item(item_id: str, input: DispatchEditInput, request: Request) -> dict:
    def operation():
        edited = dispatcher.edit(item_id, input.payload, actor=request.state.principal)
        approval_snapshot_store.invalidate_resource(
            item_id, actor=request.state.principal, reason="dispatch edited; approval invalidated",
        )
        return dispatch_item_to_dict(edited)
    return _dispatch_operation(operation)


@router.post("/api/dispatch-items/{item_id}/submit")
def submit_dispatch_item(item_id: str, input: DispatchActorInput, request: Request) -> dict:
    return _dispatch_operation(lambda: dispatch_item_to_dict(
        dispatcher.submit_for_approval(item_id, actor=request.state.principal)
    ))


@router.post("/api/dispatch-items/{item_id}/approve")
def approve_dispatch_item(item_id: str, input: DispatchApprovalInput, request: Request) -> dict:
    def operation():
        approved, snapshot = approval_snapshot_store.approve_dispatch(
            item_id, revision=input.revision, review_token=input.review_token,
            approver=request.state.principal,
        )
        return {
            **dispatch_item_to_dict(approved),
            "approval_snapshot": snapshot,
        }
    return _dispatch_operation(operation)


@router.post("/api/brands/{slug}/dispatch-items/approve-batch")
def approve_dispatch_batch(slug: str, input: DispatchBatchApprovalInput, request: Request) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")

    def operation():
        member_ids = {member.id for member in input.items}
        if len(member_ids) != len(input.items):
            raise ValueError("batch item ids must be unique")
        for item_id in member_ids:
            item = dispatch_store.get(item_id)
            if item.brand_id != brand["id"]:
                raise KeyError(item_id)
        batch_id = input.batch_id or secrets.token_hex(16)
        approved = approval_snapshot_store.approve_dispatch_batch(
            [member.model_dump() for member in input.items],
            approver=request.state.principal, batch_id=batch_id,
        )
        return [{
            **dispatch_item_to_dict(item),
            "approval_snapshot": snapshot,
        } for item, snapshot in approved]

    return _dispatch_operation(operation)


@router.post("/api/dispatch-items/{item_id}/reject")
def reject_dispatch_item(item_id: str, input: DispatchRevisionInput, request: Request) -> dict:
    return _dispatch_operation(lambda: dispatch_item_to_dict(dispatcher.reject(
        item_id, revision=input.revision, actor=request.state.principal
    )))


@router.post("/api/dispatch-items/{item_id}/queue")
def queue_dispatch_item(item_id: str) -> dict:
    """Queue an already-approved item under a server-controlled system identity."""
    try:
        return dispatch_item_to_dict(dispatcher.queue(item_id, actor=SYSTEM_QUEUE_ACTOR))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Dispatch item not found") from error
    except (DispatchError, ValueError) as error:
        try:
            item = dispatch_store.get(item_id)
            report_approval_dead_end(
                brand_id=item.brand_id, resource_id=item_id,
                resource_type="dispatch_item", operation="queue",
            )
        except Exception:
            pass
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/dispatch-items/{item_id}/audit")
def get_dispatch_audit(item_id: str) -> list[dict]:
    return _dispatch_operation(lambda: [audit_event_to_dict(event) for event in dispatch_store.list_audit(item_id)])
