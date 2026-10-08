from pathlib import Path

from brandman import store
from brandman.connectors import ConnectorEvent, ConnectorKind, ConnectorResult, EventKind
from brandman.editorial import EditorialStore
from brandman.learning_engine import BrandLearningEngine
from brandman.source_campaign import SourceCampaignOperator, semantic_cluster, source_grounded_x_draft
from brandman.sync import SyncOrchestrator, make_sync_job_handler


def setup_operator(tmp_path: Path):
    store.DATA_PATH = tmp_path / "source-campaign.db"
    store.init_db()
    brand = store.get_brand("demo-brand")
    store.ensure_demo_brand_growth_mission()
    editorial = EditorialStore(store.DATA_PATH)
    return brand, SourceCampaignOperator(editorial)


def event(
    connector: ConnectorKind = ConnectorKind.RSS,
    *,
    key: str = "story-1",
    url: str | None = "https://example.test/deal?utm_source=feed",
    title: str = "A pricing deal changed",
    summary: str = "The source says the offer is now 80,000 credits.",
) -> ConnectorEvent:
    return ConnectorEvent(
        connector=connector,
        kind=EventKind.SOURCE_ITEM,
        dedup_key=f"{connector.value}:{key}",
        occurred_at="2026-09-02T12:00:00+00:00",
        external_id=key,
        payload={
            "title": title,
            "summary": summary,
            "url": url,
            "published_at": "2026-09-02T11:00:00+00:00",
            "content_fingerprint": f"fingerprint-{key}",
            "status": "published",
        },
    )


def apply(operator, brand, connector_event, *, account_key="primary"):
    account = store.upsert_connector_account(
        brand["id"], connector_event.connector.value, account_key, account_key,
        status="healthy",
    )
    outcome = SyncOrchestrator(source_campaign_operator=operator).apply_result(
        ConnectorResult((connector_event,)),
        connector_kind=connector_event.connector,
        brand_id=brand["id"],
        connector_account_id=account["id"],
        stream="content",
    )
    return account, outcome


def test_rss_event_creates_source_grounded_candidate_campaign_and_draft(tmp_path):
    brand, operator = setup_operator(tmp_path)
    account, outcome = apply(operator, brand, event())

    assert outcome.editorial_candidates_projected == 1
    assert outcome.campaigns_projected == 1
    assert outcome.post_drafts_projected == 1
    candidate = operator.editorial.list_candidates(brand["id"])[0]
    campaign = store.row("SELECT * FROM campaigns")
    post = store.row("SELECT * FROM posts")
    projection = store.row("SELECT * FROM source_campaign_projections")
    evidence = store.row("SELECT * FROM source_campaign_evidence")

    assert candidate["recommended_treatment"] == "evaluate_for_coverage"
    assert candidate["summary"] == "The source says the offer is now 80,000 credits."
    assert candidate["supporting_sources"][0]["connector"] == "rss"
    assert candidate["supporting_sources"][0]["connector_account_id"] == account["id"]
    assert candidate["supporting_sources"][0]["connector_event_id"] == evidence["connector_event_id"]
    assert candidate["supporting_sources"][0]["authority_type"] == "third_party"
    assert campaign["source_id"] == projection["source_id"]
    assert campaign["status"] == "draft"
    assert post["campaign_id"] == campaign["id"]
    assert post["status"] == "draft"
    assert post["scheduled_for"] is None
    assert post["external_post_id"] is None
    assert post["body"] == (
        "From the source: A pricing deal changed — "
        "The source says the offer is now 80,000 credits.\nhttps://example.test/deal"
    )


def test_replay_is_idempotent_and_reports_no_new_projection(tmp_path):
    brand, operator = setup_operator(tmp_path)
    connector_event = event()
    apply(operator, brand, connector_event)
    _, replay = apply(operator, brand, connector_event)

    assert replay.recorded == 0
    assert replay.editorial_candidates_projected == 0
    assert replay.campaigns_projected == 0
    assert replay.post_drafts_projected == 0
    assert len(operator.editorial.list_candidates(brand["id"])) == 1
    assert len(store.rows("SELECT * FROM campaigns")) == 1
    assert len(store.rows("SELECT * FROM posts")) == 1
    assert len(store.rows("SELECT * FROM source_campaign_evidence")) == 1


