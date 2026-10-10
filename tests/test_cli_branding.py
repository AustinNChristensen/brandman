"""Primary brandman-* commands identify as BrandMan on caught errors (AUS-14)."""
import importlib
import tomllib
from pathlib import Path

import pytest

CASES = [
    ("app.bootstrap_cli", "brandman-bootstrap", ["--execution-agent", "demo"],
     "execution_agent and execution_channel must be provided together"),
    ("app.ops_cli", "brandman-ops", ["--database", "/nonexistent/aus14.db", "audit"],
     "database file does not exist"),
    ("app.api_supervisor", "brandman-api-supervisor", ["status", "--label", "bad"],
     "invalid managed API label"),
    ("app.launchd_supervisor", "brandman-supervisor", ["status", "--label", "bad"],
     "label must match com.brandos.worker."),
]


@pytest.mark.parametrize(("module", "prog", "argv", "diagnostic"), CASES)
def test_primary_command_errors_use_brandman_prefix(module, prog, argv, diagnostic):
    with pytest.raises(SystemExit) as raised:
        importlib.import_module(module).main(argv)
    message = str(raised.value.code)
    assert message.startswith(f"{prog}: ")
    assert diagnostic in message
    assert "brand-os" not in message


def test_legacy_aliases_remain_registered_to_same_entry_points():
    scripts = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )["project"]["scripts"]
    for primary in ("mcp", "worker", "bootstrap", "ops", "supervisor", "api-supervisor"):
        assert scripts[f"brand-os-{primary}"] == scripts[f"brandman-{primary}"]
