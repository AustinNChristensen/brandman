from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3

import pytest

from brandman import store
from brandman.ops_cli import (
    beehiiv_private_draft_manifest, create_backup, database_audit, export_usage, initialize_all, local_soak,
    main, operability_audit, reconcile_legacy_dispatches, restore_backup,
)
from brandman.provider_usage import ProviderUsageLedger
from brandman.fixture_quarantine import fixture_quarantine_plan


def test_migrate_initializes_all_schemas_idempotently(tmp_path):
    database = tmp_path / "brand.db"
    first = initialize_all(database, profile="test")
    second = initialize_all(database, profile="test")
    assert first["healthy"] is second["healthy"] is True
    required = {
        "brands", "durable_jobs", "dispatch_items", "dispatch_revisions", "newsletter_issues",
        "execution_agents", "execution_tasks", "provider_usage_events",
        "provider_pricing_versions", "periodic_schedules",
        "approval_snapshots", "approval_snapshot_invalidations",
        "campaign_asset_memberships", "performance_planning_audit",
        "source_canonical_revalidations",
    }
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert required <= tables
    assert first["profile"] == "test"


def test_local_beehiiv_manifest_command_owns_service_construction(tmp_path, monkeypatch):
    database = tmp_path / "operating.db"
    asset = tmp_path / "approved.png"
    asset.write_bytes(b"image")
    calls = []

    class Handoffs:
        def beehiiv_private_draft_manifest(self, task_id, *, asset_path, existing_draft_id):
            calls.append((task_id, Path(asset_path), existing_draft_id))
            return {"action": "upsert_private_draft", "task_id": task_id}

    class Services:
        execution_handoff_store = Handoffs()

    import brandman.main
    monkeypatch.setattr(
        brandman.main, "initialize_application_services",
        lambda path, profile: (
            calls.append((Path(path), profile)) or Services()
        ),
    )
    result = beehiiv_private_draft_manifest(
        database, task_id="task-1", asset_path=asset,
        existing_draft_id="32dcf1b3-8180-49c4-b50c-39da517010f6",
    )
    assert result == {"action": "upsert_private_draft", "task_id": "task-1"}
    assert calls == [
        (database, "operating"),
        ("task-1", asset, "32dcf1b3-8180-49c4-b50c-39da517010f6"),
    ]


def test_database_profiles_prevent_fixture_harnesses_from_writing_operating_data(tmp_path):
    operating = tmp_path / "operating.db"
    initialize_all(operating, profile="operating")
    assert store.database_profile(operating) == "operating"
    with pytest.raises(ValueError, match="refuses database profile operating"):
        local_soak(operating, cycles=1)
    with pytest.raises(ValueError, match="already profiled operating"):
        initialize_all(operating, profile="proof")

    development = tmp_path / "development.db"
    initialize_all(development, profile="development")
    assert local_soak(development, cycles=1)["profile"] == "development"


def test_profile_recovery_is_compare_and_audited(tmp_path):
    database = tmp_path / "misclassified.db"
    initialize_all(database, profile="test")
    assert store.recover_database_profile(
        database, expected_profile="test", target_profile="development",
        actor="operator", reason="Confirmed scratch fixture provenance",
    ) == "development"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT from_profile,to_profile,actor,reason FROM database_profile_audit"
        ).fetchone() == (
            "test", "development", "operator", "Confirmed scratch fixture provenance",
        )
    with pytest.raises(ValueError, match="expected database profile test"):
        store.recover_database_profile(
            database, expected_profile="test", target_profile="proof",
            actor="operator", reason="Stale retry",
        )


