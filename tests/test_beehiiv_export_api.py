import base64

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from brandman import store
from brandman.beehiiv_runtime import BEEHIIV_NEWSLETTER_EXPORT_JOB
from brandman.credentials import CredentialStore
from brandman.main import app


def auth_headers(password: str) -> dict[str, str]:
    token = base64.b64encode(f"operator:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def newsletter_review_token(client, issue_id):
    issues = client.get("/api/brands/demo-brand/newsletter-issues").json()
    return next(issue for issue in issues if issue["id"] == issue_id)["approval_scope"]["review_token"]


def complete_content() -> dict:
    return {
        "editorial_thesis": "Help readers make one useful pricing decision.",
        "target_reader": "Readers",
        "intended_outcome": "Choose the better redemption",
        "working_title": "A useful redemption",
        "final_title": "A useful redemption",
        "subject": "A useful redemption",
        "preview_text": "The useful math",
        "sections": [{"heading": "Decision", "body": "Details"}],
        "cta": {"label": "Read more", "url": "https://demo.example"},
        "seo": {"title": "A useful redemption", "description": "Guidance"},
        "content_basis": {"kind": "original_analysis", "statement": "DemoBrand redemption framework."},
        "claims": [],
        "source_provenance": [],
    }


def test_api_queues_only_approved_revision_and_exposes_durable_status(
    tmp_path, monkeypatch,
):
    password = "beehiiv-export-test"
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", password)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BRAND_OS_CREDENTIAL_MASTER_KEY", key)
    monkeypatch.setattr(store, "DATA_PATH", tmp_path / "brand-os.db")
    store.init_db()
    brand = store.get_brand("demo-brand")
    account = store.upsert_connector_account(
        brand["id"], "beehiiv", "pub-demo", "Demo Brand Beehiiv",
        status="healthy", scopes=["posts.write"], capabilities=["drafts.write"],
    )
    CredentialStore(store.DATA_PATH, key).put(
        "beehiiv", "pub-demo", "Demo Brand Beehiiv", {"api_key": "secret"},
        required_scopes=["posts.write"], granted_scopes=["posts.write"],
    )

    with TestClient(app, headers=auth_headers(password)) as client:
        issue = client.post(
            "/api/brands/demo-brand/newsletter-issues",
            json={"content": complete_content()},
        ).json()
        blocked = client.post(
            f"/api/newsletter-issues/{issue['id']}/export-draft", json={}
        )
        assert blocked.status_code == 409
        for target in ("outline", "draft"):
            response = client.post(
                f"/api/newsletter-issues/{issue['id']}/transition",
                json={"target": target},
            )
            assert response.status_code == 200
        assert client.post(
            f"/api/newsletter-issues/{issue['id']}/fact-check",
            json={"revision": 1, "verdicts": []},
        ).status_code == 200
        assert client.post(
            f"/api/newsletter-issues/{issue['id']}/approve",
            json={"revision": 1, "review_token": newsletter_review_token(client, issue["id"])},
        ).status_code == 200

        first = client.post(
            f"/api/newsletter-issues/{issue['id']}/export-draft",
            json={"connector_account_id": account["id"]},
        )
        second = client.post(
            f"/api/newsletter-issues/{issue['id']}/export-draft",
            json={"connector_account_id": account["id"]},
        )

        assert first.status_code == second.status_code == 202
        queued = first.json()
        assert second.json()["id"] == queued["id"]
        assert queued["job_type"] == BEEHIIV_NEWSLETTER_EXPORT_JOB
        assert queued["payload"] == {
            "issue_id": issue["id"], "revision": 1, "approval_revision": 1,
        }
        assert queued["status"] == "queued"
        status = client.get(
            f"/api/newsletter-export-jobs/{queued['id']}"
        ).json()
        assert status["payload"]["revision"] == 1
        history = client.get(
            f"/api/newsletter-issues/{issue['id']}/export-jobs"
        ).json()
        assert [job["id"] for job in history] == [queued["id"]]


def test_api_rejects_non_write_scoped_or_cross_brand_connector(tmp_path, monkeypatch):
    password = "beehiiv-export-test"
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", password)
    monkeypatch.setenv("BRAND_OS_CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(store, "DATA_PATH", tmp_path / "brand-os.db")
    store.init_db()
    brand = store.get_brand("demo-brand")
    store.upsert_connector_account(
        brand["id"], "beehiiv", "pub-readonly", "Read only",
        status="healthy", scopes=["posts.read"], capabilities=["posts.read"],
    )
    with TestClient(app, headers=auth_headers(password)) as client:
        issue = client.post(
            "/api/brands/demo-brand/newsletter-issues",
            json={"content": complete_content()},
        ).json()
        for target in ("outline", "draft"):
            client.post(
                f"/api/newsletter-issues/{issue['id']}/transition",
                json={"target": target},
            )
        client.post(
            f"/api/newsletter-issues/{issue['id']}/fact-check",
            json={"revision": 1, "verdicts": []},
        )
        client.post(
            f"/api/newsletter-issues/{issue['id']}/approve",
            json={"revision": 1, "review_token": newsletter_review_token(client, issue["id"])},
        )
        response = client.post(
            f"/api/newsletter-issues/{issue['id']}/export-draft", json={}
        )
        assert response.status_code == 409
        assert "posts.write" in response.json()["detail"]


def test_unapproved_export_is_rejected_before_secret_infrastructure(tmp_path, monkeypatch):
    password = "beehiiv-export-test"
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", password)
    monkeypatch.delenv("BRAND_OS_CREDENTIAL_MASTER_KEY", raising=False)
    monkeypatch.setattr(store, "DATA_PATH", tmp_path / "brand-os.db")
    store.init_db()
    with TestClient(app, headers=auth_headers(password)) as client:
        issue = client.post(
            "/api/brands/demo-brand/newsletter-issues",
            json={"content": complete_content()},
        ).json()
        response = client.post(f"/api/newsletter-issues/{issue['id']}/export-draft", json={})
        assert response.status_code == 409
        assert "currently approved revision" in response.json()["detail"]
        assert "credential" not in response.json()["detail"].casefold()
