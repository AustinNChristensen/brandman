"""Missions, KPIs, tracked links, learnings and experiments."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request

from brandman import store
from brandman.attribution_store import AttributionStoreError
from brandman.beehiiv_assisted_sync import BeehiivAssistedSyncError, ingest_beehiiv_measurements
from brandman.experiments import ExperimentError
from brandman.learning_engine import BrandLearningEngine, LearningError
from brandman.operational_feedback import report_stale_metric
from brandman.performance_planning import PerformancePlanningEngine

from brandman.main import (
    BeehiivMeasurementInput,
    ExperimentDraftInput,
    KpiSnapshotInput,
    LearningInput,
    LearningLifecycleInput,
    PerformanceInput,
    TrackedUrlInput,
    _brand_experiment,
    _brand_learning,
    attribution_store,
    experiment_store,
    operating_plan_service,
)

router = APIRouter()


@router.post("/api/brands/{slug}/measurements/beehiiv/assisted")
def sync_assisted_beehiiv_measurements(
    slug: str, input: BeehiivMeasurementInput,
) -> dict:
    """Ingest aggregate stats fetched by an authorized Beehiiv helper; no PII."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return ingest_beehiiv_measurements(
            store.DATA_PATH, brand_id=brand["id"], posts=input.posts,
            publication_stats=input.publication_stats,
            observed_at=input.observed_at.isoformat(),
        )
    except BeehiivAssistedSyncError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/brands/{slug}/performance", status_code=201)