def test_fixture_quarantine_plan_is_read_only_and_keeps_ambiguous_rows(tmp_path):
    database = tmp_path / "current.db"
    initialize_all(database, profile="test")
    baseline = tmp_path / "baseline.db"
    create_backup(database, baseline)
    store.DATA_PATH = database
    brand = store.get_brand("demo-brand")
    fixture = store.insert("campaigns", {
        "brand_id": brand["id"], "name": "Test campaign",
        "objective": "Verify governed flow", "source_id": None, "status": "draft",
    })
    real = store.insert("campaigns", {
        "brand_id": brand["id"], "name": "Real campaign",
        "objective": "Serve readers", "source_id": None, "status": "draft",
    })
    with store.connection() as connection:
        connection.execute(
            """CREATE TABLE campaign_graph_audit (
               sequence INTEGER PRIMARY KEY AUTOINCREMENT,campaign_id TEXT NOT NULL,
               membership_id TEXT,action TEXT NOT NULL,actor TEXT NOT NULL,
               reason TEXT NOT NULL,detail_json TEXT NOT NULL DEFAULT '{}',at TEXT NOT NULL)"""
        )
        connection.execute(
            """INSERT INTO campaign_graph_audit
               (campaign_id,membership_id,action,actor,reason,detail_json,at)
               VALUES (?,NULL,'created','fixture','test','{}',?)""",
            (fixture["id"], store.now()),
        )
    from brandman.dispatch import GovernedDispatcher, SQLiteDispatchStore
    dispatch = GovernedDispatcher(SQLiteDispatchStore(database)).create(
        "x", {"body": "Draft"}, brand_id=brand["id"], item_id="fixture-dispatch",
    )
    before = database.read_bytes()
    plan = fixture_quarantine_plan(
        database, baseline, fixture_ids=[fixture["id"], dispatch.id],
        recovery_adjustments=[{
            "table": "product_feedback", "key": ["preserved"],
            "operation": "compare_and_set_occurrence_count", "expected": 9, "target": 3,
        }],
    )
    assert plan["mode"] == "dry_run_read_only"
    assert [fixture["id"]] in plan["candidate_by_table"]["campaigns"]
    assert [real["id"]] in plan["ambiguous_by_table"]["campaigns"]
    assert len(plan["manifest_sha256"]) == 64
    assert [dispatch.id, 1] in plan["dependent_by_table"]["dispatch_revisions"]
    assert plan["dependent_by_table"]["campaign_graph_audit"]
    assert plan["recovery_adjustments"][0]["target"] == 3
    assert any(
        operation["key"] == [fixture["id"]] and operation["operation"] == "archive"
        for operation in plan["safe_lifecycle_operations"]
    )
    assert database.read_bytes() == before


def test_online_sqlite_backup_and_create_only_restore_are_verified(tmp_path):
    database = tmp_path / "brand.db"
    initialize_all(database)
    store.DATA_PATH = database
    brand_id = store.get_brand("demo-brand")["id"]
    backup = tmp_path / "snapshots" / "brand-1.db"
    backup.parent.mkdir()
    result = create_backup(database, backup)
    assert result["healthy"] is True
    assert oct(backup.stat().st_mode & 0o777) == "0o600"
    assert result["row_counts"]["brands"] >= 2
    with pytest.raises(FileExistsError):
        create_backup(database, backup)

    restored = tmp_path / "recovery" / "restored.db"
    restored.parent.mkdir()
    recovery = restore_backup(backup, restored)
    assert recovery["healthy"] is True
    with sqlite3.connect(restored) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM brands WHERE id=?", (brand_id,),
        ).fetchone()[0] == 1


