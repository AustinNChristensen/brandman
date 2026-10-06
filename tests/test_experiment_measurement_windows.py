from __future__ import annotations

import sqlite3

import pytest

from app import store
from app.experiments import (
    EXPERIMENT_WINDOW_COLLECT_JOB_TYPE,
    EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE,
    ExperimentError,
    ExperimentStore,
    make_experiment_window_collection_handler,
    make_experiment_window_evaluation_handler,
)
from app.jobs import JobWorker


def _setup(tmp_path, *, windows=None):
    database = tmp_path / "measurement-windows.db"
    store.DATA_PATH = database
    store.init_db()
    brand = store.get_brand("demo-brand")
    source = store.insert("sources", {
        "brand_id": brand["id"], "title": "Issuer update",
        "url": "https://issuer.test/update", "source_type": "rss",
        "body_summary": "The issuer reports an updated offer.",
        "lifecycle_state": "published", "scheduled_for": None,
        "external_source_id": "issuer-update",
    })
    campaign = store.insert("campaigns", {
        "brand_id": brand["id"], "source_id": source["id"], "name": "Offer test",
        "objective": "Test framing", "status": "draft",
    })
    experiments = ExperimentStore(database)
    experiment = experiments.draft(
        brand_id=brand["id"], campaign_id=campaign["id"],
        hypothesis="The challenger earns more clicks.", metric="clicks",
        guardrails={"min_observations_per_variant": 2, "min_impressions_per_variant": 10},
        measurement_windows=windows or [{
            "window_key": "24h", "opens_at": "2026-09-01T00:00:00Z",
            "closes_at": "2026-09-01T23:00:00Z",
            "evaluate_at": "2026-09-01T23:00:00Z",
            "late_evidence_until": "2026-09-02T12:00:00Z",
            "retry_interval_seconds": 900, "freshness_seconds": 3600,
        }],
    )
    return database, brand, experiments, experiment


def _metric(brand, variant, account, key, *, observed_at, impressions, clicks):
    store.record_connector_event(
        account["id"], "metrics", key, "metric_observed",
        {"post_id": variant["post_id"], "impressions": impressions, "clicks": clicks},
        observed_at,
    )
    return store.insert("performance_records", {
        "brand_id": brand["id"], "post_id": variant["post_id"], "source_id": None,
        "channel": "x", "observed_at": observed_at, "impressions": impressions,
        "clicks": clicks, "engagements": 0, "conversions": 0, "revenue_cents": 0,
        "notes": f"connector_event={account['id']}:{key}",
    })


def test_draft_persists_declared_window_and_restart_safe_collection_jobs(tmp_path):
    database, brand, experiments, experiment = _setup(tmp_path)
    window = experiment["measurement_windows"][0]
    assert window["metric"] == "clicks"
    assert window["status"] == "scheduled"
    initial = store.row(
        "SELECT * FROM durable_jobs WHERE job_type=?",
        (EXPERIMENT_WINDOW_COLLECT_JOB_TYPE,),
    )
    assert initial["run_after"] == "2026-09-01T23:00:00+00:00"
    with sqlite3.connect(database) as connection, pytest.raises(
        sqlite3.IntegrityError, match="definition is immutable",
    ):
        connection.execute(
            "UPDATE content_experiment_measurement_windows SET metric='impressions' WHERE id=?",
            (window["id"],),
        )

    account = store.upsert_connector_account(
        brand["id"], "x", "read", "X read", status="healthy", scopes=["tweet.read"],
    )
    other_brand = store.get_brand("demo-personal")
    other_account = store.upsert_connector_account(
        other_brand["id"], "x", "read", "Demo X read", status="healthy",
        scopes=["tweet.read"],
    )
    first = experiments.record_collection_request(
        window["id"], collection_round=1,
        connector_account_ids=[account["id"], other_account["id"]],
        as_of="2026-09-01T23:05:00Z",
    )
    restarted = ExperimentStore(database).record_collection_request(
        window["id"], collection_round=1, connector_account_ids=[account["id"]],
        as_of="2026-09-01T23:06:00Z",
    )
    assert first["collection_round"] == restarted["collection_round"] == 1
    assert len([event for event in restarted["events"] if event["event_type"] == "collection_requested"]) == 1
    assert len(store.rows(
        "SELECT id FROM durable_jobs WHERE idempotency_key LIKE ?",
        (f"experiment-window:{window['id']}:collect:1%",),
    )) == 2  # one window collection plus one connector sync
    assert store.row(
        "SELECT id FROM durable_jobs WHERE connector_account_id=? AND idempotency_key LIKE ?",
        (other_account["id"], f"experiment-window:{window['id']}:%"),
    ) is None
    assert store.row(
        "SELECT id FROM durable_jobs WHERE job_type=? AND idempotency_key LIKE ?",
        (EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE, f"experiment-window:{window['id']}:evaluate:1"),
    )


