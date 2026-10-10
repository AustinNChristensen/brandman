"""The React dashboard (web/ → app/static/app) is served at /app behind the
same boundary and preview password as everything else."""
import base64
import os
import re
from pathlib import Path

os.environ["BRAND_OS_PREVIEW_PASSWORD"] = "test-only-password"

from fastapi.testclient import TestClient

from app.main import app

BUILD = Path(__file__).parents[1] / "app" / "static" / "app"


def _client() -> TestClient:
    token = base64.b64encode(b"operator:test-only-password").decode()
    return TestClient(app, headers={"Authorization": f"Basic {token}"})


def test_build_is_complete_and_same_origin_only():
    html = (BUILD / "index.html").read_text(encoding="utf-8")
    assert "https://" not in html, "the dashboard must not load anything cross-origin"
    for asset in re.findall(r'(?:src|href)="/app/([^"]+)"', html):
        assert (BUILD / asset).is_file(), f"missing built asset {asset}"


def test_app_routes_serve_entry_and_assets():
    html = (BUILD / "index.html").read_text(encoding="utf-8")
    with _client() as client:
        for path in (
            "/app", "/app/", "/app/approvals", "/app/content/newsletter/abc",
            "/app/execution", "/app/agents", "/app/integrations",
        ):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.text == html
        script = re.search(r'src="/app/(assets/[^"]+\.js)"', html).group(1)
        asset = client.get(f"/app/{script}")
        assert asset.status_code == 200
        assert "javascript" in asset.headers["content-type"]
        missing_asset = client.get("/app/assets/stale-build-reference.js")
        assert missing_asset.status_code == 404
        assert missing_asset.headers["content-type"].startswith("application/json")


def test_app_routes_require_the_preview_password_and_stay_inside_the_build():
    with TestClient(app) as anonymous:
        assert anonymous.get("/app").status_code == 401
    with _client() as client:
        escaped = client.get("/app/../index.html")
        # Either normalized away by the client or answered with the SPA entry; never the legacy file.
        assert escaped.status_code in (200, 404)
        assert "Demo Brand · Brand OS" not in escaped.text


def test_missing_build_has_an_actionable_error_without_bypassing_auth(monkeypatch, tmp_path):
    from app import main

    monkeypatch.setattr(main, "DASHBOARD_V2_DIR", tmp_path)
    with TestClient(app) as anonymous:
        assert anonymous.get("/app").status_code == 401
    with _client() as client:
        response = client.get("/app/content")
        assert response.status_code == 503
        assert "scripts/build_dashboard.py" in response.json()["detail"]
        assert client.get("/app/assets/missing.js").status_code == 404
