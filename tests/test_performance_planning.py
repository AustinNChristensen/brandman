import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import store
from app.campaign_templates import CampaignTemplateStore
from app.campaign_graph import CampaignGraphStore
from app.connectors import ConnectorEvent, ConnectorKind, ConnectorResult, EventKind
from app.editorial import EditorialStore
from app.performance_planning import PerformancePlanningEngine
from app.source_campaign import SourceCampaignOperator
from app.sync import SyncOrchestrator


NOW = "2026-09-02T12:00:00+00:00"


def setup(tmp_path: Path):
    store.DATA_PATH = tmp_path / "performance-planning.db"
    store.init_db()
    brand = store.get_brand("demo-brand")
    store.ensure_demo_brand_growth_mission()
    SourceCampaignOperator(EditorialStore(store.DATA_PATH))
    return brand, PerformancePlanningEngine(store.DATA_PATH)


def add_source(brand_id: str, topic: str, suffix: str) -> dict:
    source = store.insert("sources", {
        "brand_id": brand_id, "title": f"Source {suffix}", "url": f"https://example.test/{suffix}",
        "source_type": "rss", "body_summary": topic, "lifecycle_state": "published",
        "scheduled_for": None, "external_source_id": suffix,
    })
    account = store.upsert_connector_account(brand_id, "rss", f"feed-{suffix}", f"Feed {suffix}")
    event = store.insert("connector_events", {
        "connector_account_id": account["id"], "stream": "content", "external_id": suffix,
        "event_type": "source_item", "payload": "{}", "observed_at": NOW,
    })
    candidate = EditorialStore(store.DATA_PATH).upsert_candidate(
        brand_id, f"Candidate {suffix}", {"relevance": 0.8},
        supporting_sources=[{"source_id": source["id"], "url": source["url"]}],
        cluster_key=topic,
    )
    with store.connection() as connection:
        connection.execute(
            """INSERT INTO source_intelligence_records
               (id,brand_id,identity,connector_event_id,source_id,candidate_id,publisher_name,
                cluster_key,intelligence_json,promotion_state,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,'backlog',?,?)""",
            (f"intel-{suffix}", brand_id, f"identity-{suffix}", event["id"], source["id"],
             candidate["id"], "Example", topic, "{}", NOW, NOW),
        )
    return source


def add_performance(
    brand_id: str, topic: str, suffix: str, *, channel: str = "x",
    observed_at: str = "2026-09-01T12:00:00+00:00", impressions: int = 1000,
    clicks: int = 100, create_campaign_only: bool = False,
) -> dict:
    source = add_source(brand_id, topic, suffix)
    campaign = store.insert("campaigns", {
        "brand_id": brand_id, "source_id": source["id"], "name": f"Campaign {suffix}",
        "objective": "Measure a governed draft", "status": "draft",
    })
    if create_campaign_only:
        return campaign
    post = store.insert("posts", {
        "campaign_id": campaign["id"], "channel": channel, "body": "Measured item",
        "status": "posted", "scheduled_for": None, "external_post_id": f"external-{suffix}",
    })
    return store.insert("performance_records", {
        "brand_id": brand_id, "post_id": post["id"], "source_id": source["id"],
        "channel": channel, "observed_at": observed_at, "impressions": impressions,
        "clicks": clicks, "engagements": clicks, "conversions": 0,
        "revenue_cents": 0, "notes": "normalized provider observation",
    })


def test_no_data_is_explicit_inert_and_audited(tmp_path):
    brand, engine = setup(tmp_path)
    result = engine.plan(brand["id"], {"stage": "source_candidate", "channel": "x"}, as_of=NOW)

    assert result["status"] == "no_data"
    assert result["bounded_prior"]["score_adjustment_points"] == 0
    assert result["policy"]["role"] == "bounded_prior_not_formula"
    assert "no_auto_publish" in result["policy"]["protected_invariants"]
    assert len(engine.audit(brand["id"])) == 1


def test_stale_and_incompatible_channel_metrics_never_influence(tmp_path):
    brand, engine = setup(tmp_path)
    add_performance(brand["id"], "transfer-bonus", "stale", observed_at="2026-01-01T00:00:00Z")
    add_performance(brand["id"], "transfer-bonus", "email", channel="newsletter")

    result = engine.plan(
        brand["id"], {"stage": "source_candidate", "channel": "x", "topic": "transfer-bonus"},
        as_of=NOW, audit=False,
    )

    assert result["status"] == "no_compatible_data"
    assert result["bounded_prior"]["score_adjustment_points"] == 0
    assert len(result["evidence"]["excluded"]["stale"]) == 1
    assert len(result["evidence"]["excluded"]["incompatible_channel"]) == 1


