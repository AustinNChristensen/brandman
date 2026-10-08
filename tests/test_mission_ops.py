from __future__ import annotations

import json

import pytest

from brandman.mission_ops import (
    AttributionConfidence,
    attribution_confidence,
    build_end_of_day_scorecard,
    build_morning_plan,
    build_utm_identity,
    calculate_goal_trajectory,
    enrich_mission_progress,
    generate_utm_url,
    parse_utm_identity,
)


def mission_progress() -> dict:
    return {
        "id": "mission-1",
        "name": "30-day growth",
        "as_of": "2026-09-11T08:00:00-06:00",
        "remaining_days": 20,
        "goals": [
            {"metric": "x_followers", "baseline": 8, "current": 45, "expected_current": 38.6667,
             "target": 100, "direction": "increase"},
            {"metric": "active_subscribers", "baseline": 13, "current": 15, "expected_current": 17,
             "target": 25, "direction": "increase"},
        ],
    }


def test_goal_trajectory_is_explicit_and_direction_aware() -> None:
    ahead = calculate_goal_trajectory(mission_progress()["goals"][0], remaining_days=20)
    assert ahead["status"] == "ahead"
    assert ahead["status_text"] == "Ahead of pace by 6.3333"
    assert ahead["trajectory_amount"] == 6.3333
    assert ahead["required_daily_change"] == 2.75

    decreasing = calculate_goal_trajectory(
        {"metric": "churn", "current": 8, "expected_current": 10, "target": 5, "direction": "decrease"},
        remaining_days=6,
    )
    assert decreasing["status"] == "ahead"
    assert decreasing["trajectory_amount"] == 2
    assert decreasing["required_daily_change"] == 0.5


def test_enrichment_does_not_mutate_progress() -> None:
    progress = mission_progress()
    enriched = enrich_mission_progress(progress)
    assert "status" not in progress["goals"][0]
    assert enriched["goals"][0]["status"] == "ahead"
    assert enriched["goals"][1]["status"] == "behind"
    assert enriched["goals"][1]["status_text"] == "Behind pace by 2"


def test_morning_plan_and_scorecard_are_persistable() -> None:
    progress = mission_progress()
    plan = build_morning_plan(progress, plan_date="2026-09-11")
    assert plan["kind"] == "morning_plan"
    assert plan["goals"][0]["required_today"] == 2.75
    assert plan["goals"][0]["end_of_day_target"] == 47.75
    assert plan["goals"][1]["trajectory_status"] == "behind"

    previous = {"goals": [{"metric": "x_followers", "current": 42}, {"metric": "active_subscribers", "current": 14}]}
    scorecard = build_end_of_day_scorecard(progress, previous_progress=previous, scorecard_date="2026-09-11")
    assert scorecard["overall_status"] == "behind"
    assert scorecard["goals"][0]["daily_change"] == 3
    assert scorecard["goals"][1]["daily_change"] == 1
    json.dumps(plan)
    json.dumps(scorecard)


def test_utm_round_trip_is_canonical_and_replaces_stale_tracking() -> None:
    identity = build_utm_identity(
        source="X", medium="Social", campaign_id="campaign-9", artifact_id="post-4",
        cta_id="newsletter-signup", brand_slug="Demo-Brand",
    )
    url = generate_utm_url(
        "HTTPS://demo.example/join?b=2&utm_campaign=stale&a=1#old", identity,
    )
    assert url == (
        "https://demo.example/join?a=1&b=2&utm_source=x&utm_medium=social&"
        "utm_campaign=campaign-9&utm_content=post-4&utm_cta=newsletter-signup&utm_brand=demo-brand"
    )
    assert parse_utm_identity(url) == {
        "source": "x", "medium": "social", "campaign_id": "campaign-9", "artifact_id": "post-4",
        "cta_id": "newsletter-signup", "brand_slug": "demo-brand", "confidence": "direct",
    }


def test_attribution_confidence_states() -> None:
    assert attribution_confidence({"campaign_id": "c", "artifact_id": "a", "cta_id": "cta"}) is AttributionConfidence.DIRECT
    assert attribution_confidence({"campaign_id": "c"}) is AttributionConfidence.ASSISTED
    assert attribution_confidence({}) is AttributionConfidence.UNATTRIBUTED
    assert parse_utm_identity("https://example.test/?utm_source=x")["confidence"] == "assisted"
    assert parse_utm_identity("https://example.test/")["confidence"] == "unattributed"


def test_invalid_utm_identity_and_url_are_rejected() -> None:
    with pytest.raises(ValueError, match="campaign_id"):
        build_utm_identity(source="x", medium="social", campaign_id="spaces are bad", artifact_id="a", cta_id="c")
    identity = build_utm_identity(source="x", medium="social", campaign_id="c", artifact_id="a", cta_id="cta")
    with pytest.raises(ValueError, match="absolute"):
        generate_utm_url("/relative", identity)
