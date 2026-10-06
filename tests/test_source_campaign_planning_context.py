from __future__ import annotations

import json

from app import store
from app.connectors import ConnectorEvent, ConnectorKind, ConnectorResult, EventKind
from app.editorial import EditorialStore
from app.learning_engine import BrandLearningEngine
from app.source_campaign import SourceCampaignOperator
from app.sync import SyncOrchestrator


def _setup(tmp_path, *, mission: bool = True):
    store.DATA_PATH = tmp_path / "source-campaign-context.db"
    store.init_db()
    brand = store.get_brand("demo-brand")
    if mission:
        store.ensure_demo_brand_growth_mission()
    operator = SourceCampaignOperator(EditorialStore(store.DATA_PATH))
    account = store.upsert_connector_account(
        brand["id"], "rss", "context-feed", "Context feed", status="healthy",
    )
    return brand, account, operator


def _ingest(brand, account, operator, key: str):
    item = ConnectorEvent(
        ConnectorKind.RSS, EventKind.SOURCE_ITEM, f"rss:{key}",
        "2026-09-02T12:00:00+00:00", key,
        {"title": f"New pricing offer {key}",
         "summary": "The source reports an 80,000 point limited-time offer.",
         "url": f"https://example.test/{key}",
         "published_at": "2026-09-02T11:00:00+00:00",
         "content_fingerprint": f"fingerprint-{key}"},
    )
    return SyncOrchestrator(source_campaign_operator=operator).apply_result(
        ConnectorResult((item,)), connector_kind=ConnectorKind.RSS,
        brand_id=brand["id"], connector_account_id=account["id"], stream="content",
    )


def test_promotion_binds_complete_canonical_context_before_draft_generation(tmp_path):
    brand, account, operator = _setup(tmp_path)
    learning_engine = BrandLearningEngine(store.DATA_PATH)
    learning = learning_engine.propose(
        brand["id"], hypothesis="Source-supported numbers improve clarity",
        proposed_change="Prefer verified numeric hooks",
        evidence_for=[{"id": "observation-1", "summary": "Higher click-through"}],
        effect={"metric": "clicks", "direction": "increase"},
        uncertainty={"confidence": "low"}, scope={"stage": "source_candidate"},
        review_at="2099-01-01T00:00:00Z", actor="test",
    )
    learning_engine.transition(learning["id"], "testing", actor="test")
    learning_engine.transition(learning["id"], "accepted", actor="test")

    first = _ingest(brand, account, operator, "one")
    second = _ingest(brand, account, operator, "two")
    assert first.campaigns_projected == 1 and second.campaigns_projected == 1

    candidate = next(item for item in operator.editorial.list_candidates(brand["id"])
                     if item["title"].endswith("two"))
    snapshot = operator.planning_context(candidate["id"])
    projection = store.row(
        "SELECT * FROM source_campaign_projections WHERE candidate_id=?", (candidate["id"],),
    )

    assert snapshot["status"] == "ready"
    assert snapshot["blockers"] == []
    context = snapshot["context"]
    assert context["brand"]["mission"] == brand["mission"]
    assert context["brand"]["voice"] == brand["voice"]
    assert context["brand"]["compliance_rules"] == brand["compliance_rules"]
    assert context["personas"][0]["audience"]
    assert context["mission"]["goals"]
    assert context["recent_nonterminal_campaigns"]["limit"] == 10
    assert len(context["recent_nonterminal_campaigns"]["items"]) == 1
    assert context["accepted_learnings"]["items"][0]["id"] == learning["id"]
    assert context["bounded_performance_prior"]["policy"]["max_score_adjustment_points"] == 3.0
    assert context["source_evidence"]["source_id"] == projection["source_id"]
    assert context["source_evidence"]["connector_event_id"] in {
        item["connector_event_id"] for item in context["source_evidence"]["supporting_sources"]
    }
    assert projection["planning_context_id"] == snapshot["id"]
    assert projection["planning_context_fingerprint"] == snapshot["context_fingerprint"]
    assert store.row("SELECT status FROM posts WHERE id=?", (projection["post_id"],))["status"] == "draft"


def test_missing_required_context_surfaces_needs_attention_and_safe_retry(tmp_path):
    brand, account, operator = _setup(tmp_path, mission=False)
    outcome = _ingest(brand, account, operator, "missing-mission")
    candidate = operator.editorial.list_candidates(brand["id"])[0]

    assert outcome.campaigns_projected == 0
    assert candidate["recommended_treatment"] == "needs_attention_context"
    blocked = operator.planning_context(candidate["id"])
    assert blocked["status"] == "needs_attention"
    assert "exactly one active brand mission is required" in blocked["blockers"]
    assert store.row("SELECT decision FROM source_promotion_audit")["decision"] == "context_blocked"
    assert store.rows("SELECT * FROM campaigns") == []

    store.ensure_demo_brand_growth_mission()
    promoted = operator.promote_shortlist(brand["id"], [candidate["id"]], minimum_score=0)
    assert len(promoted) == 1
    assert operator.planning_context(candidate["id"])["status"] == "ready"
    assert len(store.rows("SELECT * FROM campaigns")) == 1
    assert operator.promote_shortlist(brand["id"], [candidate["id"]], minimum_score=0) == []
    assert len(store.rows("SELECT * FROM campaigns")) == 1


def test_unbound_source_evidence_fails_closed_without_generation(tmp_path):
    brand, account, operator = _setup(tmp_path, mission=False)
    _ingest(brand, account, operator, "unbound")
    candidate = operator.editorial.list_candidates(brand["id"])[0]
    store.ensure_demo_brand_growth_mission()
    evidence = candidate["supporting_sources"]
    evidence[0]["connector_event_id"] = "wrong-event"
    with store.connection() as connection:
        connection.execute(
            "UPDATE editorial_candidates SET supporting_sources=? WHERE id=?",
            (json.dumps(evidence), candidate["id"]),
        )

    promoted = operator.promote_shortlist(brand["id"], [candidate["id"]], minimum_score=0)

    assert promoted == []
    assert "source evidence is not bound to the selected connector event" in (
        operator.planning_context(candidate["id"])["blockers"]
    )
    assert store.rows("SELECT * FROM campaigns") == []


def test_context_blocked_cluster_leader_does_not_hide_valid_runner_up(tmp_path):
    brand, account, operator = _setup(tmp_path, mission=False)
    _ingest(brand, account, operator, "leader")
    _ingest(brand, account, operator, "runner-up")
    candidates = operator.editorial.list_candidates(brand["id"])
    leader = next(item for item in candidates if item["title"].endswith("leader"))
    runner_up = next(item for item in candidates if item["title"].endswith("runner-up"))
    evidence = leader["supporting_sources"]
    evidence[0]["connector_event_id"] = "wrong-event"
    with store.connection() as connection:
        connection.execute(
            "UPDATE editorial_candidates SET supporting_sources=?,score=99 WHERE id=?",
            (json.dumps(evidence), leader["id"]),
        )
        connection.execute(
            "UPDATE editorial_candidates SET score=98 WHERE id=?", (runner_up["id"],),
        )
    store.ensure_demo_brand_growth_mission()

    promoted = operator.promote_shortlist(
        brand["id"], [leader["id"], runner_up["id"]], minimum_score=0, limit=1,
    )

    assert [item.candidate_id for item in promoted] == [runner_up["id"]]
    assert operator.planning_context(leader["id"])["status"] == "needs_attention"
    assert operator.planning_context(runner_up["id"])["status"] == "ready"
