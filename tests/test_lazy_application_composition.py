"""Black-box guarantees for inert imports and explicit runtime composition."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


PROJECT = Path(__file__).parents[1]


def _sha256(path: Path) -> str | None:
    # A clean checkout has no local operating database; absence must also be stable.
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _operating_sentinel(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE database_metadata (
              key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO database_metadata VALUES
              ('profile','operating','2026-09-02T00:00:00Z');
            CREATE TABLE operating_sentinel (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO operating_sentinel VALUES (1,'must remain unchanged');
            """
        )


def _run(code: str, *, database: Path, profile: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["BRAND_OS_DB"] = str(database)
    environment["BRAND_OS_DATABASE_PROFILE"] = profile
    return subprocess.run(
        [sys.executable, "-c", code], cwd=PROJECT, env=environment,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
    )


def test_main_and_mcp_imports_are_database_inert(tmp_path):
    sentinel = tmp_path / "operating-sentinel.db"
    _operating_sentinel(sentinel)
    before = _sha256(sentinel)

    result = _run(
        "import brandman.main; import brandman.mcp_server; print('imported')",
        database=sentinel,
        profile="operating",
    )

    assert result.returncode == 0, result.stdout
    assert result.stdout.strip() == "imported"
    assert _sha256(sentinel) == before
    with sqlite3.connect(sentinel) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operating_sentinel").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM database_metadata").fetchone()[0] == 1


def test_startup_initializes_only_explicit_database_and_profile(tmp_path):
    configured = tmp_path / "explicit-test.db"
    unrelated = tmp_path / "unrelated-operating.db"
    _operating_sentinel(unrelated)
    unrelated_before = _sha256(unrelated)
    code = """
import json, sqlite3
from fastapi.testclient import TestClient
from brandman.main import app
from brandman import store
with TestClient(app):
    with sqlite3.connect(store.DATA_PATH) as connection:
        profile = connection.execute(
            "SELECT value FROM database_metadata WHERE key='profile'"
        ).fetchone()[0]
        brands = connection.execute("SELECT COUNT(*) FROM brands").fetchone()[0]
print(json.dumps({"path": str(store.DATA_PATH), "profile": profile, "brands": brands}))
"""

    result = _run(code, database=configured, profile="test")

    assert result.returncode == 0, result.stdout
    observed = json.loads(result.stdout)
    assert Path(observed["path"]) == configured
    assert observed["profile"] == "test"
    assert observed["brands"] == 2
    assert configured.is_file()
    assert _sha256(unrelated) == unrelated_before


def test_mcp_first_tool_initializes_only_explicit_database(tmp_path):
    configured = tmp_path / "explicit-mcp-test.db"
    result = _run(
        "import json; from brandman.mcp_server import list_brands; "
        "print(json.dumps(list_brands()))",
        database=configured,
        profile="test",
    )

    assert result.returncode == 0, result.stdout
    assert {brand["slug"] for brand in json.loads(result.stdout)} == {
        "demo-brand", "demo-personal",
    }
    with sqlite3.connect(configured) as connection:
        assert connection.execute(
            "SELECT value FROM database_metadata WHERE key='profile'"
        ).fetchone()[0] == "test"


def test_startup_rejects_profile_mismatch_before_schema_write(tmp_path):
    sentinel = tmp_path / "operating-sentinel.db"
    _operating_sentinel(sentinel)
    before = _sha256(sentinel)
    code = """
from fastapi.testclient import TestClient
from brandman.main import app
with TestClient(app):
    pass
"""

    result = _run(code, database=sentinel, profile="test")

    assert result.returncode != 0
    assert "already profiled operating" in result.stdout
    assert _sha256(sentinel) == before


def test_new_database_startup_requires_explicit_profile(tmp_path):
    database = tmp_path / "must-not-be-created.db"
    environment = os.environ.copy()
    environment["BRAND_OS_DB"] = str(database)
    environment.pop("BRAND_OS_DATABASE_PROFILE", None)
    result = subprocess.run(
        [
            sys.executable, "-c",
            "from fastapi.testclient import TestClient; "
            "from brandman.main import app; "
            "TestClient(app).__enter__()",
        ],
        cwd=PROJECT, env=environment, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
    )

    assert result.returncode != 0
    assert "BRAND_OS_DATABASE_PROFILE must be explicitly configured" in result.stdout
    assert not database.exists()


def test_default_project_database_is_never_implicitly_initialized():
    operating_database = PROJECT / "brand_os.db"
    before = _sha256(operating_database)
    environment = os.environ.copy()
    environment.pop("BRAND_OS_DB", None)
    environment["BRAND_OS_DATABASE_PROFILE"] = "operating"
    result = subprocess.run(
        [
            sys.executable, "-c",
            "from fastapi.testclient import TestClient; "
            "from brandman.main import app; "
            "TestClient(app).__enter__()",
        ],
        cwd=PROJECT, env=environment, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
    )

    assert result.returncode != 0
    assert "BRAND_OS_DB must be explicitly configured" in result.stdout
    assert _sha256(operating_database) == before