def test_performance_prior_is_scoped_decayed_shrunk_bounded_and_deterministic(tmp_path):
    brand, engine = setup(tmp_path)
    add_performance(brand["id"], "transfer-bonus", "match-1", clicks=180)
    add_performance(brand["id"], "transfer-bonus", "match-2", clicks=160,
                    observed_at="2026-08-20T12:00:00Z")
    add_performance(brand["id"], "award-travel", "baseline-1", clicks=10)
    add_performance(brand["id"], "award-travel", "baseline-2", clicks=20)
    scope = {"stage": "source_candidate", "channel": "x", "topic": "transfer-bonus"}

    first = engine.plan(brand["id"], scope, as_of=NOW, audit=False)
    second = engine.plan(brand["id"], scope, as_of=NOW, audit=False)

    assert first == second
    assert first["status"] == "applied"
    assert 0 < first["bounded_prior"]["score_adjustment_points"] <= 3
    assert first["estimate"]["shrunk_rate"] < first["estimate"]["scoped_rate"]
    assert first["estimate"]["shrunk_rate"] > first["estimate"]["same_channel_baseline_rate"]
    assert first["policy"]["half_life_days"] == 30
    assert first["evidence"]["included_count"] == 2


def test_small_sample_and_repetition_cannot_overfit(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "now", lambda: NOW)
    brand, engine = setup(tmp_path)
    add_performance(brand["id"], "transfer-bonus", "winner", clicks=900)
    too_small = engine.plan(
        brand["id"], {"channel": "x", "topic": "transfer-bonus"}, as_of=NOW, audit=False,
    )
    assert too_small["status"] == "insufficient_evidence"
    assert too_small["bounded_prior"]["score_adjustment_points"] == 0

    add_performance(brand["id"], "transfer-bonus", "winner-2", clicks=800)
    add_performance(brand["id"], "award-travel", "baseline", clicks=10)
    # Three recent campaigns with this topic suppress positive exploitation,
    # while deterministic exploration remains a separate, unchanged policy.
    add_performance(brand["id"], "transfer-bonus", "repeat-3", clicks=700,
                    create_campaign_only=True)
    repeated = engine.plan(
        brand["id"], {"channel": "x", "topic": "transfer-bonus"}, as_of=NOW, audit=False,
    )
    assert repeated["status"] == "applied"
    assert repeated["bounded_prior"]["unfatigued_adjustment_points"] > 0
    assert repeated["bounded_prior"]["score_adjustment_points"] == 0
    assert repeated["repetition_guard"]["positive_prior_multiplier"] == 0
    assert repeated["repetition_guard"]["does_not_change_exploration_bucket"] is True


def test_tenant_isolation_excludes_other_brand_performance(tmp_path):
    brand, engine = setup(tmp_path)
    other = store.create_brand({
        "slug": "other-brand", "name": "Other", "mission": "Other mission",
        "voice": "Other voice", "compliance_rules": json.dumps([]), "approval_policy": "human",
    })
    add_performance(other["id"], "transfer-bonus", "other-1", clicks=900)
    add_performance(other["id"], "transfer-bonus", "other-2", clicks=900)

    result = engine.plan(brand["id"], {"channel": "x", "topic": "transfer-bonus"}, as_of=NOW)

    assert result["status"] == "no_data"
    assert result["evidence"]["available_count"] == 0
    assert engine.audit(other["id"]) == []


def test_native_observation_wins_over_duplicate_normalized_projection(tmp_path):
    brand, engine = setup(tmp_path)
    record = add_performance(brand["id"], "transfer-bonus", "duplicate", clicks=100)
    post = store.row("SELECT * FROM posts WHERE id=?", (record["post_id"],))
    graph = CampaignGraphStore(store.DATA_PATH)
    member = graph.attach(
        post["campaign_id"], asset_type="x_post", asset_id=post["id"], channel="x",
        role="anchor", actor="test", reason="Bind native metric",
    )
    graph.record_metric(
        member["id"], observed_at=record["observed_at"],
        native_metrics={"impressions": 1000, "clicks": 100},
        attribution_confidence="verified", idempotency_key="native-duplicate",
    )

    result = engine.plan(brand["id"], {"channel": "x"}, as_of=NOW, audit=False)

    assert result["evidence"]["available_count"] == 1
    assert result["evidence"]["included_count"] == 1
    assert result["evidence"]["included_ids"][0].startswith("native:")
    assert result["evidence"]["excluded"]["duplicate_measurement"] == [
        f"normalized:{record['id']}"
    ]


