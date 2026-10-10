"""Periodic orchestration status and ticks."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from brandman import store

from brandman.main import (
    OrchestrationTickInput,
    ScheduleControlInput,
    periodic_orchestrator,
)

router = APIRouter()


@router.get("/api/orchestration/status")
def get_orchestration_status() -> dict:
    return periodic_orchestrator.status()


@router.get("/api/orchestration/schedules")
def list_orchestration_schedules() -> list[dict]:
    return periodic_orchestrator.list_schedules()


@router.post("/api/orchestration/tick")
def trigger_orchestration_tick(input: OrchestrationTickInput) -> dict:
    periodic_orchestrator.ensure_defaults()
    return periodic_orchestrator.tick(
        as_of=input.as_of.isoformat() if input.as_of else None,
        max_decisions=input.max_decisions,
    ).as_dict()


@router.put("/api/brands/{slug}/orchestration/schedules/{schedule_key:path}")
def set_brand_schedule(slug: str, schedule_key: str, input: ScheduleControlInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    schedule = next(
        (item for item in periodic_orchestrator.list_schedules(brand_id=brand["id"])
         if item["schedule_key"] == schedule_key), None,
    )
    if schedule is None:
        raise HTTPException(status_code=404, detail="Brand schedule not found")
    try:
        return periodic_orchestrator.set_schedule_enabled(
            schedule_key, input.enabled, brand_id=brand["id"],
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Brand schedule not found") from error


@router.post("/api/brands/{slug}/orchestration/tick")
def trigger_brand_orchestration_tick(slug: str, input: OrchestrationTickInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    periodic_orchestrator.ensure_defaults()
    return periodic_orchestrator.tick(
        as_of=input.as_of.isoformat() if input.as_of else None,
        max_decisions=input.max_decisions, brand_id=brand["id"],
    ).as_dict()
