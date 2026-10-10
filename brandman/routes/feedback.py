"""Product feedback."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request

from brandman import store

from brandman.main import (
    FeedbackCommentInput,
    FeedbackReconcileInput,
    FeedbackReopenInput,
    FeedbackResolveInput,
    FeedbackStartInput,
    FeedbackVerifyInput,
    ProductFeedbackInput,
    _brand_feedback,
    _feedback_operation,
    _feedback_store,
)

router = APIRouter()


@router.post("/api/brands/{slug}/product-feedback", status_code=201)
def report_product_feedback(slug: str, input: ProductFeedbackInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _feedback_operation(
        lambda: _feedback_store().report(brand_id=brand["id"], **input.model_dump())
    )


@router.get("/api/brands/{slug}/product-feedback")
def list_product_feedback(
    slug: str,
    status: Literal["open", "in_progress", "resolved", "verified"] | None = None,
    component: str | None = None,
    assignee: str | None = None,
    severity: Literal["low", "medium", "high", "critical"] | None = None,
) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _feedback_store().list(
        brand_id=brand["id"], status=status, component=component,
        assignee=assignee, severity=severity,
    )


@router.get("/api/brands/{slug}/product-feedback/{feedback_id}")
def get_brand_product_feedback(slug: str, feedback_id: str) -> dict:
    return _brand_feedback(slug, feedback_id)


@router.get("/api/brands/{slug}/product-feedback/{feedback_id}/history")
def get_brand_product_feedback_history(slug: str, feedback_id: str) -> list[dict]:
    _brand_feedback(slug, feedback_id)
    return _feedback_operation(lambda: _feedback_store().history(feedback_id))


@router.post("/api/brands/{slug}/product-feedback/{feedback_id}/comments", status_code=201)
def comment_on_brand_product_feedback(
    slug: str, feedback_id: str, input: FeedbackCommentInput, request: Request,
) -> dict:
    _brand_feedback(slug, feedback_id)
    return _feedback_operation(lambda: _feedback_store().comment(
        feedback_id, input.body, actor=request.state.principal,
    ))


@router.post("/api/brands/{slug}/product-feedback/{feedback_id}/start")
def start_brand_product_feedback(
    slug: str, feedback_id: str, input: FeedbackStartInput, request: Request,
) -> dict:
    _brand_feedback(slug, feedback_id)
    return _feedback_operation(lambda: _feedback_store().start(
        feedback_id, assignee=input.assignee, actor=request.state.principal,
        implementation_links=input.implementation_links,
        implementation_notes=input.implementation_notes,
    ))


@router.post("/api/brands/{slug}/product-feedback/{feedback_id}/resolve")
def resolve_brand_product_feedback(
    slug: str, feedback_id: str, input: FeedbackResolveInput, request: Request,
) -> dict:
    _brand_feedback(slug, feedback_id)
    return _feedback_operation(lambda: _feedback_store().resolve(
        feedback_id, actor=request.state.principal,
        resolution_evidence=input.resolution_evidence,
        implementation_links=input.implementation_links,
        implementation_notes=input.implementation_notes,
    ))


@router.post("/api/brands/{slug}/product-feedback/{feedback_id}/verify")
def verify_brand_product_feedback(
    slug: str, feedback_id: str, input: FeedbackVerifyInput, request: Request,
) -> dict:
    _brand_feedback(slug, feedback_id)
    return _feedback_operation(lambda: _feedback_store().verify(
        feedback_id, actor=request.state.principal, evidence=input.evidence,
    ))


@router.post("/api/brands/{slug}/product-feedback/{feedback_id}/reopen")
def reopen_brand_product_feedback(
    slug: str, feedback_id: str, input: FeedbackReopenInput, request: Request,
) -> dict:
    _brand_feedback(slug, feedback_id)
    return _feedback_operation(lambda: _feedback_store().reopen(
        feedback_id, actor=request.state.principal, reason=input.reason,
    ))


@router.post("/api/product-feedback/reconcile")
def reconcile_product_feedback(input: FeedbackReconcileInput, request: Request) -> list[dict]:
    return _feedback_operation(lambda: _feedback_store().reconcile_shipped_component(
        input.component, keywords=input.keywords,
        implementation_links=input.implementation_links, actor=request.state.principal,
    ))


@router.get("/api/product-feedback/{feedback_id}")
def get_product_feedback(feedback_id: str) -> dict:
    return _feedback_operation(lambda: _feedback_store().get(feedback_id))


@router.get("/api/product-feedback/{feedback_id}/history")
def get_product_feedback_history(feedback_id: str) -> list[dict]:
    return _feedback_operation(lambda: _feedback_store().history(feedback_id))


@router.post("/api/product-feedback/{feedback_id}/comments", status_code=201)
def comment_on_product_feedback(
    feedback_id: str, input: FeedbackCommentInput, request: Request,
) -> dict:
    return _feedback_operation(lambda: _feedback_store().comment(
        feedback_id, input.body, actor=request.state.principal,
    ))


@router.post("/api/product-feedback/{feedback_id}/start")
def start_product_feedback(feedback_id: str, input: FeedbackStartInput, request: Request) -> dict:
    return _feedback_operation(lambda: _feedback_store().start(
        feedback_id, assignee=input.assignee, actor=request.state.principal,
        implementation_links=input.implementation_links,
        implementation_notes=input.implementation_notes,
    ))


@router.post("/api/product-feedback/{feedback_id}/resolve")
def resolve_product_feedback(feedback_id: str, input: FeedbackResolveInput, request: Request) -> dict:
    return _feedback_operation(lambda: _feedback_store().resolve(
        feedback_id, actor=request.state.principal,
        resolution_evidence=input.resolution_evidence,
        implementation_links=input.implementation_links,
        implementation_notes=input.implementation_notes,
    ))


@router.post("/api/product-feedback/{feedback_id}/verify")
def verify_product_feedback(feedback_id: str, input: FeedbackVerifyInput, request: Request) -> dict:
    return _feedback_operation(lambda: _feedback_store().verify(
        feedback_id, actor=request.state.principal, evidence=input.evidence
    ))


@router.post("/api/product-feedback/{feedback_id}/reopen")
def reopen_product_feedback(feedback_id: str, input: FeedbackReopenInput, request: Request) -> dict:
    return _feedback_operation(lambda: _feedback_store().reopen(
        feedback_id, actor=request.state.principal, reason=input.reason
    ))
