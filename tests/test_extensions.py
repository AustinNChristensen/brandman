"""Host extension points: authenticator, per-request database, plugins."""
from pathlib import Path
import types

from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse
import pytest

from brandman import extensions, store
from brandman.main import app
from brandman.feedback import FeedbackStore


@pytest.fixture
def two_workspaces(tmp_path):
    databases = {"alpha": tmp_path / "alpha.db", "beta": tmp_path / "beta.db"}
    for path in databases.values():
        with store.using_database(path):
            store.init_db(profile="test")

    def authenticator(request):
        workspace = request.headers.get("x-workspace")
        if request.url.path == "/health":
            return extensions.Authentication(principal=None)
        if workspace not in databases:
            return JSONResponse({"detail": "no workspace"}, status_code=401)
        return extensions.Authentication(
            principal=f"{workspace}@example.test", privileged=workspace == "alpha",
            database=databases[workspace],
        )

    extensions.set_authenticator(authenticator)
    try:
        yield databases
    finally:
        extensions.set_authenticator(None)


def test_each_request_reads_and_writes_only_its_own_database(two_workspaces):
    with TestClient(app) as client:
        assert client.get("/api/brands").status_code == 401
        created = client.post("/api/brands", headers={"x-workspace": "alpha"}, json={
            "slug": "alpha-only", "name": "Alpha only", "mission": "m", "voice": "v",
            "compliance_rules": "c",
        })
        assert created.status_code == 201
        alpha = {b["slug"] for b in client.get("/api/brands", headers={"x-workspace": "alpha"}).json()}
        beta = {b["slug"] for b in client.get("/api/brands", headers={"x-workspace": "beta"}).json()}
    assert "alpha-only" in alpha and "alpha-only" not in beta
    with store.using_database(two_workspaces["beta"]):
        assert store.get_brand("alpha-only") is None
    # The process-wide binding is untouched by request-scoped databases.
    assert store.database_override() is None


def test_public_authentication_skips_the_password_gate(two_workspaces, monkeypatch):
    monkeypatch.delenv("BRANDMAN_PREVIEW_PASSWORD", raising=False)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_privileged_flag_reaches_owner_only_checks(two_workspaces):
    from brandman.principals import is_privileged
    seen = {}

    @app.get("/__test/privileged")
    def probe(request: Request) -> dict:
        seen["result"] = is_privileged(request.state.principal)
        return seen

    try:
        with TestClient(app) as client:
            client.get("/__test/privileged", headers={"x-workspace": "alpha"})
            assert seen["result"] is True
            client.get("/__test/privileged", headers={"x-workspace": "beta"})
            assert seen["result"] is False
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/__test/privileged"]


def test_plugins_register_through_entry_points(monkeypatch):
    calls = []
    plugin = types.SimpleNamespace(register_app=lambda subject: calls.append(subject))
    entry = types.SimpleNamespace(name="fake", load=lambda: plugin)
    monkeypatch.setattr(extensions, "entry_points", lambda group: [entry] if group == "brandman.plugins" else [])
    monkeypatch.setattr(extensions, "_loaded_plugins", {})
    assert extensions.load_plugins("app", "subject") == ["fake"]
    assert extensions.load_plugins("mcp", "subject") == []
    assert calls == ["subject"]
    monkeypatch.setenv("BRANDMAN_DISABLE_PLUGINS", "1")
    assert extensions.load_plugins("app", "subject") == []


def test_worker_runs_against_the_bound_database(tmp_path, monkeypatch):
    from brandman.worker_cli import run_once
    monkeypatch.delenv("BRANDMAN_CREDENTIAL_MASTER_KEY", raising=False)
    database = tmp_path / "worker.db"
    with store.using_database(database):
        store.init_db(profile="test")
        result = run_once()
    assert result["supervision"]["database"] == str(database.resolve())
    assert store.DATA_PATH != database
