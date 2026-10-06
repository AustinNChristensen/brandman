import pytest

from app import store
from app.campaign_templates import CampaignTemplateError, CampaignTemplateStore
from app.learning_engine import BrandLearningEngine, LearningError


ANSWERS = {"goal": "Explain the offer", "audience": "Readers",
           "source": "Governed source", "cta": "Read", "flight": "Launch week",
           "success": "CTR over baseline"}


def setup(tmp_path, monkeypatch):
    database = tmp_path / "learning.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    store.init_db()
    brand = store.get_brand("demo-brand")
    return database, brand, BrandLearningEngine(database), CampaignTemplateStore(database)


def proposal(engine, brand_id):
    return engine.propose(brand_id, hypothesis="Specific hooks improve qualified clicks",
        proposed_change="Prefer a specific numeric hook when the governed source supports it.",
        evidence_for=[{"id": "outcome-1", "summary": "Variant earned 21 qualified clicks",
                       "campaign_id": "campaign-1", "metric": "clicks", "value": 21}],
        evidence_against=[{"id": "outcome-2", "summary": "Small sample"}],
        effect={"metric": "clicks", "direction": "increase", "estimate": 0.12},
        uncertainty={"confidence": "low", "sample_size": 2},
        scope={"template_key": "newsletter-led", "channel": "newsletter"},
        review_at="2099-01-01T00:00:00Z", actor="analyst")


def test_only_accepted_active_scoped_learning_influences_preflight(tmp_path, monkeypatch):
    _, brand, engine, templates = setup(tmp_path, monkeypatch)
    learning = proposal(engine, brand["id"])
    proposed = templates.preflight("newsletter-led", "demo-brand", ANSWERS)
    assert proposed["learning_context"]["applied"] is False

    engine.transition(learning["id"], "testing", actor="Chris")
    assert templates.preflight("newsletter-led", "demo-brand", ANSWERS)["learning_context"]["applied"] is False
    engine.transition(learning["id"], "accepted", actor="Chris")
    accepted = templates.preflight("newsletter-led", "demo-brand", ANSWERS)
    assert accepted["learning_context"]["selected_learning_ids"] == [learning["id"]]
    assert accepted["learning_context"]["rule"] == "accepted_active_scoped_priors_only"
    assert accepted["learning_context"]["explanations"][0]["role"] == "bounded_prior_not_formula"
    assert templates.preflight("offer-alert", "demo-brand", ANSWERS)["learning_context"]["applied"] is False

    engine.set_active(learning["id"], False, actor="Chris", reason="Revert while evidence is reviewed")
    assert templates.preflight("newsletter-led", "demo-brand", ANSWERS)["learning_context"]["applied"] is False


def test_rejected_and_cross_brand_learnings_never_apply(tmp_path, monkeypatch):
    database, brand, engine, templates = setup(tmp_path, monkeypatch)
    rejected = proposal(engine, brand["id"])
    engine.transition(rejected["id"], "rejected", actor="Chris")
    assert templates.preflight("newsletter-led", "demo-brand", ANSWERS)["learning_context"]["applied"] is False
    with pytest.raises(LearningError):
        engine.transition(rejected["id"], "accepted", actor="Chris")
    assert engine.retrieve("different-brand", {"template_key": "newsletter-led"})["learnings"] == []


def test_exploration_override_is_soft_audited_and_cannot_touch_safety(tmp_path, monkeypatch):
    database, _, _, templates = setup(tmp_path, monkeypatch)
    preview = templates.preflight("newsletter-led", "demo-brand", ANSWERS,
                                  override_reason="Try a two-day cadence")
    assert preview["expert_override"] == {
        "requested": True, "reason": "Try a two-day cadence", "kind": "soft_planning_override",
        "hard_boundaries_preserved": True, "audited": True,
    }
    assert store.row("SELECT override_kind FROM campaign_exploration_override_audit")["override_kind"] == "soft_planning_override"
    for invariant in ("canonical_brand_voice", "canonical_compliance", "exact_revision_approval",
                      "credential_safety", "destination_binding", "no_auto_publish"):
        assert invariant in preview["exploration"]["policy"]["protected_invariants"]
    with pytest.raises(CampaignTemplateError, match="protected invariants"):
        templates.preflight("newsletter-led", "demo-brand", {**ANSWERS, "auto_publish": True},
                            override_reason="Ship faster")


def test_accept_requires_testing_and_review_timestamp_is_governed(tmp_path, monkeypatch):
    _, brand, engine, _ = setup(tmp_path, monkeypatch)
    learning = proposal(engine, brand["id"])
    with pytest.raises(LearningError, match="proposed to accepted"):
        engine.transition(learning["id"], "accepted", actor="Chris")
    with pytest.raises(LearningError, match="timezone-aware"):
        engine.propose(brand["id"], hypothesis="Bad time", proposed_change="No-op",
            evidence_for=[{"summary": "Evidence"}], effect={}, uncertainty={}, scope={},
            review_at="2099-01-01T12:00:00", actor="analyst")
    normalized = engine.propose(brand["id"], hypothesis="Offset time", proposed_change="No-op",
        evidence_for=[{"summary": "Evidence"}], effect={}, uncertainty={}, scope={},
        review_at="2099-01-01T05:00:00-07:00", actor="analyst")
    assert normalized["review_at"] == "2099-01-01T12:00:00+00:00"


def test_malformed_or_expired_rows_are_inert_in_retrieval_and_brand_context(tmp_path, monkeypatch):
    _, brand, engine, _ = setup(tmp_path, monkeypatch)
    malformed = proposal(engine, brand["id"])
    engine.transition(malformed["id"], "testing", actor="Chris")
    engine.transition(malformed["id"], "accepted", actor="Chris")
    with engine._connect() as connection:
        connection.execute("UPDATE brand_learnings SET review_at='not-a-date' WHERE id=?", (malformed["id"],))
    assert engine.retrieve(brand["id"], {"stage": "brand_context"})["learnings"] == []
    assert store.brand_context("demo-brand")["accepted_learnings"] == []


def test_mcp_proposal_uses_structured_engine_and_audit(tmp_path, monkeypatch):
    _, brand, _, _ = setup(tmp_path, monkeypatch)
    from app.mcp_server import propose_brand_learning
    result = propose_brand_learning("demo-brand", "MCP hypothesis", "Outcome evidence", "Try a hook",
        evidence_for=[{"id": "outcome-7", "summary": "Observed outcome"}],
        effect={"metric": "clicks"}, uncertainty={"confidence": "low"},
        scope={"stage": "source_candidate"}, review_at="2099-01-01T00:00:00Z")
    assert result["status"] == "proposed"
    assert result["evidence_for"][0]["id"] == "outcome-7"
    audit = store.row("SELECT actor,action FROM brand_learning_audit WHERE learning_id=?", (result["id"],))
    assert audit == {"actor": "mcp:propose_brand_learning", "action": "proposed"}
