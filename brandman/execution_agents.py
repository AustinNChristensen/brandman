"""Non-credential registry and liveness records for assisted execution agents."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from typing import Any, Callable


SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_agents (
  brand_id TEXT NOT NULL, agent_id TEXT NOT NULL, channel TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, configured_at TEXT NOT NULL,
  last_heartbeat_at TEXT, PRIMARY KEY(brand_id,agent_id),
  CHECK(channel IN ('browser','mcp'))
);
"""


class ExecutionAgentRegistry:
    def __init__(self, database: str | Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.database = str(database)
        self.clock = clock or (lambda: datetime.now(UTC))
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def configure(self, brand_id: str, agent_id: str, channel: str, *, enabled: bool = True) -> dict[str, Any]:
        if channel not in {"browser", "mcp"}:
            raise ValueError("channel must be browser or mcp")
        if not agent_id.strip():
            raise ValueError("agent_id is required")
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO execution_agents
                   (brand_id,agent_id,channel,enabled,configured_at,last_heartbeat_at)
                   VALUES (?,?,?,?,?,NULL)
                   ON CONFLICT(brand_id,agent_id) DO UPDATE SET
                     channel=excluded.channel,enabled=excluded.enabled""",
                (brand_id, agent_id.strip(), channel, int(enabled), now),
            )
        return self.get(brand_id, agent_id)

    def heartbeat(self, brand_id: str, agent_id: str) -> dict[str, Any]:
        now = self._now()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE execution_agents SET last_heartbeat_at=?
                   WHERE brand_id=? AND agent_id=? AND enabled=1""",
                (now, brand_id, agent_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("unknown or disabled execution agent")
        return self.get(brand_id, agent_id)

    def get(self, brand_id: str, agent_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_agents WHERE brand_id=? AND agent_id=?",
                (brand_id, agent_id),
            ).fetchone()
        if row is None:
            raise KeyError("unknown execution agent")
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    def list(self, brand_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_agents WHERE brand_id=? ORDER BY agent_id",
                (brand_id,),
            ).fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _now(self) -> str:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("clock must be timezone-aware")
        return value.astimezone(UTC).isoformat()


__all__ = ["ExecutionAgentRegistry"]
