"""Smoke-test a wheel's dashboard from its installed files on disposable data."""
import argparse
import base64
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-only", action="store_true")
    args = parser.parse_args()
    if args.python_only:
        assert all(shutil.which(name) is None for name in ("node", "npm", "npx")), "Node tooling must be absent"
        assert not (Path.cwd() / "web").exists(), "Frontend checkout must be absent"
        import anthropic
        assert anthropic.__version__, "The wheel's anthropic extra must resolve"

        def reject_frontend_process(event, values):
            if event == "subprocess.Popen":
                executable, argv, *_ = values
                command = " ".join(str(v) for v in argv) if isinstance(argv, (list, tuple)) else str(argv)
                assert not any(name in command for name in ("build_dashboard.py", "npm", "npx")), "Startup attempted a frontend build"
                assert Path(str(executable)).name != "node", "Startup attempted Node"

        sys.addaudithook(reject_frontend_process)
    with tempfile.TemporaryDirectory(prefix="brandman-package-") as scratch:
        os.environ.update(
            BRANDMAN_DB=str(Path(scratch) / "test.db"),
            BRANDMAN_DATABASE_PROFILE="development",
            BRANDMAN_PREVIEW_PASSWORD="package-smoke-test-only",
            BRANDMAN_ALLOWED_HOSTS="testserver",
            BRANDMAN_HTTPS="false",
            BRANDMAN_REQUIRE_DASHBOARD="true",
            BRANDMAN_AUTO_BUILD_DASHBOARD="true",
        )
        import brandman
        from brandman.main import app as application
        from fastapi.testclient import TestClient

        assert Path(brandman.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), (
            "The smoke test must import the installed wheel, not the source checkout"
        )
        if args.python_only:
            assert not (Path(brandman.__file__).resolve().parent.parent / "web").exists()
        token = base64.b64encode(b"operator:package-smoke-test-only").decode()
        with TestClient(application) as client:
            assert client.get("/app").status_code == 401
            client.headers["Authorization"] = f"Basic {token}"
            assert client.get("/app").status_code == 200
            response = client.get("/app/content?brand=demo-brand")
            assert response.status_code == 200 and 'id="root"' in response.text
            assets = re.findall(r'(?:src|href)="(/app/[^\"]+)"', response.text)
            assert assets and any(asset.endswith(".js") for asset in assets)
            assert any(asset.endswith(".css") for asset in assets)
            for asset in assets:
                result = client.get(asset)
                assert result.status_code == 200 and result.content, asset
                if asset.endswith(".js"):
                    assert "javascript" in result.headers["content-type"], asset
                if asset.endswith(".css"):
                    assert "text/css" in result.headers["content-type"], asset
            assert client.get("/app/assets/missing.js").status_code == 404
        print("Installed wheel serves dashboard routes and assets behind authentication")


if __name__ == "__main__":
    main()
