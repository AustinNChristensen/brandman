from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

import pytest

from brandman.mission_artifacts import (
    END_OF_DAY_SCORECARD,
    MORNING_PLAN,
    MissionArtifactService,
    MissionArtifactStore,
)


def repository(tmp_path: Path) -> MissionArtifactStore:
    path = tmp_path / "artifacts.db"

    @contextmanager
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    repo = MissionArtifactStore(connect, clock=lambda: "2026-09-01T12:00:00+00:00")
    repo.init_schema()
    return repo


def progress(current: float = 20, *, as_of: str = "2026-09-02T23:00:00Z") -> dict:
    return {
        "id": "mission-1",
        "name": "30-day growth",
        "as_of": as_of,
        "remaining_days": 28,
        "goals": [{
            "metric": "x_followers", "baseline": 8, "current": current,
            "expected_current": 14.1333, "target": 100, "direction": "increase",
        }],
    }


def test_schema_initialization_is_migration_safe(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    first = repo.upsert("mission-1", "2026-09-01", MORNING_PLAN, {"version": 1})
    repo.init_schema()
    assert repo.get("mission-1", "2026-09-01", MORNING_PLAN) == first


def test_upsert_is_idempotent_and_payload_is_json(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    first = repo.upsert("mission-1", "2026-09-01", MORNING_PLAN, {"b": 2, "a": 1})
    second = repo.upsert("mission-1", "2026-09-01", MORNING_PLAN, {"a": 3})
    assert second["id"] == first["id"]
    assert second["created_at"] == first["created_at"]
    assert second["payload"] == {"a": 3}
    json.dumps(second)


def test_retrieval_listing_and_previous_scorecard(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    repo.upsert("mission-1", "2026-09-01", END_OF_DAY_SCORECARD, {"day": 1})
    repo.upsert("mission-1", "2026-09-02", MORNING_PLAN, {"day": 2})
    repo.upsert("mission-1", "2026-09-02", END_OF_DAY_SCORECARD, {"day": 2})
    repo.upsert("mission-other", "2026-09-03", END_OF_DAY_SCORECARD, {"day": 3})

    listed = repo.list("mission-1", start_date="2026-09-02")
    assert [(item["artifact_date"], item["kind"]) for item in listed] == [
        ("2026-09-02", END_OF_DAY_SCORECARD), ("2026-09-02", MORNING_PLAN),
    ]
    previous = repo.previous_scorecard("mission-1", "2026-09-03")
    assert previous and previous["payload"] == {"day": 2}
    assert repo.previous_scorecard("mission-1", "2026-09-01") is None


def test_service_composes_builders_and_previous_scorecard(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    observations = {
        "2026-09-01T23:00:00Z": progress(20, as_of="2026-09-01T23:00:00Z"),
        "2026-09-02T08:00:00Z": progress(20, as_of="2026-09-02T08:00:00Z"),
        "2026-09-02T23:00:00Z": progress(24, as_of="2026-09-02T23:00:00Z"),
    }
    calls = []

    def load(mission_id: str, as_of: str | None):
        calls.append((mission_id, as_of))
        return observations[as_of]

    service = MissionArtifactService(repo, load)
    morning = service.create_morning_plan("mission-1", "2026-09-02", as_of="2026-09-02T08:00:00Z")
    assert morning["payload"]["kind"] == MORNING_PLAN
    day_one = service.create_end_of_day_scorecard("mission-1", "2026-09-01", as_of="2026-09-01T23:00:00Z")
    assert day_one["payload"]["goals"][0]["daily_change"] is None
    day_two = service.create_end_of_day_scorecard("mission-1", "2026-09-02", as_of="2026-09-02T23:00:00Z")
    assert day_two["payload"]["goals"][0]["daily_change"] == 4
    assert calls[-1] == ("mission-1", "2026-09-02T23:00:00Z")


def test_validation_and_missing_mission(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    with pytest.raises(ValueError, match="ISO date"):
        repo.upsert("mission-1", "09/01/2026", MORNING_PLAN, {})
    with pytest.raises(ValueError, match="kind"):
        repo.upsert("mission-1", "2026-09-01", "weekly", {})
    service = MissionArtifactService(repo, lambda _mission_id, _as_of: None)
    with pytest.raises(LookupError, match="mission not found"):
        service.create_morning_plan("missing", "2026-09-01")
