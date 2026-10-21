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


def test_endpoint_reports_unavailable_without_a_configured_model(source, monkeypatch):
    for key in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "BRANDMAN_DRAFT_API_KEY",
        "BRANDMAN_DRAFT_BASE_URL", "BRANDMAN_DRAFT_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)
    with TestClient(app, headers=AUTH) as client:
        response = client.post(f"/api/brands/demo-brand/sources/{source['id']}/draft-campaign")
    assert response.status_code == 503
    assert "BRANDMAN_DRAFT_PROVIDER" in response.json()["detail"]


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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    plan = drafting.anthropic_planner()({"name": "Demo"}, {"title": "t"})
    assert plan.campaign_name == "Launch: t"
    assert calls["model"] == drafting.DEFAULT_MODEL
    assert calls["output_format"] is CampaignPlan
    assert calls["fallbacks"] == "default" and calls["betas"] == [drafting.FALLBACK_BETA]
    assert "Demo" in calls["messages"][0]["content"]


def test_missing_api_key_is_reported_before_a_provider_call(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("anthropic")
    fake.AnthropicError = Exception

    def refuse():
        raise AssertionError("Anthropic client must not be constructed without a key")

    fake.Anthropic = refuse
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("BRANDMAN_DRAFT_PROVIDER", "anthropic")
    with pytest.raises(drafting.DraftingUnavailable, match="ANTHROPIC_API_KEY"):
        drafting.draft_campaign_from_source("demo-brand", "missing-source", actor="chris")


def test_provider_follows_config_and_keeps_anthropic_when_both_keys_exist(monkeypatch):
    for key in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "BRANDMAN_DRAFT_API_KEY",
        "BRANDMAN_DRAFT_BASE_URL", "BRANDMAN_DRAFT_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    assert drafting.draft_provider() == "anthropic"
    monkeypatch.setenv("BRANDMAN_DRAFT_PROVIDER", "openai")
    assert drafting.draft_provider() == "openai"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("BRANDMAN_DRAFT_PROVIDER")
    assert drafting.draft_provider() == "openai"
    monkeypatch.setenv("BRANDMAN_DRAFT_PROVIDER", "local")
    with pytest.raises(drafting.DraftingUnavailable, match="anthropic, openai, or none"):
        drafting.draft_provider()


def test_missing_openai_extra_names_the_install(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "openai", None)
    monkeypatch.setenv("BRANDMAN_DRAFT_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    with pytest.raises(drafting.DraftingUnavailable, match=r"brandman\[openai\]"):
        drafting.openai_planner()


def test_no_provider_keeps_drafting_with_an_external_agent(monkeypatch):
    for key in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "BRANDMAN_DRAFT_API_KEY",
        "BRANDMAN_DRAFT_BASE_URL", "BRANDMAN_DRAFT_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(drafting.DraftingUnavailable, match="MCP agent"):
        drafting.builtin_planner()
    monkeypatch.setenv("BRANDMAN_DRAFT_PROVIDER", "none")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "present-but-disabled")
    assert drafting.draft_provider() is None


def _openai_module(parse, create=None):
    import types

    class APIStatusError(Exception):
        def __init__(self, status_code, message):
            super().__init__(message)
            self.status_code = status_code
            self.message = message

    fake = types.ModuleType("openai")
    fake.OpenAIError = Exception
    fake.APIStatusError = APIStatusError
    fake.APIConnectionError = type("APIConnectionError", (Exception,), {})
    completions = types.SimpleNamespace(parse=parse, create=create or parse)
    fake.OpenAI = lambda **kwargs: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=completions), captured=kwargs,
    )
    return fake


def test_openai_compatible_planner_uses_base_url_and_structured_output(monkeypatch):
    import sys
    calls = {}

    def parse(**kwargs):
        calls.update(kwargs)
        return types_response(_plan({}, {"title": "t"}))

    fake = _openai_module(parse)
    holders = {}
    original = fake.OpenAI

    def openai_factory(**kwargs):
        holders.update(kwargs)
        return original(**kwargs)

    fake.OpenAI = openai_factory
    monkeypatch.setitem(sys.modules, "openai", fake)
    monkeypatch.setenv("BRANDMAN_DRAFT_PROVIDER", "openai")
    monkeypatch.setenv("BRANDMAN_DRAFT_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("BRANDMAN_DRAFT_MODEL", "llama3.1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("BRANDMAN_DRAFT_API_KEY", raising=False)
    plan = drafting.openai_planner()({"name": "Demo"}, {"title": "t"})
    assert plan.campaign_name == "Launch: t"
    assert holders["base_url"] == "http://127.0.0.1:11434/v1"
    assert holders["api_key"] == "local"
    assert calls["model"] == "llama3.1"
    assert calls["response_format"] is CampaignPlan


def test_openai_endpoint_without_schema_support_parses_json(monkeypatch):
    import sys

    raised = {}

    def parse(**_kwargs):
        raise raised["error"]

    def create(**_kwargs):
        body = _plan({}, {"title": "t"}).model_dump_json()
        message = type("Message", (), {"content": f"```json\n{body}\n```", "parsed": None, "refusal": None})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()

    fake = _openai_module(parse, create)
    raised["error"] = fake.APIStatusError(400, "response_format is unsupported")
    monkeypatch.setitem(sys.modules, "openai", fake)
    monkeypatch.setenv("BRANDMAN_DRAFT_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("BRANDMAN_DRAFT_BASE_URL", raising=False)
    monkeypatch.delenv("BRANDMAN_DRAFT_MODEL", raising=False)
    plan = drafting.openai_planner()({"name": "Demo"}, {"title": "t"})
    assert plan.posts[0].draft == "It shipped."
    assert drafting.model_name() == drafting.OPENAI_DEFAULT_MODEL


def test_openai_refusal_does_not_save_a_plan(monkeypatch):
    import sys

    def parse(**_kwargs):
        message = type("Message", (), {"content": None, "parsed": None, "refusal": "no"})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()

    fake = _openai_module(parse)
    monkeypatch.setitem(sys.modules, "openai", fake)
    monkeypatch.setenv("BRANDMAN_DRAFT_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    with pytest.raises(drafting.DraftingRefused):
        drafting.openai_planner()({}, {"title": "t"})


def types_response(plan):
    message = type("Message", (), {"content": None, "parsed": plan, "refusal": None})()
    choice = type("Choice", (), {"message": message})()
    return type("Response", (), {"choices": [choice]})()