def test_window_uses_latest_cumulative_snapshot_and_marks_late_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "now", lambda: "2026-09-02T00:30:00+00:00")
    _, brand, experiments, experiment = _setup(tmp_path)
    account = store.upsert_connector_account(
        brand["id"], "x", "metrics", "X metrics", status="healthy",
    )
    for index, variant in enumerate(experiment["variants"]):
        _metric(brand, variant, account, f"{index}-1", observed_at="2026-09-01T22:15:00Z",
                impressions=10, clicks=1 + index)
        _metric(brand, variant, account, f"{index}-2", observed_at="2026-09-01T22:45:00Z",
                impressions=100, clicks=4 + index * 5)
    window = experiments.evaluate_window(
        experiment["measurement_windows"][0]["id"], as_of="2026-09-02T01:00:00Z",
    )
    assert window["status"] == "evaluated"
    assert window["evidence_state"] == "sufficient"
    assert window["has_late_evidence"] is True
    assert [item["impressions"] for item in window["evidence"]] == [100, 100]
    assert [item["metric_value"] for item in window["evidence"]] == [4, 9]
    recommendation = next(
        item for item in experiments.get(experiment["id"])["recommendations"]
        if item["id"] == window["recommendation_id"]
    )
    assert recommendation["measurement_window_id"] == window["id"]
    assert recommendation["status"] == "recommended"
    assert all(item["post_status"] == "draft" for item in experiments.get(experiment["id"])["variants"])


def test_window_rejects_relabelled_connector_observation_time(tmp_path):
    _, brand, experiments, experiment = _setup(tmp_path)
    account = store.upsert_connector_account(
        brand["id"], "x", "metrics", "X metrics", status="healthy",
    )
    for index, variant in enumerate(experiment["variants"]):
        key = f"forged-time-{index}"
        store.record_connector_event(
            account["id"], "metrics", key, "metric_observed",
            {"post_id": variant["post_id"], "impressions": 100, "clicks": 9},
            "2026-08-31T12:00:00Z",
        )
        store.insert("performance_records", {
            "brand_id": brand["id"], "post_id": variant["post_id"], "source_id": None,
            "channel": "x", "observed_at": "2026-09-01T22:45:00Z",
            "impressions": 100, "clicks": 9, "engagements": 0, "conversions": 0,
            "revenue_cents": 0, "notes": f"connector_event={account['id']}:{key}",
        })
    window = experiments.evaluate_window(
        experiment["measurement_windows"][0]["id"], as_of="2026-09-02T12:00:00Z",
    )
    assert window["status"] == "closed"
    assert window["evidence_state"] == "missing"
    assert all(item["observation_count"] == 0 for item in window["evidence"])
    assert window["recommendation_id"] is None


def test_latest_snapshot_is_chosen_by_normalized_instant_not_timestamp_text(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "now", lambda: "2026-09-02T00:30:00+00:00")
    _, brand, experiments, experiment = _setup(tmp_path)
    account = store.upsert_connector_account(
        brand["id"], "x", "metrics", "X metrics", status="healthy",
    )
    for index, variant in enumerate(experiment["variants"]):
        _metric(
            brand, variant, account, f"offset-{index}-earlier",
            observed_at="2026-09-01T23:30:00+01:00", impressions=50, clicks=4 + index,
        )
        _metric(
            brand, variant, account, f"offset-{index}-later",
            observed_at="2026-09-01T22:45:00Z", impressions=100, clicks=8 + index,
        )
    window = experiments.evaluate_window(
        experiment["measurement_windows"][0]["id"], as_of="2026-09-02T01:00:00Z",
    )
    assert window["status"] == "evaluated"
    assert [item["metric_value"] for item in window["evidence"]] == [8, 9]
    assert all(item["latest_observed_at"] == "2026-09-01T22:45:00Z" for item in window["evidence"])


def test_missing_partial_and_stale_windows_retry_then_close_visibly(tmp_path):
    _, brand, experiments, experiment = _setup(tmp_path)
    window_id = experiment["measurement_windows"][0]["id"]
    account = store.upsert_connector_account(
        brand["id"], "x", "metrics", "X metrics", status="healthy",
    )
    _metric(
        brand, experiment["variants"][0], account, "old", observed_at="2026-08-31T20:00:00Z",
        impressions=100, clicks=3,
    )
    waiting = experiments.evaluate_window(window_id, as_of="2026-09-01T23:10:00Z")
    assert waiting["status"] == "awaiting_evidence"
    assert waiting["evidence_state"] == "stale"
    retry = store.row(
        "SELECT * FROM durable_jobs WHERE idempotency_key=?",
        (f"experiment-window:{window_id}:collect:2",),
    )
    assert retry
    assert retry["run_after"] == "2026-09-01T23:25:00+00:00"
    closed = experiments.evaluate_window(window_id, as_of="2026-09-02T12:00:00Z")
    assert closed["status"] == "closed"
    assert closed["evidence_state"] == "stale"
    assert closed["recommendation_id"] is None
    feedback = store.rows(
        "SELECT * FROM product_feedback WHERE component='measurement.freshness'"
    )
    assert feedback


