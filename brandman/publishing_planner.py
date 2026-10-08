"""Reversible publishing-plan metadata; never schedules or publishes providers."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA = """
CREATE TABLE IF NOT EXISTS publishing_plan_settings (
  brand_id TEXT PRIMARY KEY, timezone TEXT NOT NULL, windows_json TEXT NOT NULL,
  cadence_json TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publishing_plan_items (
  brand_id TEXT NOT NULL, item_type TEXT NOT NULL, item_id TEXT NOT NULL,
  initiative_id TEXT NOT NULL, planned_for TEXT, pinned INTEGER NOT NULL DEFAULT 0,
  locked INTEGER NOT NULL DEFAULT 0, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(brand_id,item_type,item_id)
);
CREATE TABLE IF NOT EXISTS publishing_reflow_previews (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL, snapshot_fingerprint TEXT NOT NULL,
  proposal_json TEXT NOT NULL, requested_by TEXT NOT NULL, created_at TEXT NOT NULL,
  committed_at TEXT, commit_id TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS publishing_reflow_commits (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL, preview_id TEXT NOT NULL UNIQUE,
  before_json TEXT NOT NULL, after_json TEXT NOT NULL, committed_by TEXT NOT NULL,
  committed_at TEXT NOT NULL, undone_by TEXT, undone_at TEXT
);
"""

DEFAULT_WINDOWS = [
    {"weekday": day, "start": "09:00", "end": "17:00"} for day in range(5)
]
DEFAULT_CADENCE = {"x": 120, "newsletter": 1440}
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")


class PublishingPlannerError(ValueError):
    pass


class PublishingPlanner:
    def __init__(self, database: str | Path, *, clock=None) -> None:
        self.database = str(database)
        self.clock = clock or (lambda: datetime.now(UTC))
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def view(self, brand_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            self._assert_brand(connection, brand_id)
            settings = self._settings(connection, brand_id)
            items = self._items(connection, brand_id)
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            groups.setdefault(item["initiative_id"], []).append(item)
        return {
            "brand_id": brand_id, "settings": settings,
            "initiatives": [
                {"id": key, "items": sorted(value, key=_item_order)}
                for key, value in sorted(groups.items())
            ],
            "safety": {"planning_only": True, "provider_write_performed": False,
                       "approval_granted": False},
        }

    def update_settings(
        self, brand_id: str, *, timezone: str, windows: list[Mapping[str, Any]],
        cadence_minutes: Mapping[str, int], actor: str,
    ) -> dict[str, Any]:
        self._validate_actor(actor)
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise PublishingPlannerError("timezone must be a valid IANA timezone") from exc
        normalized_windows = _validate_windows(windows)
        normalized_cadence = _validate_cadence(cadence_minutes)
        now = self.clock().isoformat()
        with self._connect() as connection:
            self._assert_brand(connection, brand_id)
            connection.execute(
                """INSERT INTO publishing_plan_settings
                   (brand_id,timezone,windows_json,cadence_json,updated_by,updated_at)
                   VALUES (?,?,?,?,?,?) ON CONFLICT(brand_id) DO UPDATE SET
                   timezone=excluded.timezone,windows_json=excluded.windows_json,
                   cadence_json=excluded.cadence_json,updated_by=excluded.updated_by,
                   updated_at=excluded.updated_at""",
                (brand_id, timezone, _json(normalized_windows), _json(normalized_cadence), actor, now),
            )
            return self._settings(connection, brand_id)

    def update_item(
        self, brand_id: str, item_type: str, item_id: str, *, initiative_id: str,
        planned_for: str | None, pinned: bool, locked: bool, actor: str,
    ) -> dict[str, Any]:
        self._validate_actor(actor)
        if item_type not in {"post", "newsletter"}:
            raise PublishingPlannerError("planner item_type must be post or newsletter")
        if not SAFE_ID.fullmatch(initiative_id):
            raise PublishingPlannerError("initiative_id must be a safe identifier")
        planned = _timestamp(planned_for) if planned_for else None
        now = self.clock().isoformat()
        with self._connect() as connection:
            self._assert_item(connection, brand_id, item_type, item_id)
            existing = connection.execute(
                "SELECT * FROM publishing_plan_items WHERE brand_id=? AND item_type=? AND item_id=?",
                (brand_id, item_type, item_id),
            ).fetchone()
            if existing and bool(existing["locked"]) and (
                locked or initiative_id != existing["initiative_id"]
                or planned != existing["planned_for"] or bool(existing["pinned"]) != pinned
            ):
                raise PublishingPlannerError("unlock the planner item before changing it")
            connection.execute(
                """INSERT INTO publishing_plan_items
                   (brand_id,item_type,item_id,initiative_id,planned_for,pinned,locked,updated_by,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(brand_id,item_type,item_id) DO UPDATE SET
                   initiative_id=excluded.initiative_id,planned_for=excluded.planned_for,
                   pinned=excluded.pinned,locked=excluded.locked,updated_by=excluded.updated_by,
                   updated_at=excluded.updated_at""",
                (brand_id, item_type, item_id, initiative_id, planned, int(pinned), int(locked), actor, now),
            )
            return next(item for item in self._items(connection, brand_id)
                        if item["item_type"] == item_type and item["item_id"] == item_id)

    def preview_reflow(self, brand_id: str, *, start_at: str, actor: str) -> dict[str, Any]:
        self._validate_actor(actor)
        start = datetime.fromisoformat(_timestamp(start_at))
        with self._connect() as connection:
            self._assert_brand(connection, brand_id)
            settings = self._settings(connection, brand_id)
            items = self._items(connection, brand_id)
            snapshot = _snapshot(items, settings)
            proposal = self._proposal(items, settings, start)
            preview_id = str(uuid4())
            connection.execute(
                """INSERT INTO publishing_reflow_previews
                   (id,brand_id,snapshot_fingerprint,proposal_json,requested_by,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (preview_id, brand_id, snapshot, _json(proposal), actor, self.clock().isoformat()),
            )
        return {"id": preview_id, "snapshot_fingerprint": snapshot, "changes": proposal,
                "planning_only": True}

    def commit_reflow(self, brand_id: str, preview_id: str, *, actor: str) -> dict[str, Any]:
        self._validate_actor(actor)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM publishing_reflow_previews WHERE id=? AND brand_id=?",
                (preview_id, brand_id),
            ).fetchone()
            if row is None:
                raise KeyError("reflow preview not found")
            if row["committed_at"]:
                commit = connection.execute(
                    "SELECT * FROM publishing_reflow_commits WHERE id=?", (row["commit_id"],),
                ).fetchone()
                return self._decode_commit(commit)
            items = self._items(connection, brand_id)
            settings = self._settings(connection, brand_id)
            if _snapshot(items, settings) != row["snapshot_fingerprint"]:
                raise PublishingPlannerError("publishing plan changed after preview; create a new preview")
            changes = json.loads(row["proposal_json"])
            before = []
            now = self.clock().isoformat()
            for change in changes:
                current = connection.execute(
                    "SELECT * FROM publishing_plan_items WHERE brand_id=? AND item_type=? AND item_id=?",
                    (brand_id, change["item_type"], change["item_id"]),
                ).fetchone()
                before.append(_override(current, change))
                connection.execute(
                    """INSERT INTO publishing_plan_items
                       (brand_id,item_type,item_id,initiative_id,planned_for,pinned,locked,updated_by,updated_at)
                       VALUES (?,?,?,?,?,0,0,?,?) ON CONFLICT(brand_id,item_type,item_id) DO UPDATE SET
                       planned_for=excluded.planned_for,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                    (brand_id, change["item_type"], change["item_id"], change["initiative_id"],
                     change["after"], actor, now),
                )
            commit_id = str(uuid4())
            connection.execute(
                """INSERT INTO publishing_reflow_commits
                   (id,brand_id,preview_id,before_json,after_json,committed_by,committed_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (commit_id, brand_id, preview_id, _json(before), _json(changes), actor, now),
            )
            connection.execute(
                "UPDATE publishing_reflow_previews SET committed_at=?,commit_id=? WHERE id=?",
                (now, commit_id, preview_id),
            )
            return self._decode_commit(connection.execute(
                "SELECT * FROM publishing_reflow_commits WHERE id=?", (commit_id,),
            ).fetchone())

    def undo_reflow(self, brand_id: str, commit_id: str, *, actor: str) -> dict[str, Any]:
        self._validate_actor(actor)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM publishing_reflow_commits WHERE id=? AND brand_id=?",
                (commit_id, brand_id),
            ).fetchone()
            if row is None:
                raise KeyError("reflow commit not found")
            if row["undone_at"]:
                return self._decode_commit(row)
            before, after = json.loads(row["before_json"]), json.loads(row["after_json"])
            for expected in after:
                current = connection.execute(
                    """SELECT initiative_id,planned_for,pinned,locked FROM publishing_plan_items
                       WHERE brand_id=? AND item_type=? AND item_id=?""",
                    (brand_id, expected["item_type"], expected["item_id"]),
                ).fetchone()
                if current is None or (
                    current["initiative_id"] != expected["initiative_id"]
                    or current["planned_for"] != expected["after"]
                    or bool(current["pinned"]) or bool(current["locked"])
                ):
                    raise PublishingPlannerError("publishing plan changed after commit; exact undo is unsafe")
            now = self.clock().isoformat()
            for prior in before:
                if prior["existed"]:
                    connection.execute(
                        """UPDATE publishing_plan_items SET initiative_id=?,planned_for=?,pinned=?,locked=?,
                           updated_by=?,updated_at=? WHERE brand_id=? AND item_type=? AND item_id=?""",
                        (prior["initiative_id"], prior["planned_for"], int(prior["pinned"]),
                         int(prior["locked"]), actor, now, brand_id, prior["item_type"], prior["item_id"]),
                    )
                else:
                    connection.execute(
                        "DELETE FROM publishing_plan_items WHERE brand_id=? AND item_type=? AND item_id=?",
                        (brand_id, prior["item_type"], prior["item_id"]),
                    )
            connection.execute(
                "UPDATE publishing_reflow_commits SET undone_by=?,undone_at=? WHERE id=?",
                (actor, now, commit_id),
            )
            return self._decode_commit(connection.execute(
                "SELECT * FROM publishing_reflow_commits WHERE id=?", (commit_id,),
            ).fetchone())

    def _proposal(self, items: list[dict[str, Any]], settings: dict[str, Any], start: datetime) -> list[dict[str, Any]]:
        zone = ZoneInfo(settings["timezone"])
        cursor = start.astimezone(zone)
        last_by_channel: dict[str, datetime] = {}
        proposal = []
        for item in sorted(items, key=lambda value: (value["initiative_id"], value["channel"], value["item_id"])):
            if item["pinned"] or item["locked"] or item["status"] in {"published", "measured", "archived", "abandoned"}:
                continue
            cadence = timedelta(minutes=settings["cadence_minutes"].get(item["channel"], 0))
            previous = last_by_channel.get(item["channel"])
            channel_cursor = max(cursor, previous + cadence) if previous is not None else cursor
            slot = _next_window(channel_cursor, settings["windows"])
            after = slot.astimezone(UTC).isoformat()
            if item.get("planned_for") != after:
                proposal.append({"item_type": item["item_type"], "item_id": item["item_id"],
                                 "initiative_id": item["initiative_id"], "channel": item["channel"],
                                 "before": item.get("planned_for"), "after": after})
            last_by_channel[item["channel"]] = slot
            cursor = slot
        return proposal

    def _items(self, connection: sqlite3.Connection, brand_id: str) -> list[dict[str, Any]]:
        rows = []
        for row in connection.execute(
            """SELECT 'post' item_type,p.id item_id,p.channel,p.status,p.scheduled_for,
                      COALESCE(p.campaign_id,p.id) default_initiative,p.body title
               FROM posts p JOIN campaigns c ON c.id=p.campaign_id WHERE c.brand_id=?""", (brand_id,),
        ):
            rows.append(dict(row))
        for row in connection.execute(
            """SELECT 'newsletter' item_type,i.id item_id,'newsletter' channel,i.lifecycle status,
                      i.scheduled_for,COALESCE(i.candidate_id,i.id) default_initiative,
                      COALESCE(NULLIF(r.final_title,''),NULLIF(r.subject,''),i.id) title
               FROM newsletter_issues i JOIN newsletter_revisions r
                 ON r.issue_id=i.id AND r.revision=i.current_revision WHERE i.brand_id=?""", (brand_id,),
        ):
            rows.append(dict(row))
        overrides = {(row["item_type"], row["item_id"]): dict(row) for row in connection.execute(
            "SELECT * FROM publishing_plan_items WHERE brand_id=?", (brand_id,),
        )}
        result = []
        for row in rows:
            override = overrides.get((row["item_type"], row["item_id"]))
            result.append({
                "item_type": row["item_type"], "item_id": row["item_id"], "channel": row["channel"],
                "status": row["status"], "title": row["title"],
                "initiative_id": override["initiative_id"] if override else row["default_initiative"],
                "planned_for": override["planned_for"] if override else row["scheduled_for"],
                "pinned": bool(override["pinned"]) if override else False,
                "locked": bool(override["locked"]) if override else False,
            })
        return result

    def _settings(self, connection: sqlite3.Connection, brand_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM publishing_plan_settings WHERE brand_id=?", (brand_id,)).fetchone()
        return {"timezone": row["timezone"] if row else "UTC",
                "windows": json.loads(row["windows_json"]) if row else DEFAULT_WINDOWS,
                "cadence_minutes": json.loads(row["cadence_json"]) if row else DEFAULT_CADENCE,
                "updated_by": row["updated_by"] if row else None, "updated_at": row["updated_at"] if row else None}

    def _assert_brand(self, connection: sqlite3.Connection, brand_id: str) -> None:
        if connection.execute("SELECT 1 FROM brands WHERE id=?", (brand_id,)).fetchone() is None:
            raise KeyError("brand not found")

    def _assert_item(self, connection: sqlite3.Connection, brand_id: str, item_type: str, item_id: str) -> None:
        query = (
            "SELECT 1 FROM posts p JOIN campaigns c ON c.id=p.campaign_id WHERE p.id=? AND c.brand_id=?"
            if item_type == "post" else
            "SELECT 1 FROM newsletter_issues WHERE id=? AND brand_id=?"
        )
        if connection.execute(query, (item_id, brand_id)).fetchone() is None:
            raise KeyError("planner item not found")

    @staticmethod
    def _validate_actor(actor: str) -> None:
        if not SAFE_ID.fullmatch(actor):
            raise PublishingPlannerError("actor must be a safe identifier")

    @staticmethod
    def _decode_commit(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row); result["before"] = json.loads(result.pop("before_json")); result["changes"] = json.loads(result.pop("after_json")); return result

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _validate_windows(windows):
    if not windows:
        raise PublishingPlannerError("at least one publishing window is required")
    result = []
    for window in windows:
        weekday = int(window.get("weekday", -1)); start = str(window.get("start", "")); end = str(window.get("end", ""))
        if weekday not in range(7) or not re.fullmatch(r"\d{2}:\d{2}", start) or not re.fullmatch(r"\d{2}:\d{2}", end):
            raise PublishingPlannerError("publishing windows require weekday 0-6 and HH:MM times")
        try:
            invalid_order = _clock(start) >= _clock(end)
        except ValueError as exc:
            raise PublishingPlannerError("publishing windows require valid HH:MM times") from exc
        if invalid_order:
            raise PublishingPlannerError("publishing window start must be before end")
        result.append({"weekday": weekday, "start": start, "end": end})
    return sorted(result, key=lambda value: (value["weekday"], value["start"], value["end"]))


def _validate_cadence(values):
    result = {}
    for channel, minutes in values.items():
        if not SAFE_ID.fullmatch(str(channel)) or not isinstance(minutes, int) or not 0 <= minutes <= 43200:
            raise PublishingPlannerError("cadence minutes must be integers from 0 to 43200")
        result[str(channel)] = minutes
    return result


def _next_window(cursor: datetime, windows) -> datetime:
    for offset in range(181):
        day = (cursor + timedelta(days=offset)).date()
        for window in windows:
            if day.weekday() != window["weekday"]:
                continue
            start = datetime.combine(day, _clock(window["start"]), cursor.tzinfo)
            end = datetime.combine(day, _clock(window["end"]), cursor.tzinfo)
            candidate = max(cursor, start)
            if candidate < end:
                return candidate
    raise PublishingPlannerError("no publishing slot found within 180 days")


def _clock(value: str) -> time:
    return time.fromisoformat(value)


def _timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise PublishingPlannerError("planned timestamps must include a timezone")
    return parsed.isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _snapshot(items, settings) -> str:
    material = {"settings": settings, "items": sorted(items, key=_item_order)}
    return "sha256:" + sha256(_json(material).encode()).hexdigest()


def _item_order(item):
    return item.get("planned_for") or "", item["item_type"], item["item_id"]


def _override(row, change):
    if row is None:
        return {"item_type": change["item_type"], "item_id": change["item_id"], "existed": False,
                "initiative_id": change["initiative_id"], "planned_for": None, "pinned": False, "locked": False}
    return {"item_type": row["item_type"], "item_id": row["item_id"], "existed": True,
            "initiative_id": row["initiative_id"], "planned_for": row["planned_for"],
            "pinned": bool(row["pinned"]), "locked": bool(row["locked"])}
