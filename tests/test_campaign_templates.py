from fastapi.testclient import TestClient
import pytest

from app import store
from app.campaign_templates import CampaignTemplateError, CampaignTemplateStore
from app.main import app


def answers():
    return {
        "goal": "Explain the offer", "audience": "Points collectors",
        "source": "Governed candidate source", "cta": "Read the guide",
        "flight": "Launch week", "success": "CTR above the 2% baseline",
    }


def test_starter_recipes_are_versioned_and_hide_ai_instructions(tmp_path, monkeypatch):
    database = tmp_path / "templates.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    store.init_db()
    templates = CampaignTemplateStore(database).list()

    assert {item["template_key"] for item in templates} == {
        "newsletter-led", "x-thread-explainer", "offer-alert",
        "evergreen-resurfacing", "youtube-launch",
    }
    assert all(item["current_version"] == 1 for item in templates)
    assert all(len(item["contract"]["questions"]) == 6 for item in templates)
    assert "ai_instructions" not in repr(templates)


def test_preflight_is_non_mutating_and_preserves_hard_boundaries(tmp_path, monkeypatch):
    database = tmp_path / "template-preflight.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    store.init_db()
    templates = CampaignTemplateStore(database)
    with pytest.raises(CampaignTemplateError, match="guided intake"):
        templates.preflight("newsletter-led", "demo-brand", {"goal": "Explain"})

    preview = templates.preflight(
        "newsletter-led", "demo-brand", answers(),
        override_reason="Use a two-day follow-up cadence",
    )

    assert preview["ready"] is True
    assert preview["creates_nothing"] is True
    assert preview["graph"]["anchor"]["asset_type"] == "newsletter"
    assert "exact_revision_approval" in preview["hard_boundaries"]
    assert preview["expert_override"]["hard_boundaries_preserved"] is True
    assert preview["exploration"]["bucket"] in {"proven", "adjacent", "high_variance"}
    assert preview["exploration"] == templates.preflight(
        "newsletter-led", "demo-brand", answers(),
        override_reason="Use a two-day follow-up cadence",
    )["exploration"]
    assert preview["exploration"]["policy"]["accepted_learnings_role"] == "prior_not_formula"
    assert "tenant_privacy" in preview["exploration"]["policy"]["protected_invariants"]
    assert store.rows("SELECT id FROM campaigns") == []


def test_template_rest_contract_returns_graph_preview_only(tmp_path, monkeypatch):
    database = tmp_path / "template-api.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "test-password")
    headers = {"Authorization": "Basic b3BlcmF0b3I6dGVzdC1wYXNzd29yZA=="}
    with TestClient(app, headers=headers) as client:
        listed = client.get("/api/campaign-templates")
        assert listed.status_code == 200
        assert len(listed.json()) == 5
        preview = client.post(
            "/api/brands/demo-brand/campaign-templates/newsletter-led/preflight",
            json={"answers": answers()},
        )
        assert preview.status_code == 200
        assert preview.json()["creates_nothing"] is True
        assert "ai_instructions" not in preview.text