def test_window_validation_fails_closed(tmp_path):
    with pytest.raises(ExperimentError, match="timestamps must satisfy"):
        _setup(tmp_path, windows=[{
            "window_key": "bad", "opens_at": "2026-09-02T00:00:00Z",
            "closes_at": "2026-09-01T00:00:00Z",
        }])
    with pytest.raises(ExperimentError, match="timezone"):
        _setup(tmp_path / "second", windows=[{
            "window_key": "bad", "opens_at": "2026-09-01T00:00:00",
            "closes_at": "2026-09-02T00:00:00Z",
        }])
    _, _, experiments, experiment = _setup(tmp_path / "third")
    with pytest.raises(ExperimentError, match="before evaluate_at"):
        experiments.evaluate_window(
            experiment["measurement_windows"][0]["id"], as_of="2026-09-01T22:59:59Z",
        )
    assert experiment["measurement_windows"][0]["status"] == "scheduled"


def test_evidence_persisted_after_late_cutoff_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "now", lambda: "2026-09-02T08:00:00+00:00")
    _, brand, experiments, experiment = _setup(tmp_path, windows=[{
        "window_key": "expired", "opens_at": "2026-09-01T00:00:00Z",
        "closes_at": "2026-09-01T23:00:00Z", "evaluate_at": "2026-09-01T23:00:00Z",
        "late_evidence_until": "2026-09-02T07:00:00Z", "freshness_seconds": 3600,
    }])
    account = store.upsert_connector_account(
        brand["id"], "x", "metrics", "X metrics", status="healthy",
    )
    for index, variant in enumerate(experiment["variants"]):
        for observation in range(2):
            _metric(
                brand, variant, account, f"too-late-{index}-{observation}",
                observed_at=f"2026-09-01T22:{30 + observation:02d}:00Z",
                impressions=100, clicks=5 + index,
            )
    window = experiments.evaluate_window(
        experiment["measurement_windows"][0]["id"], as_of="2026-09-02T08:00:00Z",
    )
    assert window["status"] == "closed"
    assert window["evidence_state"] == "late_rejected"
    assert window["has_late_evidence"] is False
    assert window["recommendation_id"] is None


def test_handlers_are_internal_only_and_idempotently_finish_terminal_window(tmp_path):
    database, brand, experiments, experiment = _setup(tmp_path)
    window_id = experiment["measurement_windows"][0]["id"]
    worker = JobWorker("measurement-worker")
    worker.register(
        EXPERIMENT_WINDOW_COLLECT_JOB_TYPE,
        make_experiment_window_collection_handler(database, ()),
    )
    worker.register(
        EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE,
        make_experiment_window_evaluation_handler(database),
    )
    collected = worker.run_once(as_of="2026-09-01T23:00:00+00:00")
    assert collected["status"] == "completed"
    evaluated = worker.run_once(as_of="2026-09-02T12:00:00+00:00")
    assert evaluated["status"] == "completed"
    assert experiments.get_window(window_id)["status"] == "closed"
    posts = store.rows(
        "SELECT p.status,p.scheduled_for,p.external_post_id FROM posts p JOIN content_experiment_variants v ON v.post_id=p.id WHERE v.experiment_id=?",
        (experiment["id"],),
    )
    assert all(row["status"] == "draft" for row in posts)
    assert all(row["scheduled_for"] is None and row["external_post_id"] is None for row in posts)


def test_handlers_fail_closed_on_cross_tenant_or_experiment_binding(tmp_path):
    database, brand, experiments, experiment = _setup(tmp_path)
    window = experiment["measurement_windows"][0]
    other_brand = store.get_brand("demo-personal")
    collect = make_experiment_window_collection_handler(database, ())
    evaluate = make_experiment_window_evaluation_handler(database)
    initial_jobs = store.rows("SELECT id FROM durable_jobs ORDER BY id")
    before = experiments.get_window(window["id"])
    with pytest.raises(ExperimentError, match="resource binding"):
        collect({
            "brand_id": other_brand["id"], "locked_at": "2026-09-01T23:00:00Z",
            "payload": {"experiment_id": experiment["id"],
                        "measurement_window_id": window["id"], "collection_round": 1},
        })
    with pytest.raises(ExperimentError, match="resource binding"):
        evaluate({
            "brand_id": brand["id"], "locked_at": "2026-09-01T23:00:00Z",
            "payload": {"experiment_id": "another-experiment",
                        "measurement_window_id": window["id"], "collection_round": 1},
        })
    after = experiments.get_window(window["id"])
    assert after["status"] == before["status"] == "scheduled"
    assert after["events"] == before["events"] == []
    assert store.rows("SELECT id FROM durable_jobs ORDER BY id") == initial_jobs
