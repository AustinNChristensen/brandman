import json

from brandman.mission_artifacts import MissionArtifactStore
from brandman.operating_plan import (
    OperatingPlanService,
    build_operating_eod_scorecard,
    build_operating_morning_plan,
)


def progress(current_x=10, current_subscribers=14):
    return {
        "id": "mission-1", "brand_id": "brand-1", "name": "30-day growth",
        "as_of": "2026-09-02T12:00:00Z", "remaining_days": 29,
        "goals": [
            {"metric": "x_followers", "current": current_x, "expected_current": 11,
             "target": 100, "baseline": 8, "direction": "increase"},
            {"metric": "active_beehiiv_subscribers", "current": current_subscribers,
             "expected_current": 13.4, "target": 25, "baseline": 13, "direction": "increase"},
        ],
    }


INPUTS = {
    "scheduled_work": [
        {"id": "post-draft", "campaign_id": "campaign-1", "channel": "x", "status": "awaiting_approval", "scheduled_for": "2026-09-02T16:00:00Z"},
        {"id": "post-done", "campaign_id": "campaign-0", "channel": "x", "status": "published"},
    ],
    "approvals": [{"id": "approval-1", "artifact_id": "post-draft", "campaign_id": "campaign-1", "channel": "x", "revision": 2}],
    "connectors": [{"id": "connector-x", "connector_type": "x", "status": "reconnect_required", "last_error": "token expired"}],
    "performance": [{"id": "perf-1", "post_id": "post-old", "campaign_id": "campaign-0", "channel": "x", "clicks": 9, "engagements": 20, "conversions": 1}],
    "candidates": [{"id": "candidate-1", "title": "A new transfer bonus", "score": 92, "supporting_sources": [{"source_id": "source-1"}]}],
    "gaps": [{"id": "gap-1", "severity": "high", "component": "x.auth", "summary": "Reconnect flow unclear", "actual_behavior": "No useful prompt", "related_ids": ["connector-x"]}],
    "failures": [{"id": "job-1", "job_type": "x.metrics.sync", "connector_account_id": "connector-x", "last_error": "unauthorized"}],
}


def test_morning_plan_prioritizes_blockers_and_has_actionable_fields():
    plan = build_operating_morning_plan(progress(), plan_date="2026-09-02", **INPUTS)
    assert plan["schema_version"] == 2
    assert plan["actions"][0]["type"] == "restore_connector"
    assert plan["actions"][0]["related_ids"] == ["connector-x"]
    assert plan["actions"][0]["blocker"] == "token expired"
    assert plan["actions"][0]["expected_kpi_contribution"]["metric"] == "x_followers"
    assert any(action["type"] == "review_approval" and action["owner"] == "Chris" for action in plan["actions"])
    assert not any(action["type"] == "prepare_scheduled_work" for action in plan["actions"])
    assert any(action["type"] == "develop_editorial_candidate" and "source-1" in action["related_ids"] for action in plan["actions"])
    assert [action["rank"] for action in plan["actions"]] == list(range(1, len(plan["actions"]) + 1))
    assert plan["context_snapshot"] == {
        "scheduled_work": 2, "approvals_waiting": 1, "unhealthy_connectors": 1,
        "editorial_candidates": 1, "high_severity_gaps": 1, "job_failures": 1,
        "recent_performance_records": 1,
    }
    json.dumps(plan, allow_nan=False)


def test_plan_is_deterministic_and_does_not_invent_related_ids():
    first = build_operating_morning_plan(progress(), plan_date="2026-09-02", **INPUTS)
    second = build_operating_morning_plan(progress(), plan_date="2026-09-02", **INPUTS)
    assert first == second
    allowed = {"post-draft", "post-done", "campaign-1", "campaign-0", "approval-1", "connector-x", "perf-1", "post-old", "candidate-1", "source-1", "gap-1", "job-1"}
    assert {value for action in first["actions"] for value in action["related_ids"]} <= allowed


def test_terminal_scheduled_items_never_become_morning_actions():
    terminal = [
        {"id": status, "channel": "x", "status": status,
         "scheduled_for": "2026-09-02T16:00:00Z"}
        for status in ("published", "completed", "measured", "rejected", "cancelled",
                       "stale", "archived", "abandoned")
    ]
    plan = build_operating_morning_plan(
        progress(), plan_date="2026-09-02",
        **{**INPUTS, "scheduled_work": terminal, "approvals": []},
    )
    assert not any(action["type"] == "prepare_scheduled_work" for action in plan["actions"])


