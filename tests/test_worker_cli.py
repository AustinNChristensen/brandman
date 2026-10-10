from __future__ import annotations

from datetime import UTC, datetime
import json

from cryptography.fernet import Fernet

from brandman import store
from brandman.beehiiv_assisted_pull import (
    ASSISTED_BEEHIIV_PULL_JOB_TYPE,
    BeehiivAssistedPullStore,
)
from brandman.experiments import (
    EXPERIMENT_WINDOW_COLLECT_JOB_TYPE, EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE,
)
from brandman.worker_cli import build_secretless_assisted_runtime, main


def test_bounded_worker_cli_reports_safe_empty_run(tmp_path, monkeypatch, capsys):
    store.DATA_PATH = tmp_path / "worker.db"
    store.init_db()
    monkeypatch.setenv("BRANDMAN_CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("BRANDMAN_WORKER_MAX_JOBS", "2")
    main()
    output = json.loads(capsys.readouterr().out)
    assert output["run"]["processed"] == 0
    assert output["configuration"]["read_connector_account_ids"] == []


def test_worker_cli_rejects_unbounded_job_count(monkeypatch):
    monkeypatch.setenv("BRANDMAN_WORKER_MAX_JOBS", "1001")
    try:
        main()
    except SystemExit as error:
        assert "between 1 and 1000" in str(error)
    else:
        raise AssertionError("worker should reject an unsafe bound")


def test_secretless_assisted_worker_composes_public_rss_only(tmp_path, monkeypatch, capsys):
    database = tmp_path / "assisted-worker.db"
    store.DATA_PATH = database
    store.init_db()
    brand = store.get_brand("demo-brand")
    rss = store.upsert_connector_account(
        brand["id"], "rss", "https://example.test/feed", "Public feed",
        status="healthy", scopes=[], capabilities=["content.read"],
    )
    runtime, _, configuration = build_secretless_assisted_runtime(
        "test-worker", database, transport_factory=lambda _account: object(),
    )
    assert configuration["mode"] == "assisted_secretless"
    assert configuration["read_connector_account_ids"] == [rss["id"]]
    assert rss["id"] in runtime.connectors
    assert {
        ASSISTED_BEEHIIV_PULL_JOB_TYPE,
        EXPERIMENT_WINDOW_COLLECT_JOB_TYPE,
        EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE,
    } <= set(runtime.worker.handlers)


def test_worker_cli_runs_without_master_key_in_assisted_mode(tmp_path, monkeypatch, capsys):
    store.DATA_PATH = tmp_path / "secretless-cli.db"
    monkeypatch.delenv("BRANDMAN_CREDENTIAL_MASTER_KEY", raising=False)
    monkeypatch.setenv("BRANDMAN_WORKER_MAX_JOBS", "2")
    main()
    output = json.loads(capsys.readouterr().out)
    assert output["configuration"]["mode"] == "assisted_secretless"


def test_secretless_assisted_worker_processes_beehiiv_pull_requests(tmp_path, monkeypatch):
    database = tmp_path / "assisted-pull-worker.db"
    store.DATA_PATH = database
    store.init_db()
    brand = store.get_brand("demo-brand")
    account = store.upsert_connector_account(
        brand["id"], "beehiiv", "pub_demo", "DemoBrand Beehiiv",
        status="connected", capabilities=["browser.assisted", "beehiiv_read"],
        configuration={
            "delivery_mode": "browser_assisted",
            "connection_role": "beehiiv_read",
        },
    )
    scheduled_for = datetime(2026, 9, 3, 4, 8, tzinfo=UTC).isoformat()
    job = store.enqueue_job(
        ASSISTED_BEEHIIV_PULL_JOB_TYPE,
        "scheduled-assisted-pull",
        {
            "brand_id": brand["id"],
            "connector_account_id": account["id"],
            "scheduled_for": scheduled_for,
        },
        brand_id=brand["id"],
        connector_account_id=account["id"],
        run_after=scheduled_for,
    )

    runtime, _, _ = build_secretless_assisted_runtime("test-worker", database)
    completed = runtime.worker.run_once(as_of=job["run_after"])

    assert completed["status"] == "completed"
    tasks = BeehiivAssistedPullStore(database).list(brand["id"])
    assert len(tasks) == 1
    assert tasks[0]["status"] == "pending"
    assert tasks[0]["read_only"] is True
