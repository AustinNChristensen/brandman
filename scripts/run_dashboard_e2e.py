"""Start BrandOS with a disposable, seeded development database for browser tests."""

from __future__ import annotations

import os
from pathlib import Path
import sys


PROJECT = Path(__file__).parents[1]
DATABASE = Path("/tmp/brand-os-dashboard-e2e-8011.db")
PASSWORD = "brand-os-e2e-only"


def main() -> None:
    DATABASE.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment.update({
        "BRAND_OS_DB": str(DATABASE),
        "BRAND_OS_DATABASE_PROFILE": "development",
        "BRAND_OS_PREVIEW_PASSWORD": PASSWORD,
        "BRAND_OS_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "BRAND_OS_HTTPS": "false",
    })
    os.environ.update(environment)
    sys.path.insert(0, str(PROJECT))
    # Application startup creates the standard development brands. UI tests add
    # only reversible scratch records in this disposable database.
    from fastapi.testclient import TestClient
    from brandman.connectors import ConnectorEvent, ConnectorKind, EventKind, dedup_identity
    from brandman.main import app, engagement_store
    from brandman import store

    with TestClient(app) as client:
        response = client.get("/api/brands", auth=("operator", PASSWORD))
        response.raise_for_status()
        brand = store.get_brand("demo-brand")
        engagement_store.project(
            brand_id=brand["id"], connector_account_id="e2e-x-read",
            event=ConnectorEvent(
                ConnectorKind.X, EventKind.POST_PUBLISHED,
                dedup_identity(ConnectorKind.X, external_id="mention:9910000001"),
                "2026-09-03T12:00:00+00:00", "9910000001",
                {
                    "evidence_type": "x_engagement_opportunity", "opportunity_type": "mention",
                    "text": "E2E: should I transfer these points now?",
                    "author": {"id": "e2e-reader", "username": "e2e_reader"},
                    "conversation_id": "9910000001", "referenced_tweets": [],
                    "parent_context": [], "public_metrics": {"reply_count": 0},
                    "requires_approval": True,
                    "external_url": "https://x.com/e2e_reader/status/9910000001",
                },
            ),
        )
    os.execve(sys.executable, [
        sys.executable, "-m", "uvicorn", "brandman.main:app",
        "--host", "127.0.0.1", "--port", "8011",
    ], environment)


if __name__ == "__main__":
    main()