def test_source_promotion_reads_bounded_prior_without_approval_side_effects(tmp_path):
    brand, _ = setup(tmp_path)
    add_performance(brand["id"], "transfer-bonus", "history-1", clicks=180)
    add_performance(brand["id"], "transfer-bonus", "history-2", clicks=160)
    add_performance(brand["id"], "award-travel", "history-base", clicks=5)
    editorial = EditorialStore(store.DATA_PATH)
    operator = SourceCampaignOperator(editorial)
    account = store.upsert_connector_account(brand["id"], "rss", "live", "Live feed")
    incoming = ConnectorEvent(
        connector=ConnectorKind.RSS, kind=EventKind.SOURCE_ITEM,
        dedup_key="rss:new-transfer", occurred_at="2026-09-02T11:00:00Z",
        external_id="new-transfer", payload={
            "title": "New 30% transfer bonus", "summary": "Transfer points for a limited time.",
            "url": "https://example.test/new-transfer", "published_at": "2026-09-02T11:00:00Z",
            "canonical_revalidation": {
                "canonical_url": "https://example.test/new-transfer",
                "observed_at": "2026-09-02T11:30:00Z",
                "snapshot_fingerprint": "sha256:verified-current-page",
                "feed_fingerprint": "feed-current", "status": "verified",
                "confidence": "medium", "title": "New 30% transfer bonus",
                "summary": "Transfer points for a limited time.", "claims": ["30%"],
                "rationale": ["Current canonical metadata agrees with the feed"],
                "idempotency_key": "verified-new-transfer",
            },
        },
    )

    SyncOrchestrator(source_campaign_operator=operator).apply_result(
        ConnectorResult((incoming,)), connector_kind=ConnectorKind.RSS,
        brand_id=brand["id"], connector_account_id=account["id"], stream="content",
    )

    candidate = next(item for item in editorial.list_candidates(brand["id"])
                     if item["title"] == "New 30% transfer bonus")
    context = candidate["intelligence"]["performance_context"]
    assert context["status"] == "applied"
    assert context["bounded_prior"]["score_adjustment_points"] > 0
    assert candidate["recommended_treatment"] == "evaluate_with_performance_prior"
    new_post = store.row(
        """SELECT p.* FROM posts p JOIN source_campaign_projections sp ON sp.post_id=p.id
           WHERE sp.candidate_id=?""", (candidate["id"],),
    )
    assert new_post["status"] == "draft"
    assert new_post["scheduled_for"] is None and new_post["external_post_id"] is None
    assert store.rows("SELECT * FROM approval_snapshots") == [] if store.row(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='approval_snapshots'"
    ) else True


def test_guided_template_preflight_exposes_performance_but_keeps_exploration_stable(tmp_path):
    brand, _ = setup(tmp_path)
    templates = CampaignTemplateStore(store.DATA_PATH)
    source_one = add_source(brand["id"], "transfer-bonus", "template-source-1")
    source_two = add_source(brand["id"], "transfer-bonus", "template-source-2")
    answers = {"goal": "Explain", "audience": "Readers", "source": "Official",
               "cta": "Read", "flight": "Launch", "success": "Clicks", "topic": "transfer-bonus"}
    for index, source in enumerate((source_one, source_two), start=1):
        campaign = templates.instantiate(
            "newsletter-led", "demo-brand", answers, name=f"Newsletter {index}",
            objective="Explain", source_id=source["id"], idempotency_key=f"newsletter:{index}",
            actor="test",
        )
        post = store.insert("posts", {"campaign_id": campaign["id"], "channel": "newsletter",
                                      "body": "Measured newsletter", "status": "posted",
                                      "scheduled_for": None, "external_post_id": f"newsletter-{index}"})
        store.insert("performance_records", {
            "brand_id": brand["id"], "post_id": post["id"], "source_id": source["id"],
            "channel": "newsletter", "observed_at": "2026-09-01T12:00:00Z",
            "impressions": 1000, "clicks": 160, "engagements": 160,
            "conversions": 0, "revenue_cents": 0, "notes": "provider measurement",
        })
    add_performance(brand["id"], "award-travel", "newsletter-baseline",
                    channel="newsletter", clicks=10)

    first = templates.preflight("newsletter-led", "demo-brand", answers)
    second = templates.preflight("newsletter-led", "demo-brand", answers)

    assert first["performance_context"]["status"] == "applied"
    assert first["performance_context"]["bounded_prior"]["score_adjustment_points"] >= 0
    assert first["exploration"] == second["exploration"]
    assert first["performance_context"]["repetition_guard"]["does_not_change_exploration_bucket"] is True
    assert first["creates_nothing"] is True
    assert "no_auto_publish" in first["hard_boundaries"]


