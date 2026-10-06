import json
import os
from pathlib import Path
import stat

from cryptography.fernet import Fernet
import pytest

from app import store
from app.bootstrap_cli import PROJECT_ROOT, bootstrap, generate_master_key_file, main


def test_bootstrap_without_secrets_initializes_safe_internal_cycle(tmp_path):
    database = tmp_path / "bootstrap.db"
    report = bootstrap(
        database=database,
        environment={},
        transport_factory=lambda _account: (_ for _ in ()).throw(
            AssertionError("no transport may be built without a key")
        ),
    )

    assert database.exists()
    assert report["status"] == "needs_action"
    assert report["environment"] == {
        "preview_password_configured": False,
        "credential_master_key_configured": False,
    }
    assert report["initialization"]["schedules_discovered"] >= 1
    assert report["initialization"]["tick"]["jobs_enqueued"] >= 1
    assert report["worker"]["jobs_processed"] >= 1
    assert all(
        job["job_type"] == "operating_plan.refresh"
        for job in report["worker"]["jobs"]
    )
    assert any("BRAND_OS_PREVIEW_PASSWORD" in action for action in report["next_actions"])
    assert any("execution agent" in action for action in report["next_actions"])
    assert any(
        "BRAND_OS_CREDENTIAL_MASTER_KEY" in action
        for action in report["optional_api_actions"]
    )


def test_bootstrap_json_never_contains_environment_secret_values(tmp_path, monkeypatch, capsys):
    database = tmp_path / "json.db"
    preview = "preview-sensitive-value"
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", preview)
    monkeypatch.setenv("BRAND_OS_CREDENTIAL_MASTER_KEY", key)

    main(["--database", str(database), "--json", "--max-jobs", "2"])
    output = capsys.readouterr().out
    parsed = json.loads(output)

    assert parsed["environment"]["preview_password_configured"] is True
    assert parsed["environment"]["credential_master_key_configured"] is True
    assert preview not in output
    assert key not in output


def test_explicit_key_generation_writes_only_new_0600_file_and_never_stdout(tmp_path, capsys):
    target = tmp_path / "brand-os-master.key"
    database = tmp_path / "generated.db"
    main([
        "--database", str(database), "--generate-master-key", str(target), "--json",
    ])
    output = capsys.readouterr().out
    report = json.loads(output)
    key = target.read_text().strip()

    Fernet(key.encode())
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert key not in output
    assert report["generated_master_key_file"] == str(target.resolve())
    assert report["environment"]["credential_master_key_configured"] is True
    with pytest.raises(FileExistsError):
        generate_master_key_file(target)


def test_key_generation_rejects_repo_and_dotenv_targets(tmp_path):
    with pytest.raises(ValueError, match="outside the project"):
        generate_master_key_file(PROJECT_ROOT / "forbidden-bootstrap.key")
    with pytest.raises(ValueError, match=".env"):
        generate_master_key_file(tmp_path / ".env")
    assert not (PROJECT_ROOT / "forbidden-bootstrap.key").exists()
    assert not (tmp_path / ".env").exists()


def test_bootstrap_worker_never_claims_delivery_jobs(tmp_path):
    database = tmp_path / "safe-worker.db"
    store.DATA_PATH = database
    store.init_db()
    brand = store.get_brand("demo-brand")
    delivery = store.enqueue_job(
        "x.dispatch", "approved-but-not-bootstrap-authorized",
        {"dispatch_item_id": "dispatch-1", "expected_revision": 1},
        brand_id=brand["id"], priority=100,
    )
    key = Fernet.generate_key().decode()

    report = bootstrap(
        database=database,
        environment={
            "BRAND_OS_PREVIEW_PASSWORD": "configured",
            "BRAND_OS_CREDENTIAL_MASTER_KEY": key,
        },
        max_jobs=10,
    )

    assert "x.dispatch" not in report["worker"]["safe_job_types"]
    assert store.row("SELECT status FROM durable_jobs WHERE id=?", (delivery["id"],))["status"] == "queued"
    assert all(job["job_type"] != "x.dispatch" for job in report["worker"]["jobs"])


def test_bootstrap_validates_bounds_before_mutating_database(tmp_path):
    database = tmp_path / "must-not-exist.db"
    with pytest.raises(ValueError, match="max_jobs"):
        bootstrap(database=database, max_jobs=0)
    assert not database.exists()