def test_same_canonical_url_across_third_party_accounts_deduplicates_and_keeps_evidence(tmp_path):
    brand, operator = setup_operator(tmp_path)
    apply(operator, brand, event(key="doc", url="https://example.test/deal?utm_source=doc"), account_key="doc")
    apply(operator, brand, event(key="tpg", url="https://example.test/deal?utm_source=tpg"), account_key="tpg")

    candidates = operator.editorial.list_candidates(brand["id"])
    assert len(candidates) == 1
    assert len(candidates[0]["supporting_sources"]) == 2
    assert len(store.rows("SELECT * FROM campaigns")) == 1
    assert len(store.rows("SELECT * FROM posts")) == 1
    assert len(store.rows("SELECT * FROM source_campaign_evidence")) == 2


def test_beehiiv_event_is_owned_but_still_only_creates_a_review_draft(tmp_path):
    brand, operator = setup_operator(tmp_path)
    _, outcome = apply(operator, brand, event(ConnectorKind.BEEHIIV))

    candidate = operator.editorial.list_candidates(brand["id"])[0]
    post = store.row("SELECT * FROM posts")
    assert outcome.post_drafts_projected == 1
    assert candidate["recommended_treatment"] == "amplify_owned_post"
    assert candidate["supporting_sources"][0]["authority_type"] == "owned"
    assert post["body"].startswith("New from DemoBrand: ")
    assert post["status"] == "draft"


def test_long_or_missing_source_fields_stay_within_x_limit(tmp_path):
    long = source_grounded_x_draft(
        title="T" * 400,
        summary="S" * 400,
        url="https://example.test/story",
        owned=False,
    )
    no_url = source_grounded_x_draft(
        title="A title", summary="A summary", url=None, owned=False,
    )
    assert len(long) == 280
    assert long.endswith("\nhttps://example.test/story")
    assert no_url == "From the source: A title — A summary"


def test_sync_job_handler_accepts_injected_source_operator(tmp_path):
    brand, operator = setup_operator(tmp_path)
    account = store.upsert_connector_account(
        brand["id"], "rss", "handler", "Handler", status="healthy"
    )

    class Connector:
        kind = ConnectorKind.RSS

        def sync(self, cursor=None):
            return ConnectorResult((event(key="handler"),))

    handler = make_sync_job_handler(
        {account["id"]: Connector()}, source_campaign_operator=operator,
    )
    result = handler(
        {"payload": {
            "brand_id": brand["id"],
            "connector_account_id": account["id"],
            "stream": "feed",
        }}
    )
    assert result["editorial_candidates_projected"] == 1
    assert result["campaigns_projected"] == 1
    assert result["post_drafts_projected"] == 1


def test_batch_ingests_all_intelligence_but_promotes_clustered_top_three(tmp_path):
    brand, operator = setup_operator(tmp_path)
    account = store.upsert_connector_account(
        brand["id"], "rss", "batch", "Example Newsletter", status="healthy",
    )
    events = (
        event(key="transfer-1", url="https://example.test/transfer-1", title="New 30% price drop ends September 9", summary="Pricing falls for a limited time."),
        event(key="transfer-2", url="https://example.test/transfer-2", title="A price drop is announced for annual plans", summary="Another report about the price drop."),
        event(key="card", url="https://example.test/card", title="New discount for annual plans", summary="Earn 90,000 credits after minimum spend."),
        event(key="hotel", url="https://example.test/hotel", title="Integration partnership update", summary="New partner rates affect integrations."),
        event(key="unrelated", url="https://example.test/desk", title="Office furniture review", summary="A review of a standing desk."),
    )
    outcome = SyncOrchestrator(source_campaign_operator=operator).apply_result(
        ConnectorResult(events), connector_kind=ConnectorKind.RSS,
        brand_id=brand["id"], connector_account_id=account["id"], stream="content",
    )

    candidates = operator.editorial.list_candidates(brand["id"])
    assert outcome.editorial_candidates_projected == 5
    assert len(candidates) == 5
    assert len(store.rows("SELECT * FROM source_intelligence_records")) == 5
    assert outcome.campaigns_projected <= 3
    assert outcome.post_drafts_projected <= 3
    assert len(store.rows("SELECT * FROM campaigns")) <= 3
    assert len({candidate["score"] for candidate in candidates}) > 1
    assert {candidate["publisher_name"] for candidate in candidates} == {"Example Newsletter"}
    assert any(candidate["cluster_key"] == "pricing-change" for candidate in candidates)
    transfer = next(candidate for candidate in candidates if "30%" in candidate["title"])
    assert transfer["intelligence"]["offers"] == ["30%"]
    assert transfer["intelligence"]["deadlines"] == ["September 9"]
    assert transfer["intelligence"]["terms"] == ["limited time"]
    assert transfer["intelligence"]["authority"]["publisher"] == "Example Newsletter"
    assert transfer["intelligence"]["licensing"]["reuse"] == "source_attribution_required"