def test_source_content_drift_withholds_an_otherwise_positive_prior(tmp_path):
    brand, _ = setup(tmp_path)
    add_performance(brand["id"], "transfer-bonus", "prior-1", clicks=180)
    add_performance(brand["id"], "transfer-bonus", "prior-2", clicks=160)
    add_performance(brand["id"], "award-travel", "prior-base", clicks=5)
    editorial = EditorialStore(store.DATA_PATH)
    operator = SourceCampaignOperator(editorial)
    account = store.upsert_connector_account(brand["id"], "rss", "drift", "Drifted feed")
    incoming = ConnectorEvent(
        connector=ConnectorKind.RSS, kind=EventKind.SOURCE_ITEM,
        dedup_key="rss:drift", occurred_at="2026-09-02T11:00:00Z", external_id="drift",
        payload={
            "title": "Old transfer bonus headline", "summary": "Terms may have changed.",
            "url": "https://example.test/drift", "published_at": "2026-09-02T11:00:00Z",
            "content_fingerprint": "feed-version", "canonical_content_fingerprint": "current-page-version",
            "claim_conflicts": ["Offer amount differs from the canonical page"],
            "canonical_revalidation": {
                "canonical_url": "https://example.test/drift",
                "observed_at": "2026-09-02T11:30:00Z",
                "snapshot_fingerprint": "sha256:current-page-version",
                "feed_fingerprint": "feed-version", "status": "conflict",
                "confidence": "medium", "title": "Current canonical offer",
                "summary": "Different current terms", "claims": ["30%"],
                "rationale": ["Feed offer differs from current canonical metadata"],
                "idempotency_key": "conflict-drift",
            },
        },
    )
    SyncOrchestrator(source_campaign_operator=operator).apply_result(
        ConnectorResult((incoming,)), connector_kind=ConnectorKind.RSS,
        brand_id=brand["id"], connector_account_id=account["id"], stream="content",
    )

    candidate = next(item for item in editorial.list_candidates(brand["id"])
                     if item["title"] == "Old transfer bonus headline")
    context = candidate["intelligence"]["performance_context"]
    gate = context["source_evidence_gate"]
    assert context["status"] == "source_evidence_blocked"
    assert context["bounded_prior"]["unfatigued_adjustment_points"] > 0
    assert context["bounded_prior"]["score_adjustment_points"] == 0
    assert gate["content_drift_detected"] is True
    assert gate["canonical_revalidation_status"] == "conflict"
    assert gate["claim_conflicts"]
    assert candidate["recommended_treatment"] == "verify_source_conflict"
    assert store.row(
        "SELECT 1 FROM source_campaign_projections WHERE candidate_id=?", (candidate["id"],),
    ) is None


def test_rest_and_mcp_expose_read_only_planning_and_audit(tmp_path, monkeypatch):
    brand, _ = setup(tmp_path)
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "test-password")
    from app.main import app
    from app.mcp_server import get_performance_planning

    headers = {"Authorization": "Basic b3BlcmF0b3I6dGVzdC1wYXNzd29yZA=="}
    with TestClient(app, headers=headers) as client:
        response = client.get(
            "/api/brands/demo-brand/performance-planning",
            params={"channel": "x", "topic": "transfer-bonus", "as_of": NOW},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "no_data"
        audit = client.get("/api/brands/demo-brand/performance-planning/audit").json()
        assert audit[0]["brand_id"] == brand["id"]
        assert audit[0]["scope"]["topic"] == "transfer-bonus"

    mcp_result = get_performance_planning(
        "demo-brand", channel="x", topic="transfer-bonus", as_of=NOW,
    )
    assert mcp_result["policy"]["side_effects"].startswith("read_and_audit_only")
    assert store.rows("SELECT * FROM campaigns") == []
