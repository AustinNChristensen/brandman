"""Persistence and generation seam for daily mission operating artifacts."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime
from hashlib import sha256
import json
import sqlite3
from typing import Any

from app.mission_ops import build_end_of_day_scorecard, build_morning_plan


MORNING_PLAN = "morning_plan"
END_OF_DAY_SCORECARD = "end_of_day_scorecard"
ARTIFACT_KINDS = frozenset({MORNING_PLAN, END_OF_DAY_SCORECARD})

ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]
ProgressLoader = Callable[[str, str | None], Mapping[str, Any] | None]


SCHEMA = """
CREATE TABLE IF NOT EXISTS mission_daily_artifacts (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL,
  artifact_date TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(mission_id, artifact_date, kind)
);
CREATE INDEX IF NOT EXISTS idx_mission_daily_artifacts_lookup
  ON mission_daily_artifacts(mission_id, kind, artifact_date DESC);
"""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_date(value: str | date | datetime) -> str:
    candidate = value.date().isoformat() if isinstance(value, datetime) else value.isoformat() if isinstance(value, date) else str(value)
    try:
        parsed = date.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("artifact_date must be an ISO date (YYYY-MM-DD)") from exc
    if candidate != parsed.isoformat():
        raise ValueError("artifact_date must be an ISO date (YYYY-MM-DD)")
    return candidate


def _canonical_payload(payload: Mapping[str, Any]) -> str:
    try:
        return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must be JSON-persistable") from exc


def _artifact_id(mission_id: str, artifact_date: str, kind: str) -> str:
    digest = sha256(f"{mission_id}\0{artifact_date}\0{kind}".encode()).hexdigest()[:24]
    return f"mission-artifact-{digest}"


def _decode(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    result["payload"] = json.loads(result["payload"])
    return result


class MissionArtifactStore:
    """SQLite repository that can use BrandMan or an isolated connection seam."""

    def __init__(self, connection_factory: ConnectionFactory, *, clock: Callable[[], str] = _utc_now) -> None:
        self.connection_factory = connection_factory
        self.clock = clock

    def init_schema(self) -> None:
        """Create additive schema objects without modifying existing tables or data."""

        with self.connection_factory() as conn:
            conn.executescript(SCHEMA)

    def upsert(
        self, mission_id: str, artifact_date: str | date | datetime, kind: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        day = _canonical_date(artifact_date)
        if kind not in ARTIFACT_KINDS:
            raise ValueError(f"kind must be one of: {', '.join(sorted(ARTIFACT_KINDS))}")
        if not str(mission_id).strip():
            raise ValueError("mission_id is required")
        encoded = _canonical_payload(payload)
        timestamp = self.clock()
        record = {
            "id": _artifact_id(str(mission_id), day, kind),
            "mission_id": str(mission_id),
            "artifact_date": day,
            "kind": kind,
            "payload": encoded,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        with self.connection_factory() as conn:
            conn.execute(
                """INSERT INTO mission_daily_artifacts
                   (id,mission_id,artifact_date,kind,payload,created_at,updated_at)
                   VALUES (:id,:mission_id,:artifact_date,:kind,:payload,:created_at,:updated_at)
                   ON CONFLICT(mission_id,artifact_date,kind) DO UPDATE SET
                     payload=excluded.payload, updated_at=excluded.updated_at""",
                record,
            )
            found = conn.execute(
                """SELECT * FROM mission_daily_artifacts
                   WHERE mission_id=? AND artifact_date=? AND kind=?""",
                (mission_id, day, kind),
            ).fetchone()
        decoded = _decode(found)
        assert decoded is not None
        return decoded

    def get(self, mission_id: str, artifact_date: str | date | datetime, kind: str) -> dict[str, Any] | None:
        day = _canonical_date(artifact_date)
        with self.connection_factory() as conn:
            found = conn.execute(
                """SELECT * FROM mission_daily_artifacts
                   WHERE mission_id=? AND artifact_date=? AND kind=?""",
                (mission_id, day, kind),
            ).fetchone()
        return _decode(found)

    def list(
        self, mission_id: str, *, kind: str | None = None,
        start_date: str | date | datetime | None = None,
        end_date: str | date | datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if kind is not None and kind not in ARTIFACT_KINDS:
            raise ValueError(f"kind must be one of: {', '.join(sorted(ARTIFACT_KINDS))}")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        clauses = ["mission_id=?"]
        params: list[Any] = [mission_id]
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if start_date is not None:
            clauses.append("artifact_date>=?")
            params.append(_canonical_date(start_date))
        if end_date is not None:
            clauses.append("artifact_date<=?")
            params.append(_canonical_date(end_date))
        params.append(limit)
        query = (
            "SELECT * FROM mission_daily_artifacts WHERE " + " AND ".join(clauses)
            + " ORDER BY artifact_date DESC, kind ASC LIMIT ?"
        )
        with self.connection_factory() as conn:
            found = conn.execute(query, params).fetchall()
        return [_decode(row) for row in found if row is not None]  # type: ignore[misc]

    def previous_scorecard(
        self, mission_id: str, before_date: str | date | datetime,
    ) -> dict[str, Any] | None:
        day = _canonical_date(before_date)
        with self.connection_factory() as conn:
            found = conn.execute(
                """SELECT * FROM mission_daily_artifacts
                   WHERE mission_id=? AND kind=? AND artifact_date<?
                   ORDER BY artifact_date DESC LIMIT 1""",
                (mission_id, END_OF_DAY_SCORECARD, day),
            ).fetchone()
        return _decode(found)


class MissionArtifactService:
    """Compose progress loading, pure builders, and idempotent persistence."""

    def __init__(self, repository: MissionArtifactStore, progress_loader: ProgressLoader) -> None:
        self.repository = repository
        self.progress_loader = progress_loader

    def _progress(self, mission_id: str, as_of: str | None) -> Mapping[str, Any]:
        progress = self.progress_loader(mission_id, as_of)
        if progress is None:
            raise LookupError(f"mission not found: {mission_id}")
        if str(progress.get("id")) != str(mission_id):
            raise ValueError("progress loader returned a different mission")
        return progress

    def create_morning_plan(
        self, mission_id: str, artifact_date: str | date | datetime, *, as_of: str | None = None,
    ) -> dict[str, Any]:
        day = _canonical_date(artifact_date)
        payload = build_morning_plan(self._progress(mission_id, as_of), plan_date=day)
        return self.repository.upsert(mission_id, day, MORNING_PLAN, payload)

    def create_end_of_day_scorecard(
        self, mission_id: str, artifact_date: str | date | datetime, *, as_of: str | None = None,
    ) -> dict[str, Any]:
        day = _canonical_date(artifact_date)
        previous = self.repository.previous_scorecard(mission_id, day)
        previous_progress = previous["payload"] if previous else None
        payload = build_end_of_day_scorecard(
            self._progress(mission_id, as_of),
            previous_progress=previous_progress,
            scorecard_date=day,
        )
        return self.repository.upsert(mission_id, day, END_OF_DAY_SCORECARD, payload)
