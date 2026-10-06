"""Suite-wide database identity guard for scratch fixtures.

This module binds collection to scratch state before application imports and
also installs a path-level backstop. Application composition is import-inert,
but the backstop protects future modules and direct repository constructors
from regressing that guarantee.
"""

import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import unquote

import pytest

_COLLECTION_SCRATCH = Path(tempfile.mkdtemp(prefix="brand-os-pytest-")) / "collection.db"
_PROJECT_DATABASE = Path(__file__).parents[1] / "brand_os.db"
_LEGACY_TEST_DATABASE = Path(__file__).parent / "test_brand_os.db"
_INHERITED_DATABASE = Path(os.environ.get("BRAND_OS_DB", _PROJECT_DATABASE))
_PROTECTED_DATABASES = frozenset(
    path.expanduser().resolve()
    for path in {_PROJECT_DATABASE, _LEGACY_TEST_DATABASE, _INHERITED_DATABASE}
)


def _refuse_protected_sqlite_connection(event, arguments):
    """Make accidental operating-path access impossible inside pytest.

    Python's SQLite audit event occurs before the connection is opened, which
    protects direct repository constructors as well as ``app.store`` helpers.
    The guard is deliberately path-based: tests may create temporary databases
    profiled as operating to test policy, but never access the inherited or
    project operating files.
    """
    if event != "sqlite3.connect" or not arguments:
        return
    database = arguments[0]
    if not isinstance(database, (str, bytes, os.PathLike)):
        return
    value = os.fsdecode(database)
    if value == ":memory:":
        return
    # SQLite URI filenames can otherwise bypass an exact path comparison.
    path_value = unquote(value[5:].split("?", 1)[0]) if value.startswith("file:") else value
    if Path(path_value).expanduser().resolve() in _PROTECTED_DATABASES:
        raise RuntimeError(
            "pytest refuses to open an operating or persistent database path; "
            "use the per-test scratch database"
        )


sys.addaudithook(_refuse_protected_sqlite_connection)
os.environ["BRAND_OS_DB"] = str(_COLLECTION_SCRATCH)
os.environ["BRAND_OS_DATABASE_PROFILE"] = "test"

# This is intentionally the first application import in the pytest process.
from app import store  # noqa: E402

store.DATA_PATH = _COLLECTION_SCRATCH


def pytest_collection_finish(session):
    """Undo any module-level environment assignments made during collection."""
    os.environ["BRAND_OS_DB"] = str(_COLLECTION_SCRATCH)
    os.environ["BRAND_OS_DATABASE_PROFILE"] = "test"
    store.DATA_PATH = _COLLECTION_SCRATCH


@pytest.fixture(autouse=True)
def isolated_test_database(tmp_path, monkeypatch):
    """Bind every test to a fresh, explicitly profiled scratch database.

    This fixture runs before any TestClient lifespan, which is where app.main
    composes its lazy service set. Tests remain free to replace DATA_PATH with
    another tmp_path database, but can never inherit the operating path.
    """
    original = store.DATA_PATH
    scratch = tmp_path / "brand-os-pytest.db"
    store.DATA_PATH = scratch
    monkeypatch.setenv("BRAND_OS_DB", str(scratch))
    monkeypatch.setenv("BRAND_OS_DATABASE_PROFILE", "test")
    yield
    store.DATA_PATH = original


@pytest.fixture
def launch_window_clock(monkeypatch):
    """Pin store.now() inside the seeded September launch mission window.

    The mission now rolls forward with the real clock, so tests whose fixtures use
    fixed September observation dates must pin the clock instead of relying on today.
    """
    monkeypatch.setattr(store, "now", lambda: "2026-09-15T12:00:00+00:00")
