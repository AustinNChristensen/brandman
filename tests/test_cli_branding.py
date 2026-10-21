"""Primary brandman-* commands identify as BrandMan on caught errors (AUS-14)."""
import importlib
import tomllib
import warnings
from pathlib import Path

import pytest

from brandman import warn_if_legacy_command
from brandman import cli

CASES = [
    ("brandman.bootstrap_cli", "brandman-bootstrap", ["--execution-agent", "demo"],
     "execution_agent and execution_channel must be provided together"),
    ("brandman.ops_cli", "brandman-ops", ["--database", "/nonexistent/aus14.db", "audit"],
     "database file does not exist"),
    ("brandman.api_supervisor", "brandman-api-supervisor", ["status", "--label", "bad"],
     "invalid managed API label"),
    ("brandman.launchd_supervisor", "brandman-supervisor", ["status", "--label", "bad"],
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


def test_legacy_command_name_warns(capsys):
    with pytest.warns(DeprecationWarning, match="brand-os-ops is deprecated"):
        warn_if_legacy_command("/usr/local/bin/brand-os-ops")
    assert "use brandman-ops instead" in capsys.readouterr().err


def test_legacy_windows_command_name_warns(capsys):
    with pytest.warns(DeprecationWarning, match="brand-os-mcp is deprecated"):
        warn_if_legacy_command("/usr/local/bin/brand-os-mcp.exe")
    assert "use brandman-mcp instead" in capsys.readouterr().err


def test_canonical_command_name_is_silent(capsys):
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        warn_if_legacy_command("/usr/local/bin/brandman-ops")
    assert capsys.readouterr().err == ""


def test_brandman_help_states_open_core_and_deprecated_aliases(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    assert raised.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "AGPL-3.0" in text
    assert "BrandMan Cloud" in text
    assert "usebrandman.com" in text
    assert "brand-os-*" in text
    assert "BRANDMAN_*" in text
