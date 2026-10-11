"""A missing dashboard build fails at startup, not as a 503 on a user request."""
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from brandman import dashboard_build, main
from brandman.dashboard_build import DashboardMissingError, ensure_dashboard


@pytest.fixture
def require(monkeypatch):
    monkeypatch.setenv("BRANDMAN_REQUIRE_DASHBOARD", "true")
    monkeypatch.delenv("BRANDMAN_AUTO_BUILD_DASHBOARD", raising=False)


def _checkout(tmp_path):
    """A source-checkout-shaped tree: <root>/brandman/static/app + web + scripts."""
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "build_dashboard.py").write_text("")
    return tmp_path / "brandman" / "static" / "app"


def test_application_refuses_to_start_without_a_dashboard(require, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DASHBOARD_V2_DIR", tmp_path / "brandman" / "static" / "app")
    with pytest.raises(DashboardMissingError, match="scripts/build_dashboard.py"):
        with TestClient(main.app):
            pass


def test_present_dashboard_starts_without_building(require, monkeypatch, tmp_path):
    app_dir = _checkout(tmp_path)
    app_dir.mkdir(parents=True)
    (app_dir / "index.html").write_text("<html></html>")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not build"))
    ensure_dashboard(app_dir)


def test_source_checkout_builds_on_startup(require, monkeypatch, tmp_path):
    app_dir = _checkout(tmp_path)
    monkeypatch.setattr(dashboard_build.shutil, "which", lambda name: "/usr/bin/npm")

    def fake_build(command, cwd, check):
        assert command == [sys.executable, str(tmp_path / "scripts" / "build_dashboard.py")]
        app_dir.mkdir(parents=True)
        (app_dir / "index.html").write_text("<html></html>")

    monkeypatch.setattr(dashboard_build.subprocess, "run", fake_build)
    ensure_dashboard(app_dir)
    assert (app_dir / "index.html").is_file()


def test_failed_or_unavailable_build_is_a_startup_error(require, monkeypatch, tmp_path):
    app_dir = _checkout(tmp_path)
    monkeypatch.setattr(dashboard_build.shutil, "which", lambda name: None)
    with pytest.raises(DashboardMissingError, match="npm are not installed"):
        ensure_dashboard(app_dir)

    monkeypatch.setattr(dashboard_build.shutil, "which", lambda name: "/usr/bin/npm")

    def broken_build(command, cwd, check):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(dashboard_build.subprocess, "run", broken_build)
    with pytest.raises(DashboardMissingError, match="startup build failed"):
        ensure_dashboard(app_dir)

    monkeypatch.setenv("BRANDMAN_AUTO_BUILD_DASHBOARD", "false")
    with pytest.raises(DashboardMissingError, match="AUTO_BUILD_DASHBOARD is disabled"):
        ensure_dashboard(app_dir)


def test_api_only_opt_out_downgrades_to_a_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("BRANDMAN_REQUIRE_DASHBOARD", "false")
    ensure_dashboard(tmp_path / "brandman" / "static" / "app")
    assert "dashboard build is missing" in caplog.text.lower()
