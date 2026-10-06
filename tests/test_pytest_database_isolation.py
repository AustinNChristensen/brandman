"""Black-box proof that a pytest session cannot touch its operating input DB."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _counts(path: Path) -> tuple[int, int]:
    with sqlite3.connect(path) as connection:
        metadata = connection.execute("SELECT COUNT(*) FROM database_metadata").fetchone()[0]
        sentinels = connection.execute("SELECT COUNT(*) FROM operating_sentinel").fetchone()[0]
    return metadata, sentinels


def test_full_pytest_collection_and_run_preserve_operating_database(tmp_path):
    """Run the suite in a child process with an operating sentinel as input.

    The isolation regression excludes itself from the child run to avoid
    recursion. Both full collection and the remaining full test run must leave
    the sentinel byte-for-byte and logically unchanged.
    """
    sentinel = tmp_path / "operating-sentinel.db"
    with sqlite3.connect(sentinel) as connection:
        connection.executescript(
            """
            CREATE TABLE database_metadata (
              key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO database_metadata VALUES ('profile','operating','2026-09-02T00:00:00Z');
            CREATE TABLE operating_sentinel (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO operating_sentinel VALUES (1,'must remain unchanged');
            """
        )
    before = (_digest(sentinel), _counts(sentinel))
    project = Path(__file__).parents[1]
    environment = os.environ.copy()
    environment["BRAND_OS_DB"] = str(sentinel)
    environment["BRAND_OS_DATABASE_PROFILE"] = "operating"
    environment.pop("PYTEST_ADDOPTS", None)
    commands = (
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests"],
        [
            sys.executable, "-m", "pytest", "-q", "tests",
            "--ignore=tests/test_pytest_database_isolation.py",
        ],
    )
    for command in commands:
        result = subprocess.run(
            command, cwd=project, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180,
        )
        assert result.returncode == 0, result.stdout
        assert (_digest(sentinel), _counts(sentinel)) == before
