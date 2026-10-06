from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from app import store
from app import fixture_quarantine_apply as quarantine_apply
from app.dispatch import GovernedDispatcher, SQLiteDispatchStore
from app.fixture_quarantine import fixture_quarantine_plan
from app.fixture_quarantine_apply import FixtureQuarantineError, execute_fixture_quarantine
from app.ops_cli import create_backup, initialize_all


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inner(plan: dict) -> str:
    material = {key: plan.get(key, [] if key in {
        "protected_ids", "recovery_adjustments", "protected_conflicts"
    } else {} if key.endswith("_by_table") else None) for key in (
        "candidate_by_table", "root_by_table", "dependent_by_table", "protected_ids",
        "recovery_adjustments", "ambiguous_count", "protected_conflicts",
    )}
    return hashlib.sha256(json.dumps(material,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def _fixture_manifest(tmp_path: Path) -> tuple[Path, Path, dict, str]:
    database = tmp_path / "operating.db"
    initialize_all(database, profile="operating")
    baseline = tmp_path / "baseline.db"
    create_backup(database, baseline)
    store.DATA_PATH = database
    brand = store.get_brand("demo-brand")
    campaign = store.insert("campaigns", {
        "brand_id": brand["id"], "source_id": None, "name": "Test campaign",
        "objective": "Verify governed flow", "status": "draft",
    })
    post = store.insert("posts", {
        "campaign_id": campaign["id"], "channel": "x", "body": "Draft text",
        "status": "scheduled", "scheduled_for": "2026-09-03T15:00:00+00:00",
        "external_post_id": None,
    })
    dispatch = GovernedDispatcher(SQLiteDispatchStore(database)).create(
        "x", {"body": "Draft"}, brand_id=brand["id"], item_id="qa-dispatch",
    )
    inert_dispatch = GovernedDispatcher(SQLiteDispatchStore(database)).create(
        "x", {"body": "No", "reply_to_post_id": "123"},
        brand_id=brand["id"], item_id="qa-rejected-dispatch",
    )
    snapshot_id = "qa-snapshot"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE dispatch_items SET status='rejected' WHERE id=?",(inert_dispatch.id,))
        timestamp = store.now()
        connection.execute("""INSERT INTO newsletter_issues
            (id,brand_id,candidate_id,lifecycle,current_revision,approved_revision,approved_by,
             approved_at,beehiiv_external_id,beehiiv_preview_url,scheduled_for,published_at,created_at,updated_at)
             VALUES ('qa-archived-issue',?,NULL,'archived',1,NULL,NULL,NULL,NULL,NULL,NULL,NULL,?,?)""",
            (brand["id"],timestamp,timestamp))
        connection.execute("""INSERT INTO product_feedback
            (id,brand_id,reporter,summary,details,status,created_at,component,severity,
             related_ids,first_seen_at,last_seen_at,occurrence_count,updated_at)
             VALUES ('preserved-feedback',?,'agent','Real feedback','Real','open',?,'test','medium',
             '[]',?,'2026-09-02T08:33:46+00:00',9,'2026-09-02T08:33:46+00:00')""",
            (brand["id"],timestamp,timestamp))
        connection.execute("""INSERT INTO approval_snapshots
            (id,brand_id,account_ref,campaign_id,asset_membership_id,resource_type,
             resource_id,action_type,destination,intended_schedule,revision,approver,
             approved_at,material_fingerprint,material_json,created_at)
            VALUES (?,?,?, ?,NULL,'dispatch_item',?,'publish','x',NULL,1,'tester',?,?,?,?)""",
            (snapshot_id,brand["id"],"qa",campaign["id"],dispatch.id,store.now(),
             "fingerprint",json.dumps({"body":"Draft"}),store.now()))
    plan = fixture_quarantine_plan(
        database, baseline, fixture_ids=[campaign["id"],post["id"],dispatch.id,
            inert_dispatch.id,"qa-archived-issue"],
        preserve_ids=["preserved-feedback"],
        recovery_adjustments=[{
            "table":"product_feedback","key":["preserved-feedback"],
            "operation":"compare_and_set_with_append_only_recovery_audit",
            "expected":{"occurrence_count":9,"last_seen_at":"2026-09-02T08:33:46+00:00","updated_at":"2026-09-02T08:33:46+00:00"},
            "target":{"occurrence_count":3,"last_seen_at":"2026-09-02T08:08:13+00:00","updated_at":"2026-09-02T08:08:13+00:00"},
            "evidence_history_sequences":[168,169],
        }],
    )
    manifest = tmp_path / "manifest.json"
    fixture_ids = [campaign["id"],post["id"],dispatch.id,inert_dispatch.id,"qa-archived-issue"]
    manifest.write_text(json.dumps({
        "evidence_version": 2, "database_sha256": _sha(database),
        "baseline_sha256": _sha(baseline), "fixture_ids": fixture_ids,
        "protected_ids_input": [], "plan": plan,
    }, sort_keys=True), encoding="utf-8")
    return database, manifest, {
        "campaign": campaign["id"], "post": post["id"],
        "dispatch": dispatch.id, "inert_dispatch": inert_dispatch.id,
        "snapshot": snapshot_id, "issue": "qa-archived-issue",
    }, _sha(manifest)


def test_manifest_executor_is_gated_atomic_and_idempotent(tmp_path):
    database, manifest, ids, file_sha = _fixture_manifest(tmp_path)
    plan = json.loads(manifest.read_text())["plan"]
    dry = execute_fixture_quarantine(
        database, manifest, expected_file_sha256=file_sha,
        actor="system:fixture-recovery", reason="Scratch executor proof",
    )
    assert dry["mode"] == "dry_run" and dry["provider_actions"] == 0
    backup = tmp_path / "fresh-backup.db"
    with pytest.raises(FixtureQuarantineError, match="exact manifest digest"):
        execute_fixture_quarantine(
            database, manifest, expected_file_sha256=file_sha,
            confirmation_manifest_sha256="wrong", backup_path=backup,
            actor="system:fixture-recovery", reason="Scratch executor proof", apply=True,
        )
    assert not backup.exists()

    applied = execute_fixture_quarantine(
        database, manifest, expected_file_sha256=file_sha,
        confirmation_manifest_sha256=plan["manifest_sha256"], backup_path=backup,
        actor="system:fixture-recovery", reason="Scratch executor proof", apply=True,
    )
    assert applied["mode"] == "applied" and applied["provider_actions"] == 0
    assert oct(backup.stat().st_mode & 0o777) == "0o600"
    with sqlite3.connect(backup) as backup_connection:
        assert backup_connection.execute("SELECT status FROM campaigns WHERE id=?",(ids["campaign"],)).fetchone()[0] == "draft"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status FROM campaigns WHERE id=?",(ids["campaign"],)).fetchone()[0] == "archived"
        assert connection.execute("SELECT status FROM posts WHERE id=?",(ids["post"],)).fetchone()[0] == "cancelled"
        assert connection.execute("SELECT status FROM dispatch_items WHERE id=?",(ids["dispatch"],)).fetchone()[0] == "cancelled"
        assert connection.execute("SELECT status FROM dispatch_items WHERE id=?",(ids["inert_dispatch"],)).fetchone()[0] == "rejected"
        assert connection.execute("SELECT lifecycle FROM newsletter_issues WHERE id=?",(ids["issue"],)).fetchone()[0] == "archived"
        assert connection.execute("SELECT COUNT(*) FROM approval_snapshot_invalidations WHERE snapshot_id=?",(ids["snapshot"],)).fetchone()[0] == 1
        assert connection.execute("SELECT occurrence_count FROM product_feedback WHERE id='preserved-feedback'").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM feedback_history WHERE feedback_id='preserved-feedback' AND action='fixture_recovery_adjustment'").fetchone()[0] == 1
        audit_count = connection.execute("SELECT COUNT(*) FROM fixture_quarantine_audit").fetchone()[0]
    replay = execute_fixture_quarantine(
        database, manifest, expected_file_sha256=file_sha,
        confirmation_manifest_sha256=plan["manifest_sha256"], backup_path=tmp_path/"unused.db",
        actor="system:fixture-recovery", reason="Scratch executor proof", apply=True,
    )
    assert replay["mode"] == "idempotent_replay"
    assert replay["transitions"] == replay["registrations"] == replay["snapshot_invalidations"] == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM fixture_quarantine_audit").fetchone()[0] == audit_count
    assert not (tmp_path/"unused.db").exists()


def test_executor_accepts_only_unmistakable_test_source_roots(tmp_path):
    database = tmp_path / "operating.db"
    initialize_all(database, profile="operating")
    baseline = tmp_path / "baseline.db"
    create_backup(database, baseline)
    source_id = "escaped-test-source"
    with sqlite3.connect(database) as connection:
        brand_id = connection.execute(
            "SELECT id FROM brands WHERE slug='demo-brand'"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO sources
               (id,brand_id,title,url,source_type,body_summary,lifecycle_state,
                scheduled_for,created_at,external_source_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (source_id, brand_id, "Live Beehiiv Post", "https://example.test/edit",
             "beehiiv", "Beehiiv scheduled post · tags: test", "scheduled",
             "2026-09-02T15:30:00+00:00", store.now(), "post_test"),
        )
    plan = fixture_quarantine_plan(database, baseline, fixture_ids=[source_id])
    manifest = tmp_path / "source-manifest.json"
    manifest.write_text(json.dumps({
        "evidence_version": 4, "database_sha256": _sha(database),
        "baseline_sha256": _sha(baseline), "fixture_ids": [source_id],
        "protected_ids_input": [], "plan": plan,
    }, sort_keys=True), encoding="utf-8")

    dry = execute_fixture_quarantine(
        database, manifest, expected_file_sha256=_sha(manifest),
        actor="system:fixture-recovery", reason="Scratch source recovery proof",
    )
    assert dry["mode"] == "dry_run" and dry["root_count"] == 1

    backup = tmp_path / "source-backup.db"
    applied = execute_fixture_quarantine(
        database, manifest, expected_file_sha256=_sha(manifest),
        confirmation_manifest_sha256=plan["manifest_sha256"], backup_path=backup,
        actor="system:fixture-recovery", reason="Scratch source recovery proof", apply=True,
    )
    assert applied["transitions"] == 0 and applied["registrations"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM fixture_quarantine_registry WHERE table_name='sources' AND record_key_json=?",
            (json.dumps([source_id], separators=(",", ":")),),
        ).fetchone()[0] == 1


def test_manifest_executor_rejects_non_operating_and_changed_inputs(tmp_path):
    database, manifest, _, file_sha = _fixture_manifest(tmp_path)
    with pytest.raises(FixtureQuarantineError, match="file digest"):
        execute_fixture_quarantine(database, manifest, expected_file_sha256="0"*64,
            actor="recovery", reason="proof")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE database_metadata SET value='development' WHERE key='profile'")
    with pytest.raises(FixtureQuarantineError, match="explicit operating profile"):
        execute_fixture_quarantine(database, manifest, expected_file_sha256=file_sha,
            actor="recovery", reason="proof")


@pytest.mark.parametrize("profile", [None, "mystery"])
def test_manifest_executor_fails_closed_on_missing_or_unknown_profile(tmp_path, profile):
    database, manifest, _, file_sha = _fixture_manifest(tmp_path)
    with sqlite3.connect(database) as connection:
        if profile is None:
            connection.execute("DELETE FROM database_metadata WHERE key='profile'")
        else:
            connection.execute("UPDATE database_metadata SET value=? WHERE key='profile'",(profile,))
    with pytest.raises(FixtureQuarantineError,match="profile"):
        execute_fixture_quarantine(database,manifest,expected_file_sha256=file_sha,
            actor="recovery",reason="proof")


def test_manifest_executor_rechecks_digest_after_writer_lock(tmp_path, monkeypatch):
    database, manifest, _, file_sha = _fixture_manifest(tmp_path)
    plan=json.loads(manifest.read_text())["plan"]
    original=quarantine_apply._sha256
    calls=0
    def race(path):
        nonlocal calls
        calls += 1
        digest=original(path)
        if calls == 1:
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE brands SET mission=mission||' raced' WHERE slug='demo-brand'")
        return digest
    monkeypatch.setattr(quarantine_apply,"_sha256",race)
    backup=tmp_path/"must-not-exist.db"
    with pytest.raises(FixtureQuarantineError,match="changed before locked apply"):
        execute_fixture_quarantine(database,manifest,expected_file_sha256=file_sha,
            confirmation_manifest_sha256=plan["manifest_sha256"],backup_path=backup,
            actor="recovery",reason="proof",apply=True)
    assert not backup.exists()


def test_backup_reservation_and_writer_lock_close_races(tmp_path, monkeypatch):
    database, manifest, _, file_sha = _fixture_manifest(tmp_path)
    plan=json.loads(manifest.read_text())["plan"]
    existing=tmp_path/"existing.db"; existing.write_bytes(b"do not overwrite")
    with pytest.raises(FileExistsError):
        execute_fixture_quarantine(database,manifest,expected_file_sha256=file_sha,
            confirmation_manifest_sha256=plan["manifest_sha256"],backup_path=existing,
            actor="recovery",reason="proof",apply=True)
    assert existing.read_bytes()==b"do not overwrite"

    original=quarantine_apply._create_verified_backup_locked
    observed=[]
    def probe(source,target):
        try:
            with sqlite3.connect(database,timeout=0.01) as writer:
                writer.execute("UPDATE brands SET mission=mission||' concurrent' WHERE slug='demo-brand'")
        except sqlite3.OperationalError as error:
            observed.append(str(error))
        return original(source,target)
    monkeypatch.setattr(quarantine_apply,"_create_verified_backup_locked",probe)
    backup=tmp_path/"locked-backup.db"
    applied=execute_fixture_quarantine(database,manifest,expected_file_sha256=file_sha,
        confirmation_manifest_sha256=plan["manifest_sha256"],backup_path=backup,
        actor="recovery",reason="proof",apply=True)
    assert applied["mode"]=="applied"
    assert observed and "locked" in observed[0].lower()
    assert oct(backup.stat().st_mode&0o777)=="0o600"


def test_self_consistent_rehashed_root_injection_is_rejected(tmp_path):
    database, manifest, _, _ = _fixture_manifest(tmp_path)
    evidence=json.loads(manifest.read_text())

    def write_forged(table,key,operation):
        plan=evidence["plan"]
        plan["candidate_by_table"]={table:[[key]]}
        plan["root_by_table"]={table:[[key]]}
        plan["dependent_by_table"]={}
        plan["candidate_count"]=plan["root_count"]=1;plan["dependent_count"]=0
        plan["safe_lifecycle_operations"]=[{
            "table":table,"key":[key],"operation":operation,
            "classification":"root_recovery_action","current_status":"open","requires_audit":True,
        }]
        plan["manifest_sha256"]=_inner(plan)
        evidence["fixture_ids"]=[key]
        manifest.write_text(json.dumps(evidence,sort_keys=True),encoding="utf-8")

    # Even an allowed table/action cannot hide arbitrary legitimate feedback.
    write_forged("product_feedback","preserved-feedback","quarantine_fixture_feedback")
    with pytest.raises(FixtureQuarantineError,match="approved fixture signature"):
        execute_fixture_quarantine(database,manifest,expected_file_sha256=_sha(manifest),
            actor="recovery",reason="forged")

    brand=store.get_brand("demo-brand")
    write_forged("brands",brand["id"],"register_quarantined_exclude_from_reads")
    with pytest.raises(FixtureQuarantineError,match="undeclared root table"):
        execute_fixture_quarantine(database,manifest,expected_file_sha256=_sha(manifest),
            actor="recovery",reason="forged")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO sources
               (id,brand_id,title,url,source_type,body_summary,lifecycle_state,
                scheduled_for,created_at,external_source_id)
               VALUES ('arbitrary-source',?,'Official source','https://example.com/live',
                       'manual','Genuine operating evidence','published',NULL,?,NULL)""",
            (brand["id"], store.now()),
        )
    write_forged("sources","arbitrary-source","register_quarantined_exclude_from_reads")
    with pytest.raises(FixtureQuarantineError,match="approved fixture signature"):
        execute_fixture_quarantine(database,manifest,expected_file_sha256=_sha(manifest),
            actor="recovery",reason="forged")


def test_rehashed_undeclared_operation_and_partition_mismatch_are_rejected(tmp_path):
    database, manifest, _, _ = _fixture_manifest(tmp_path)
    evidence=json.loads(manifest.read_text());plan=evidence["plan"]
    plan["safe_lifecycle_operations"][0]["operation"]="hide_anything"
    manifest.write_text(json.dumps(evidence,sort_keys=True),encoding="utf-8")
    with pytest.raises(FixtureQuarantineError,match="allowed table/action"):
        execute_fixture_quarantine(database,manifest,expected_file_sha256=_sha(manifest),
            actor="recovery",reason="forged")

    evidence=json.loads(_fixture_manifest(tmp_path/"second")[1].read_text())
    second=tmp_path/"second"/"manifest.json";plan=evidence["plan"]
    table=next(iter(plan["root_by_table"]));plan["candidate_by_table"].pop(table,None)
    plan["manifest_sha256"]=_inner(plan)
    second.write_text(json.dumps(evidence,sort_keys=True),encoding="utf-8")
    with pytest.raises(FixtureQuarantineError,match="exactly reconcile candidates"):
        execute_fixture_quarantine(tmp_path/"second"/"operating.db",second,
            expected_file_sha256=_sha(second),actor="recovery",reason="forged")


def test_manifest_executor_detects_inner_tamper_and_rolls_back_atomically(tmp_path):
    database, manifest, ids, _ = _fixture_manifest(tmp_path)
    evidence = json.loads(manifest.read_text())
    evidence["plan"]["root_by_table"]["campaigns"].append(["invented-root"])
    manifest.write_text(json.dumps(evidence,sort_keys=True),encoding="utf-8")
    with pytest.raises(FixtureQuarantineError,match="inner manifest digest"):
        execute_fixture_quarantine(database,manifest,expected_file_sha256=_sha(manifest),
            actor="recovery",reason="proof")

    # Rebuild a valid digest around an unsupported adjustment so the executor
    # fails after root processing has started; the transaction must undo it all.
    evidence["plan"]["root_by_table"]["campaigns"].pop()
    evidence["plan"]["recovery_adjustments"].append({
        "table":"unsupported","key":["x"],"operation":"unsupported",
    })
    evidence["plan"]["manifest_sha256"] = _inner(evidence["plan"])
    manifest.write_text(json.dumps(evidence,sort_keys=True),encoding="utf-8")
    backup=tmp_path/"rollback-backup.db"
    with pytest.raises(FixtureQuarantineError,match="unsupported recovery adjustment"):
        execute_fixture_quarantine(database,manifest,expected_file_sha256=_sha(manifest),
            confirmation_manifest_sha256=evidence["plan"]["manifest_sha256"],backup_path=backup,
            actor="recovery",reason="proof",apply=True)
    assert backup.exists() and oct(backup.stat().st_mode&0o777)=="0o600"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status FROM campaigns WHERE id=?",(ids["campaign"],)).fetchone()[0]=="draft"
        assert connection.execute("SELECT COUNT(*) FROM fixture_quarantine_runs").fetchone()[0]==0
        assert connection.execute("SELECT COUNT(*) FROM fixture_quarantine_audit").fetchone()[0]==0