def test_replay_does_not_promote_more_backlog(tmp_path):
    brand, operator = setup_operator(tmp_path)
    account = store.upsert_connector_account(brand["id"], "rss", "replay-batch", "Feed", status="healthy")
    events = tuple(event(key=str(index), url=f"https://example.test/{index}", title=f"General update number {index}") for index in range(5))
    orchestrator = SyncOrchestrator(source_campaign_operator=operator)
    orchestrator.apply_result(ConnectorResult(events), connector_kind=ConnectorKind.RSS,
                              brand_id=brand["id"], connector_account_id=account["id"])
    before = len(store.rows("SELECT * FROM campaigns"))
    replay = orchestrator.apply_result(ConnectorResult(events), connector_kind=ConnectorKind.RSS,
                                       brand_id=brand["id"], connector_account_id=account["id"])
    assert replay.campaigns_projected == 0
    assert len(store.rows("SELECT * FROM campaigns")) == before


def test_legacy_fanout_is_preserved_and_backfilled_without_new_work(tmp_path):
    brand, operator = setup_operator(tmp_path)
    apply(operator, brand, event())
    campaign_ids = [row["id"] for row in store.rows("SELECT id FROM campaigns")]
    post_ids = [row["id"] for row in store.rows("SELECT id FROM posts")]
    with store.connection() as connection:
        connection.execute("DELETE FROM source_intelligence_records")
        connection.execute("DELETE FROM source_promotion_audit")
        connection.execute(
            "UPDATE editorial_candidates SET publisher_name='',cluster_key='',intelligence='{}'"
        )

    SourceCampaignOperator(operator.editorial)

    migrated = store.row("SELECT * FROM source_intelligence_records")
    audit = store.row("SELECT * FROM source_promotion_audit")
    candidate = operator.editorial.list_candidates(brand["id"])[0]
    assert migrated["promotion_state"] == "legacy_promoted"
    assert audit["decision"] == "legacy_fanout_preserved"
    assert [row["id"] for row in store.rows("SELECT id FROM campaigns")] == campaign_ids
    assert [row["id"] for row in store.rows("SELECT id FROM posts")] == post_ids
    assert candidate["publisher_name"] == "primary"
    assert candidate["cluster_key"]
    assert candidate["intelligence"]["migration"] == "legacy_fanout_preserved"