def test_usage_export_json_and_csv_are_new_private_files(tmp_path):
    database = tmp_path / "brand.db"
    initialize_all(database)
    store.DATA_PATH = database
    brand = store.get_brand("demo-brand")
    ProviderUsageLedger(database).record(
        brand_id=brand["id"], connector_account_id="x-account", provider="x",
        method="GET", url="https://api.x.com/2/users/1?token=never", status_code=200,
        billable_category="user.read",
    )
    json_file = tmp_path / "usage.json"
    csv_file = tmp_path / "usage.csv"
    assert export_usage(database, "demo-brand", json_file, "json")["rows"] == 1
    assert export_usage(database, "demo-brand", csv_file, "csv")["rows"] == 1
    assert "never" not in json_file.read_text()
    assert "user.read" in csv_file.read_text()
    assert oct(json_file.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(FileExistsError):
        export_usage(database, "demo-brand", json_file, "json")


def test_repeatable_local_soak_never_executes_delivery_jobs(tmp_path):
    result = local_soak(tmp_path / "soak.db", cycles=20)
    assert result["cycles"] == 20
    assert result["healthy"] is True
    assert result["external_delivery_job_types_executed"] == []


def test_rate_card_cli_emits_metadata_not_credentials(tmp_path, capsys):
    database = tmp_path / "ops.db"
    initialize_all(database)
    main([
        "--database", str(database), "rate-card-add",
        "--version", "operator-v1", "--provider", "x", "--method", "POST",
        "--endpoint-pattern", "https://api.x.com/2/tweets",
        "--billable-category", "post.create_plain", "--unit-name", "resource",
        "--unit-price", "0.01", "--currency", "USD",
        "--effective-at", "2026-09-02T00:00:00Z", "--actor", "chris",
    ])
    output = json.loads(capsys.readouterr().out)
    assert output["billable_category"] == "post.create_plain"
    assert "credential" not in repr(output).lower()


def test_audit_rejects_missing_database(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        database_audit(tmp_path / "missing.db")


def test_operability_audit_is_compact_and_reports_exact_next_actions(tmp_path):
    database = tmp_path / "audit.db"
    initialize_all(database)
    report = operability_audit(database, environment={})
    assert report["healthy"] is True
    assert report["readiness"]["ready"] is False
    assert report["readiness"]["next_actions"]
    assert "events" not in report["provider_usage"]


def test_dispatch_reconciliation_is_dry_run_first_and_idempotent(tmp_path):
    database = tmp_path / "reconcile.db"
    initialize_all(database)
    store.DATA_PATH = database
    brand = store.get_brand("demo-brand")
    campaign = store.insert("campaigns", {
        "brand_id": brand["id"], "name": "Tracked", "objective": "Test",
        "source_id": None, "status": "draft",
    })
    post = store.insert("posts", {
        "campaign_id": campaign["id"], "channel": "x",
        "body": "Read https://example.test/story", "status": "draft",
        "scheduled_for": None, "external_post_id": None,
    })
    from brandman.dispatch import GovernedDispatcher, SQLiteDispatchStore
    dispatcher = GovernedDispatcher(SQLiteDispatchStore(database))
    retained = dispatcher.create(
        "x", {"body": post["body"]}, brand_id=brand["id"],
        canonical_post_id=post["id"], item_id="canonical",
    )
    tracked_url = (
        "https://example.test/story?utm_source=x&utm_medium=organic-social"
        f"&utm_campaign={campaign['id']}&utm_content={post['id']}"
    )
    duplicate = dispatcher.create("x", {
        "body": "Read " + tracked_url, "tracked_url": tracked_url,
        "canonical_post_id": post["id"], "campaign_id": campaign["id"],
    }, brand_id=brand["id"], item_id="legacy")

    preview = reconcile_legacy_dispatches(database)
    assert preview["dry_run"] is True and preview["candidates"] == 1
    assert dispatcher.store.get(retained.id).revision == 1
    assert dispatcher.store.get(duplicate.id).status.value == "draft"
    applied = reconcile_legacy_dispatches(database, apply=True)
    assert applied["external_actions"] == 0 and applied["candidates"] == 1
    assert dispatcher.store.get(retained.id).payload == {"body": "Read " + tracked_url}
    assert dispatcher.store.get(duplicate.id).status.value == "cancelled"
    assert reconcile_legacy_dispatches(database, apply=True)["candidates"] == 0
