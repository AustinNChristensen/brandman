import pytest

from app import store
from app.experiments import ExperimentError, ExperimentStore
from app.learning_engine import BrandLearningEngine


def setup_experiment(tmp_path):
    database = tmp_path / "experiments.db"
    store.DATA_PATH = database
    store.init_db()
    brand = store.get_brand("demo-brand")
    source = store.insert("sources", {
        "brand_id": brand["id"], "title": "An issuer offer changed",
        "url": "https://issuer.test/offer", "source_type": "rss",
        "body_summary": "The issuer source reports an 80,000-point offer.",
        "lifecycle_state": "published", "scheduled_for": None, "external_source_id": "story-1",
    })
    campaign = store.insert("campaigns", {
        "brand_id": brand["id"], "source_id": source["id"],
        "name": "Offer coverage", "objective": "Explain the source", "status": "draft",
    })
    experiments = ExperimentStore(database)
    experiment = experiments.draft(
        brand_id=brand["id"], campaign_id=campaign["id"],
        hypothesis="An explicit source-update frame earns more clicks.", metric="clicks",
        guardrails={"min_observations_per_variant": 1, "min_impressions_per_variant": 10},
        measurement_windows=[{
            "window_key": "launch", "opens_at": "2026-09-02T10:00:00Z",
            "closes_at": "2026-09-02T13:00:00Z",
            "evaluate_at": "2026-09-02T13:00:00Z",
            "late_evidence_until": "2026-09-03T13:00:00Z",
        }],
    )
    return brand, campaign, experiments, experiment


def add_evidence(brand, variant, *, account, key, impressions, clicks):
    store.record_connector_event(
        account["id"], "metrics", key, "metric_observed",
        {"post_id": variant["post_id"], "impressions": impressions, "clicks": clicks},
        "2026-09-02T12:00:00Z",
    )
    return store.insert("performance_records", {
        "brand_id": brand["id"], "post_id": variant["post_id"], "source_id": None,
        "channel": "x", "observed_at": "2026-09-02T12:00:00Z",
        "impressions": impressions, "clicks": clicks, "engagements": 0,
        "conversions": 0, "revenue_cents": 0,
        "notes": f"connector_event={account['id']}:{key}",
        "created_at": "2026-09-02T12:00:00Z",
    })


def test_drafts_two_deterministic_source_grounded_variants_without_authority(tmp_path):
    brand, campaign, experiments, experiment = setup_experiment(tmp_path)
    assert experiment["status"] == "active"
    assert experiment["metric"] == "clicks"
    assert {item["variant_key"] for item in experiment["variants"]} == {"control", "source_update"}
    for variant in experiment["variants"]:
        assert variant["post_status"] == "draft"
        assert variant["scheduled_for"] is None
        assert variant["external_post_id"] is None
        assert "80,000-point offer" in variant["body"]
        assert variant["body"].endswith("https://issuer.test/offer")
        assert len(variant["body"]) <= 280
    with pytest.raises(ExperimentError, match="already has an active"):
        experiments.draft(
            brand_id=brand["id"], campaign_id=campaign["id"], hypothesis="Another", metric="clicks",
        )


