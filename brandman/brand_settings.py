"""Audited, tenant-scoped editable brand settings."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
import json
import sqlite3
from typing import Any, Callable


SCHEMA = """
CREATE TABLE IF NOT EXISTS brand_settings_audit (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, brand_id TEXT NOT NULL,
  actor TEXT NOT NULL, reason TEXT NOT NULL, before_json TEXT NOT NULL,
  after_json TEXT NOT NULL, at TEXT NOT NULL
);
"""


class BrandSettingsError(ValueError):
    pass


class BrandSettingsStore:
    EDITABLE = ("mission", "voice", "compliance_rules", "approval_policy")

    def __init__(self, database: str | Path, *, clock: Callable[[], str] | None = None) -> None:
        self.database = str(database)
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def update(self, brand_id: str, changes: Mapping[str, Any], *, actor: str, reason: str) -> dict[str, Any]:
        unknown = set(changes) - set(self.EDITABLE)
        if unknown:
            raise BrandSettingsError("unsupported brand settings: " + ", ".join(sorted(unknown)))
        if not changes or not actor.strip() or len(reason.strip()) < 3:
            raise BrandSettingsError("settings, authenticated actor, and a specific reason are required")
        normalized = {key: str(value).strip() for key, value in changes.items()}
        if any(not value for value in normalized.values()):
            raise BrandSettingsError("brand settings cannot be blank")
        if "approval_policy" in normalized and normalized["approval_policy"] not in {
            "human_approval_required", "standing_approval",
        }:
            raise BrandSettingsError("unsupported approval policy")
        timestamp = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
            if row is None:
                raise KeyError("brand not found")
            before = {key: row[key] for key in self.EDITABLE}
            after = {**before, **normalized}
            assignments = ",".join(f"{key}=?" for key in normalized)
            connection.execute(
                f"UPDATE brands SET {assignments},updated_at=? WHERE id=?",
                (*normalized.values(), timestamp, brand_id),
            )
            connection.execute(
                """INSERT INTO brand_settings_audit
                   (brand_id,actor,reason,before_json,after_json,at) VALUES (?,?,?,?,?,?)""",
                (brand_id, actor.strip(), reason.strip(), json.dumps(before, sort_keys=True),
                 json.dumps(after, sort_keys=True), timestamp),
            )
            updated = connection.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
        return dict(updated)

    def audit(self, brand_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM brand_settings_audit WHERE brand_id=? ORDER BY sequence", (brand_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["before"] = json.loads(item.pop("before_json"))
            item["after"] = json.loads(item.pop("after_json"))
            result.append(item)
        return result
