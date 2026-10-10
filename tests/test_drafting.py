import base64

from fastapi.testclient import TestClient
import pytest

from brandman import drafting, store
from brandman.main import app
from brandman.workflows import CampaignPlan, CampaignPost

AUTH = {"Authorization": "Basic " + base64.b64encode(b"operator:drafting-test").decode()}


def _plan(_context, source):
    return CampaignPlan(
        campaign_name=f"Launch: {source['title']}", objective="Explain the change",
        hook="What changed", audience="Existing users",
        posts=[CampaignPost(channel="X", timing="launch", angle="news", draft="It shipped.")],
        compliance_checks=["Verify the date"],
    )


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setenv("BRANDMAN_PREVIEW_PASSWORD", "drafting-test")
    store.DATA_PATH = tmp_path / "drafting.db"
    store.init_db(profile="test")
    brand = store.get_brand("demo-brand")
    return store.insert("sources", {
        "brand_id": brand["id"], "title": "Pricing update", "url": "https://demo.example/p",
        "source_type": "manual", "body_summary": "New tiers.", "lifecycle_state": "new",
    })


def test_draft_saves_only_drafts_attributed_to_the_principal(source):
    result = drafting.draft_campaign_from_source(
        "demo-brand", source["id"], actor="chris", planner=_plan,
    )
    assert result.campaign["name"] == "Launch: Pricing update"
    assert [post["status"] for post in result.posts] == ["draft"]
    assert result.posts[0]["channel"] == "x"
    audit = store.rows("SELECT actor, action FROM campaign_post_audit WHERE post_id=?", (result.posts[0]["id"],))
    assert audit == [{"actor": "chris", "action": "draft_created"}]


def test_unknown_source_and_cross_brand_source_are_refused(source):
    with pytest.raises(KeyError):
        drafting.draft_campaign_from_source("demo-personal", source["id"], actor="chris", planner=_plan)


def test_endpoint_reports_unavailable_without_the_extra(source, monkeypatch):
    def unavailable():
        raise drafting.DraftingUnavailable("not installed")
    monkeypatch.setattr(drafting, "anthropic_planner", unavailable)
    with TestClient(app, headers=AUTH) as client:
        response = client.post(f"/api/brands/demo-brand/sources/{source['id']}/draft-campaign")
    assert response.status_code == 503


def test_anthropic_planner_requests_structured_output_with_refusal_fallback(monkeypatch):
    import sys
    import types
    calls = {}

    class FakeMessages:
        def parse(self, **kwargs):
            calls.update(kwargs)
            return types.SimpleNamespace(stop_reason="end_turn", parsed_output=_plan({}, {"title": "t"}))

    fake = types.ModuleType("anthropic")
    fake.AnthropicError = Exception
    fake.APIStatusError = type("APIStatusError", (Exception,), {})
    fake.APIConnectionError = type("APIConnectionError", (Exception,), {})
    fake.Anthropic = lambda: types.SimpleNamespace(beta=types.SimpleNamespace(messages=FakeMessages()))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    plan = drafting.anthropic_planner()({"name": "Demo"}, {"title": "t"})
    assert plan.campaign_name == "Launch: t"
    assert calls["model"] == drafting.DEFAULT_MODEL
    assert calls["output_format"] is CampaignPlan
    assert calls["fallbacks"] == "default" and calls["betas"] == [drafting.FALLBACK_BETA]
    assert "Demo" in calls["messages"][0]["content"]