def test_only_connector_backed_performance_can_recommend_and_chris_must_accept(tmp_path):
    brand, _, experiments, experiment = setup_experiment(tmp_path)
    account = store.upsert_connector_account(
        brand["id"], "x", "metrics", "X metrics", status="healthy", scopes=["tweet.read"],
    )
    variants = experiment["variants"]
    # A manually inserted performance row is intentionally ignored.
    store.insert("performance_records", {
        "brand_id": brand["id"], "post_id": variants[0]["post_id"], "source_id": None,
        "channel": "x", "observed_at": "2026-09-02T11:00:00Z",
        "impressions": 1000, "clicks": 999, "engagements": 0, "conversions": 0,
        "revenue_cents": 0, "notes": "manual claim",
    })
    window_id = experiment["measurement_windows"][0]["id"]
    insufficient = experiments.evaluate_window(window_id, as_of="2026-09-02T13:00:00Z")
    assert insufficient["evidence_state"] == "missing"
    assert insufficient["recommendation_id"] is None

    store.record_connector_event(
        account["id"], "metrics", "mismatched", "metric_observed",
        {"post_id": variants[0]["post_id"], "impressions": 10, "clicks": 1},
        "2026-09-02T11:30:00Z",
    )
    store.insert("performance_records", {
        "brand_id": brand["id"], "post_id": variants[0]["post_id"], "source_id": None,
        "channel": "x", "observed_at": "2026-09-02T11:30:00Z",
        "impressions": 10, "clicks": 500, "engagements": 0, "conversions": 0,
        "revenue_cents": 0, "notes": f"connector_event={account['id']}:mismatched",
        "created_at": "2026-09-02T11:30:00Z",
    })
    assert experiments.evaluate_window(
        window_id, as_of="2026-09-02T13:05:00Z",
    )["evidence_state"] == "missing"

    add_evidence(brand, variants[0], account=account, key="control-metrics", impressions=100, clicks=4)
    add_evidence(brand, variants[1], account=account, key="challenger-metrics", impressions=100, clicks=9)
    evaluated = experiments.evaluate_window(window_id, as_of="2026-09-02T13:10:00Z")
    recommendation = next(
        item for item in experiments.get(experiment["id"])["recommendations"]
        if item["id"] == evaluated["recommendation_id"]
    )
    assert recommendation["status"] == "recommended"
    assert recommendation["winner_variant_id"] == variants[1]["id"]
    assert sum(len(item["performance_record_ids"]) for item in recommendation["evidence"]) == 2

    add_evidence(
        brand, variants[1], account=account, key="challenger-metrics-2", impressions=50, clicks=1,
    )
    current_recommendation = experiments.recommend(experiment["id"])
    assert current_recommendation["id"] == recommendation["id"]
    with pytest.raises(PermissionError, match="authenticated human"):
        experiments.accept(experiment["id"], current_recommendation["id"], actor="agent")

    accepted = experiments.accept(
        experiment["id"], current_recommendation["id"], actor="preview-operator",
    )
    replayed = experiments.accept(
        experiment["id"], current_recommendation["id"], actor="preview-operator",
    )
    assert replayed["accepted_recommendation_id"] == accepted["accepted_recommendation_id"]
    assert replayed["recommendations"][0]["learning_id"] == accepted["recommendations"][0]["learning_id"]
    with pytest.raises(ExperimentError, match="different accepted recommendation"):
        experiments.accept(experiment["id"], "different-recommendation", actor="preview-operator")
    assert accepted["status"] == "completed"
    assert accepted["recommendations"][0]["status"] == "accepted"
    assert all(window["status"] in {"evaluated", "closed"} for window in accepted["measurement_windows"])
    learning = store.row("SELECT * FROM brand_learnings WHERE id=?", (accepted["recommendations"][0]["learning_id"],))
    assert learning["status"] == "proposed"
    assert learning["active"] == 0
    assert learning["reviewed_at"] is None
    assert learning["scope_json"] == '{"channel":"x"}'
    assert store.row(
        "SELECT action,actor FROM brand_learning_audit WHERE learning_id=?", (learning["id"],),
    ) == {"action": "proposed", "actor": "preview-operator"}
    assert BrandLearningEngine(experiments.database).retrieve(
        brand["id"], {"channel": "x"}, audit=False,
    )["learnings"] == []
    assert {row["status"] for row in store.rows(
        "SELECT p.status FROM posts p JOIN content_experiment_variants v ON v.post_id=p.id WHERE v.experiment_id=?",
        (experiment["id"],),
    )} == {"draft"}


def test_tie_and_stale_recommendation_cannot_be_accepted(tmp_path):
    brand, _, experiments, experiment = setup_experiment(tmp_path)
    account = store.upsert_connector_account(brand["id"], "x", "metrics", "X", status="healthy")
    for index, variant in enumerate(experiment["variants"]):
        add_evidence(brand, variant, account=account, key=f"tie-{index}", impressions=50, clicks=5)
    window = experiments.evaluate_window(
        experiment["measurement_windows"][0]["id"], as_of="2026-09-02T13:00:00Z",
    )
    tied = next(
        item for item in experiments.get(experiment["id"])["recommendations"]
        if item["id"] == window["recommendation_id"]
    )
    assert tied["status"] == "tie"
    assert window["status"] == "awaiting_evidence"
    assert window["evidence_state"] == "tie"
    with pytest.raises(ExperimentError, match="evidence-backed"):
        experiments.accept(experiment["id"], tied["id"], actor="preview-operator")