def test_legacy_reconciliation_is_dry_run_first_and_fails_closed(tmp_path):
    brand, operator = setup_operator(tmp_path)
    for index, title in enumerate((
        "New pricing change ends soon", "Annual plan discount increased",
        "Integration partnership update", "Feature launch announced",
        "Subscription plan offer increased", "Pricing page update changed",
    )):
        apply(operator, brand, event(key=f"legacy-{index}", url=f"https://example.test/legacy-{index}",
                                     title=title, summary="Earn 80,000 credits for a limited time."),
              account_key=f"legacy-{index}")
    dry_run = operator.reconcile_legacy_fanout(brand["id"])
    assert dry_run["mode"] == "dry_run"
    assert dry_run["keep"] <= 3
    assert dry_run["eligible"] >= 1
    assert all(row["status"] == "draft" for row in store.rows("SELECT status FROM campaigns"))

    protected = next(item for item in dry_run["decisions"] if item["decision"] == "archive_cancel")
    with store.connection() as connection:
        connection.execute("UPDATE posts SET body='Human edit',updated_at=? WHERE id=?",
                           ("2026-09-02T23:00:00+00:00", protected["post_id"]))
    guarded = operator.reconcile_legacy_fanout(brand["id"])
    refused = next(item for item in guarded["decisions"] if item["post_id"] == protected["post_id"])
    assert refused["decision"] == "refuse"

    applied = operator.reconcile_legacy_fanout(brand["id"], apply=True, actor="test")
    assert applied["eligible"] >= 1
    assert store.row("SELECT status FROM posts WHERE id=?", (protected["post_id"],))["status"] == "draft"
    assert len(store.rows("SELECT * FROM legacy_fanout_reconciliation_audit")) == applied["eligible"]
    rerun = operator.reconcile_legacy_fanout(brand["id"], apply=True, actor="test")
    assert rerun["eligible"] == 0


def test_product_family_titles_cluster_by_vendor_line_and_content_type():
    from brandman.source_campaign import SourceVocabulary, configure_source_vocabulary
    configure_source_vocabulary(SourceVocabulary(
        vendors=(("acme", ("acme",)),),
        product_lines=(("cloud", ("acme cloud", "cloud")), ("desk", ("acme desk", "desk"))),
    ))
    try:
        cloud_offers = (
            "Acme Cloud Starter plan: 20% Discount + 3 Months Free",
            "Acme Cloud Team plan: 30% Discount + 6 Months Free",
        )
        desk_reviews = (
            "Acme Desk Basic plan review: Best for small teams",
            "Acme Desk Pro plan review: A solid mid-tier pick",
            "Acme Desk Enterprise product review: Valuable perks without onboarding",
        )
        assert {semantic_cluster(title, "") for title in cloud_offers} == {"offer:acme-cloud-product"}
        assert {semantic_cluster(title, "") for title in desk_reviews} == {"review:acme-desk-product"}
        assert semantic_cluster("Acme is cutting its longest running promotion", "") != "review:acme-desk-product"
    finally:
        configure_source_vocabulary(None)


def test_only_accepted_active_learning_changes_source_candidate_planning(tmp_path):
    brand, operator = setup_operator(tmp_path)
    engine = BrandLearningEngine(store.DATA_PATH)
    learning = engine.propose(brand["id"], hypothesis="Numeric hooks help",
        proposed_change="Prefer source-supported numeric hooks",
        evidence_for=[{"id": "metric-1", "summary": "Clicks improved"}],
        effect={"metric": "clicks", "direction": "increase"},
        uncertainty={"confidence": "low"}, scope={"stage": "source_candidate"},
        review_at="2099-01-01T00:00:00Z", actor="analyst")
    apply(operator, brand, event(key="prior-proposed", url="https://example.test/prior-proposed"))
    assert operator.editorial.list_candidates(brand["id"])[0]["recommended_treatment"] != "evaluate_with_accepted_priors"

    engine.transition(learning["id"], "testing", actor="Chris")
    engine.transition(learning["id"], "accepted", actor="Chris")
    apply(operator, brand, event(key="prior-accepted", url="https://example.test/prior-accepted"))
    accepted = next(item for item in operator.editorial.list_candidates(brand["id"])
                    if item["supporting_sources"][0]["provider_external_id"] == "prior-accepted")
    assert accepted["recommended_treatment"] == "evaluate_with_accepted_priors"
    assert accepted["intelligence"]["learning_context"]["selected_learning_ids"] == [learning["id"]]

    engine.set_active(learning["id"], False, actor="Chris", reason="Revert")
    apply(operator, brand, event(key="prior-disabled", url="https://example.test/prior-disabled"))
    disabled = next(item for item in operator.editorial.list_candidates(brand["id"])
                    if item["supporting_sources"][0]["provider_external_id"] == "prior-disabled")
    assert disabled["recommended_treatment"] == "evaluate_for_coverage"
