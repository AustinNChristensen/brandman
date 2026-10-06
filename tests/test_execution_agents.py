from datetime import UTC, datetime

import pytest

from app.execution_agents import ExecutionAgentRegistry


NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)


def test_agent_requires_configuration_before_heartbeat(tmp_path):
    registry = ExecutionAgentRegistry(tmp_path / "agents.db", clock=lambda: NOW)
    with pytest.raises(KeyError):
        registry.heartbeat("brand", "codex")
    configured = registry.configure("brand", "codex", "browser")
    assert configured["last_heartbeat_at"] is None
    live = registry.heartbeat("brand", "codex")
    assert live["channel"] == "browser"
    assert live["last_heartbeat_at"] == NOW.isoformat()
    assert "credential" not in str(live).lower()
