"""Assisted execution tasks, agents and controls."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request

from brandman import store
from brandman.beehiiv_assisted_pull import BeehiivAssistedPullError
from brandman.dispatch import DispatchError
from brandman.editorial import EditorialError
from brandman.execution_handoff import ExecutionHandoffError

from brandman.main import (
    BeehiivPrivateDraftManifestInput,
    ExecutionAgentInput,
    ExecutionClaimInput,
    ExecutionControlInput,
    ExecutionDestinationInput,
    ExternalActionStartInput,
    ExternalReceiptInput,
    PublicActionConfirmationInput,
    beehiiv_assisted_pull_store,
    execution_agent_registry,
    execution_handoff_store,
)

router = APIRouter()


@router.get("/api/brands/{slug}/execution-agents")
def list_execution_agents(slug: str) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return execution_agent_registry.list(brand["id"])


@router.post("/api/brands/{slug}/execution-agents")
def configure_execution_agent(slug: str, input: ExecutionAgentInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return execution_agent_registry.configure(
            brand["id"], input.agent_id, input.channel, enabled=input.enabled,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/api/brands/{slug}/execution-agents/{agent_id}/heartbeat")
def heartbeat_execution_agent(slug: str, agent_id: str) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return execution_agent_registry.heartbeat(brand["id"], agent_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Execution agent not found or disabled") from error


@router.get("/api/brands/{slug}/execution-tasks")
def list_execution_tasks(
    slug: str,
    status: Literal["pending", "claimed", "needs_attention", "completed", "stale"] | None = None,
) -> list[dict]:
    """Materialize and list exact-approved browser/MCP delivery handoffs."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        execution_handoff_store.ensure_for_brand(brand["id"])
        return execution_handoff_store.list(brand["id"], status=status)
    except (ExecutionHandoffError, EditorialError, DispatchError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/brands/{slug}/execution-console")
def get_execution_console(slug: str) -> dict:
    """Read-only operator context for assisted delivery and aggregate pulls."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        execution_handoff_store.ensure_for_brand(brand["id"])
        tasks = execution_handoff_store.operator_view(brand["id"])
        agents = execution_agent_registry.list(brand["id"])
        now = datetime.now().astimezone()
        for agent in agents:
            heartbeat = agent.get("last_heartbeat_at")
            try:
                heartbeat_age = (now - datetime.fromisoformat(str(heartbeat))).total_seconds()
            except (TypeError, ValueError):
                heartbeat_age = None
            agent["heartbeat_age_seconds"] = heartbeat_age
            agent["ready_to_claim"] = bool(
                agent.get("enabled") and heartbeat_age is not None
                and 0 <= heartbeat_age <= 900
            )
        pulls = beehiiv_assisted_pull_store.list(brand["id"])
    except (
        ExecutionHandoffError, EditorialError, DispatchError, BeehiivAssistedPullError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {
        "schema_version": 1,
        "tasks": tasks,
        "execution_agents": agents,
        "beehiiv_aggregate_pulls": pulls,
        "safety": {
            "provider_write_performed": False,
            "claim_grants_approval": False,
            "receipt_reconciles_existing_result": True,
            "subscriber_data_allowed": False,
        },
    }


@router.get("/api/brands/{slug}/execution-controls")
def list_execution_controls(slug: str) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return {
        "controls": execution_handoff_store.controls(brand["id"]),
        "audit": execution_handoff_store.control_audit(brand["id"]),
    }


@router.put("/api/brands/{slug}/execution-controls/{provider}")
def set_execution_control(
    slug: str, provider: Literal["all", "beehiiv", "x"],
    input: ExecutionControlInput, request: Request,
) -> dict:
    """Authenticated operator kill switch for new assisted-execution claims."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return execution_handoff_store.set_control(
            brand["id"], provider, enabled=input.enabled, actor=request.state.principal,
        )
    except ExecutionHandoffError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/execution-tasks/{task_id}/claim")
def claim_execution_task(task_id: str, input: ExecutionClaimInput) -> dict:
    """Lease one approved action; claiming cannot grant or change approval."""
    try:
        return execution_handoff_store.claim(
            task_id, actor=input.actor, lease_seconds=input.lease_seconds,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Execution task not found") from error
    except (ExecutionHandoffError, EditorialError, DispatchError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.put("/api/execution-tasks/{task_id}/destination")
def bind_execution_task_destination(
    task_id: str, input: ExecutionDestinationInput, request: Request,
) -> dict:
    """Authenticated operator binding for an ambiguous assisted destination."""
    try:
        return execution_handoff_store.bind_destination_account(
            task_id, actor=request.state.principal, **input.model_dump(),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Execution task not found") from error
    except (ExecutionHandoffError, EditorialError, DispatchError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/execution-tasks/{task_id}/begin-external-action")
def begin_external_execution_action(task_id: str, input: ExternalActionStartInput) -> dict:
    """Mark the exact last safe boundary immediately before the provider click."""
    try:
        return execution_handoff_store.begin_external_action(task_id, **input.model_dump())
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Execution task not found") from error
    except (ExecutionHandoffError, EditorialError, DispatchError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/execution-tasks/{task_id}/receipt")
def submit_external_execution_receipt(task_id: str, input: ExternalReceiptInput) -> dict:
    """Reconcile a provider-UI result; this endpoint performs no provider write."""
    try:
        return execution_handoff_store.submit_receipt(task_id, **input.model_dump())
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Execution task not found") from error
    except (ExecutionHandoffError, EditorialError, DispatchError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/execution-tasks/{task_id}/beehiiv-private-draft-manifest")
def prepare_beehiiv_private_draft_manifest(
    task_id: str, input: BeehiivPrivateDraftManifestInput,
) -> dict:
    """Prepare one exact private-draft browser action; never schedules or sends."""
    try:
        return execution_handoff_store.beehiiv_private_draft_manifest(
            task_id, **input.model_dump(),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Execution task not found") from error
    except (ExecutionHandoffError, EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/execution-tasks/{task_id}/confirm-public-action")
def confirm_public_execution_action(
    task_id: str, input: PublicActionConfirmationInput, request: Request,
) -> dict:
    """Confirm one exact claimed X action now; performs no provider write.

    This authenticated human-facing boundary is intentionally not mirrored by
    the MCP execution tools.  A helper may claim and reconcile work, but cannot
    create the separate action-time confirmation through its MCP surface.
    """
    try:
        return execution_handoff_store.confirm_public_action(
            task_id, actor=request.state.principal, **input.model_dump(),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Execution task not found") from error
    except (ExecutionHandoffError, EditorialError, DispatchError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/execution-tasks/{task_id}/audit")
def get_execution_task_audit(task_id: str) -> list[dict]:
    try:
        return execution_handoff_store.audit(task_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Execution task not found") from error