def test_recurring_connector_failures_are_grouped_without_crowding_editorial_work():
    failures = [
        {"id": f"job-{index}", "job_type": "connector.sync",
         "connector_account_id": "rss-1", "last_error": "rss DNS resolution failed"}
        for index in range(20)
    ]
    plan = build_operating_morning_plan(
        progress(), plan_date="2026-09-02",
        **{**INPUTS, "connectors": [], "gaps": [], "approvals": [],
           "scheduled_work": [], "failures": failures},
    )
    incidents = [item for item in plan["actions"] if item["type"] == "resolve_job_failure"]
    assert len(incidents) == 1
    assert incidents[0]["incident_occurrence_count"] == 20
    assert {f"job-{index}" for index in range(20)} <= set(incidents[0]["related_ids"])
    assert any(item["type"] == "develop_editorial_candidate" for item in plan["actions"])


def test_assisted_mode_ignores_optional_native_api_failures_and_surfaces_handoff_need():
    inputs = {**INPUTS, "connectors": [
        {"id": "x-api", "connector_type": "x", "status": "disconnected",
         "required_for_operating_plan": False, "execution_mode": "api"},
        {"id": "beehiiv-api", "connector_type": "beehiiv", "status": "disconnected",
         "required_for_operating_plan": False, "execution_mode": "api"},
        {"id": "website", "connector_type": "website", "status": "disconnected",
         "required_for_operating_plan": False, "execution_mode": "api"},
        {"id": "assisted-execution", "connector_type": "assisted_execution",
         "status": "needs_attention", "required_for_operating_plan": True,
         "execution_mode": "assisted",
         "detail": "start or sign in to the browser/MCP execution helper and record an approval-bound receipt"},
    ]}

    plan = build_operating_morning_plan(progress(), plan_date="2026-09-02", **inputs)

    restore = [action for action in plan["actions"] if action["type"] == "restore_connector"]
    assert [action["related_ids"] for action in restore] == [["assisted-execution"]]
    assert "browser/MCP execution helper" in restore[0]["blocker"]
    assert plan["context_snapshot"]["unhealthy_connectors"] == 1


def test_eod_scorecard_includes_completed_failures_waiting_and_kpi_change():
    previous = {"goals": [
        {"metric": "x_followers", "current": 8},
        {"metric": "active_beehiiv_subscribers", "current": 13},
    ]}
    card = build_operating_eod_scorecard(
        progress(), scorecard_date="2026-09-02", previous_progress=previous, **INPUTS
    )
    assert card["schema_version"] == 2
    assert card["goals"][0]["daily_change"] == 2
    assert card["goals"][1]["daily_change"] == 1
    assert card["completed_work"][0]["id"] == "post-done"
    assert card["failures"][0]["id"] == "job-1"
    assert card["approvals_waiting"][0]["id"] == "approval-1"
    assert len(card["next_priorities"]) <= 5
    json.dumps(card, allow_nan=False)


class FakeRepository:
    def mission_progress(self, mission_id, as_of=None): return progress()
    def scheduled_work(self, brand_id, plan_date): return INPUTS["scheduled_work"]
    def approval_queue(self, brand_id): return INPUTS["approvals"]
    def connector_health(self, brand_id): return INPUTS["connectors"]
    def recent_performance(self, brand_id): return INPUTS["performance"]
    def editorial_candidates(self, brand_id): return INPUTS["candidates"]
    def open_gaps(self, brand_id): return INPUTS["gaps"]
    def job_failures(self, brand_id): return INPUTS["failures"]


class DenverRepository(FakeRepository):
    def mission_progress(self, mission_id, as_of=None):
        return {**progress(), "timezone": "America/Denver"}


def test_service_persists_composed_artifacts_idempotently(tmp_path):
    import sqlite3
    from contextlib import contextmanager

    database = tmp_path / "plans.db"

    @contextmanager
    def connection():
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    artifacts = MissionArtifactStore(connection, clock=lambda: "2026-09-02T12:00:00Z")
    artifacts.init_schema()
    service = OperatingPlanService(FakeRepository(), artifacts)
    morning = service.create_morning_plan("mission-1", "2026-09-02")
    repeated = service.create_morning_plan("mission-1", "2026-09-02")
    assert morning["id"] == repeated["id"]
    assert repeated["payload"]["actions"][0]["type"] == "restore_connector"
    eod = service.create_eod_scorecard("mission-1", "2026-09-02")
    assert eod["kind"] == "end_of_day_scorecard"


def test_service_resolves_plan_instant_in_mission_timezone(tmp_path):
    import sqlite3
    from contextlib import contextmanager

    database = tmp_path / "plans-timezone.db"

    @contextmanager
    def connection():
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    artifacts = MissionArtifactStore(connection, clock=lambda: "2026-09-04T03:15:00Z")
    artifacts.init_schema()
    service = OperatingPlanService(DenverRepository(), artifacts)
    before_midnight = service.create_morning_plan("mission-1", "2026-09-04T03:15:00Z")
    after_midnight = service.create_morning_plan("mission-1", "2026-09-04T06:15:00Z")
    assert before_midnight["artifact_date"] == "2026-09-03"
    assert after_midnight["artifact_date"] == "2026-09-04"