def record_performance(slug: str, input: PerformanceInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return store.insert("performance_records", {"brand_id": brand["id"], **input.model_dump(mode="json")})


@router.get("/api/brands/{slug}/performance")
def list_performance(slug: str) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return store.rows("""SELECT * FROM performance_records WHERE brand_id=? AND NOT EXISTS (
        SELECT 1 FROM fixture_quarantine_registry q
        WHERE q.table_name='performance_records'
          AND q.record_key_json=json_array(performance_records.id))
        ORDER BY observed_at DESC""", (brand["id"],))


@router.get("/api/brands/{slug}/performance-planning")
def get_performance_planning(
    slug: str, stage: str = "portfolio", channel: str = "x",
    topic: str | None = None, template_key: str | None = None,
    as_of: datetime | None = None,
) -> dict:
    """Explain the bounded measured-performance prior; never mutate planned work."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    scope = {"stage": stage, "channel": channel, "topic": topic, "template_key": template_key}
    return PerformancePlanningEngine(store.DATA_PATH).plan(
        brand["id"], scope, as_of=as_of,
    )


@router.get("/api/brands/{slug}/performance-planning/audit")
def list_performance_planning_audit(slug: str, limit: int = 50) -> list[dict]:
    """Read the tenant-scoped trail of performance evidence used during planning."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return PerformancePlanningEngine(store.DATA_PATH).audit(brand["id"], limit=limit)


@router.get("/api/brands/{slug}/mission")
def get_active_mission(slug: str) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    mission = store.row(
        "SELECT id FROM missions WHERE brand_id=? AND status='active' ORDER BY starts_at DESC LIMIT 1",
        (brand["id"],),
    )
    if not mission:
        raise HTTPException(status_code=404, detail="No active mission")
    return store.mission_progress(mission["id"]) or {}


@router.get("/api/brands/{slug}/mission/morning-plan")
def get_morning_plan(slug: str) -> dict:
    progress = get_active_mission(slug)
    artifact = operating_plan_service.create_morning_plan(
        progress["id"], datetime.now().date().isoformat(), as_of=progress["as_of"],
    )
    return {**artifact["payload"], "artifact_id": artifact["id"], "persisted_at": artifact["updated_at"]}


@router.get("/api/brands/{slug}/mission/scorecard")
def get_mission_scorecard(slug: str) -> dict:
    progress = get_active_mission(slug)
    artifact = operating_plan_service.create_eod_scorecard(
        progress["id"], datetime.now().date().isoformat(), as_of=progress["as_of"],
    )
    return {**artifact["payload"], "artifact_id": artifact["id"], "persisted_at": artifact["updated_at"]}


@router.post("/api/brands/{slug}/tracked-url")
def create_tracked_url(slug: str, input: TrackedUrlInput, request: Request) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        link = attribution_store.create_tracked_link(
            brand_id=brand["id"], campaign_id=input.campaign_id,
            artifact_id=input.artifact_id, cta_id=input.cta_id,
            source=input.source, medium=input.medium, destination=input.base_url,
            brand_slug=slug, actor=request.state.principal,
        )
    except AttributionStoreError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {**link, "url": link["tracked_url"]}


@router.get("/api/brands/{slug}/tracked-links")
def list_tracked_links(slug: str) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return attribution_store.list_tracked_links(brand["id"])


@router.post("/api/brands/{slug}/mission/kpis", status_code=201)
def record_mission_kpi(slug: str, input: KpiSnapshotInput, request: Request) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    mission = store.row(
        "SELECT id FROM missions WHERE brand_id=? AND status='active' ORDER BY starts_at DESC LIMIT 1",
        (brand["id"],),
    )
    if not mission:
        raise HTTPException(status_code=404, detail="No active mission")
    try:
        evidence = attribution_store.record_kpi_evidence(
            mission_id=mission["id"], metric=input.metric, value=input.value,
            observed_at=input.observed_at.isoformat(), source=input.source,
            connector_account_id=input.connector_account_id,
            connector_event_id=input.connector_event_id,
            human_manual=input.source == "manual",
            human_verified_by=request.state.principal if input.source == "manual" else None,
            human_verification_note=input.verification_note or None,
            dimensions=input.dimensions,
        )
        canonical = attribution_store.promote_kpi_evidence(evidence["id"], actor=request.state.principal)
    except AttributionStoreError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    store.record_kpi_snapshot(
        mission["id"], input.metric, canonical["value"], canonical["observed_at"],
        canonical["source"], dimensions={"evidence_record_id": evidence["id"]},
    )
    return canonical


@router.post("/api/brands/{slug}/learnings", status_code=201)
def propose_learning(slug: str, input: LearningInput, request: Request) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    supporting = input.evidence_for or [{"summary": input.evidence}]
    try:
        return BrandLearningEngine(store.DATA_PATH).propose(
            brand["id"], hypothesis=input.hypothesis, proposed_change=input.proposed_change,
            evidence_for=supporting, evidence_against=input.evidence_against,
            effect=input.effect, uncertainty=input.uncertainty, scope=input.scope,
            review_at=input.review_at, actor=request.state.principal,
        )
    except LearningError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/brands/{slug}/learnings")
def list_learnings(slug: str) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return BrandLearningEngine(store.DATA_PATH).list(brand["id"])


@router.get("/api/brands/{slug}/learnings/{learning_id}/audit")
def get_brand_learning_audit(slug: str, learning_id: str) -> list[dict]:
    _, engine = _brand_learning(slug, learning_id)
    return engine.audit(learning_id)


@router.post("/api/brands/{slug}/learnings/{learning_id}/{transition}")
def transition_brand_learning(
    slug: str, learning_id: str,
    transition: Literal["testing", "accept", "reject", "supersede", "disable", "enable"],
    request: Request, input: LearningLifecycleInput | None = None,
) -> dict:
    _, engine = _brand_learning(slug, learning_id)
    try:
        if transition in {"disable", "enable"}:
            return engine.set_active(
                learning_id, transition == "enable", actor=request.state.principal,
                reason=(input.reason if input and input.reason else f"{transition.title()}d by reviewer"),
            )
        target = {"accept": "accepted", "reject": "rejected", "supersede": "superseded"}.get(
            transition, transition,
        )
        return engine.transition(learning_id, target, actor=request.state.principal)
    except LearningError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/brands/{slug}/experiments", status_code=201)
def draft_experiment(slug: str, input: ExperimentDraftInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return experiment_store.draft(
            brand_id=brand["id"], campaign_id=input.campaign_id,
            hypothesis=input.hypothesis, metric=input.metric, guardrails=input.guardrails,
            measurement_windows=input.measurement_windows,
        )
    except ExperimentError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/brands/{slug}/experiments")
def list_experiments(slug: str) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return experiment_store.list(brand["id"])


@router.get("/api/brands/{slug}/experiments/{experiment_id}")
def get_brand_experiment(slug: str, experiment_id: str) -> dict:
    return _brand_experiment(slug, experiment_id)


@router.post("/api/brands/{slug}/experiments/{experiment_id}/recommendations", status_code=201)
def recommend_brand_experiment(slug: str, experiment_id: str) -> dict:
    _brand_experiment(slug, experiment_id)
    try:
        return experiment_store.recommend(experiment_id)
    except ExperimentError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/brands/{slug}/experiments/{experiment_id}/recommendations/{recommendation_id}/accept")
def accept_brand_experiment_recommendation(
    slug: str, experiment_id: str, recommendation_id: str, request: Request,
) -> dict:
    _brand_experiment(slug, experiment_id)
    try:
        return experiment_store.accept(
            experiment_id, recommendation_id, actor=request.state.principal,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ExperimentError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/experiments/{experiment_id}")
def get_experiment(experiment_id: str) -> dict:
    try:
        return experiment_store.get(experiment_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Experiment not found") from error


@router.get("/api/experiments/{experiment_id}/measurement-windows")
def list_experiment_measurement_windows(experiment_id: str) -> list[dict]:
    try:
        experiment_store.get(experiment_id)
        return experiment_store.list_windows(experiment_id=experiment_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Experiment not found") from error


@router.get("/api/experiment-measurement-windows/{window_id}")
def get_experiment_measurement_window(window_id: str) -> dict:
    try:
        return experiment_store.get_window(window_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Experiment measurement window not found") from error


@router.post("/api/experiments/{experiment_id}/recommendations", status_code=201)
def recommend_experiment_winner(experiment_id: str) -> dict:
    try:
        recommendation = experiment_store.recommend(experiment_id)
        if recommendation["status"] == "insufficient_evidence":
            experiment = experiment_store.get(experiment_id)
            report_stale_metric(
                brand_id=experiment["brand_id"], metric=experiment["metric"],
                evidence_window="experiment-review",
                related_ids=[experiment_id, *[variant["post_id"] for variant in experiment["variants"]]],
            )
        return recommendation
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Experiment not found") from error
    except ExperimentError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/experiments/{experiment_id}/recommendations/{recommendation_id}/accept")
def accept_experiment_winner(
    experiment_id: str, recommendation_id: str, request: Request,
) -> dict:
    try:
        return experiment_store.accept(
            experiment_id, recommendation_id, actor=request.state.principal,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Experiment not found") from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ExperimentError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/learnings/{learning_id}/{transition}")
def transition_learning(learning_id: str, transition: Literal["testing", "accept", "reject", "supersede", "disable", "enable"],
                        request: Request, input: LearningLifecycleInput | None = None) -> dict:
    if transition in {"disable", "enable"}:
        try:
            return BrandLearningEngine(store.DATA_PATH).set_active(
                learning_id, transition == "enable", actor=request.state.principal,
                reason=(input.reason if input and input.reason else f"{transition.title()}d by reviewer"),
            )
        except (KeyError, LearningError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
    target = {"accept": "accepted", "reject": "rejected", "supersede": "superseded"}.get(transition, transition)
    try:
        return BrandLearningEngine(store.DATA_PATH).transition(
            learning_id, target, actor=request.state.principal,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Learning not found") from error
    except LearningError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
