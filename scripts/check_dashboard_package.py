"""Smoke-test a wheel's dashboard from its installed files on disposable data."""
import base64
import os
from pathlib import Path
import re
import sys
import tempfile


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="brandman-package-") as scratch:
        os.environ.update(
            BRAND_OS_DB=str(Path(scratch) / "test.db"),
            BRAND_OS_DATABASE_PROFILE="development",
            BRAND_OS_PREVIEW_PASSWORD="package-smoke-test-only",
            BRAND_OS_ALLOWED_HOSTS="testserver",
            BRAND_OS_HTTPS="false",
        )
        import app
        from app.main import app as application
        from fastapi.testclient import TestClient

        assert Path(app.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), (
            "The smoke test must import the installed wheel, not the source checkout"
        )
        token = base64.b64encode(b"operator:package-smoke-test-only").decode()
        with TestClient(application) as client:
            assert client.get("/app").status_code == 401
            client.headers["Authorization"] = f"Basic {token}"
            response = client.get("/app/content?brand=demo-brand")
            assert response.status_code == 200
            assets = re.findall(r'(?:src|href)="(/app/[^\"]+)"', response.text)
            assert assets and any(asset.endswith(".js") for asset in assets)
            for asset in assets:
                assert client.get(asset).status_code == 200, asset
            assert client.get("/app/assets/missing.js").status_code == 404
        print("Installed wheel serves dashboard routes and assets behind authentication")


if __name__ == "__main__":
    main()
