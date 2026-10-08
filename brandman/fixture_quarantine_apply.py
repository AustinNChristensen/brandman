"""Strict, local-only executor for an independently validated fixture manifest."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping
from urllib.parse import urlparse

from . import store


class FixtureQuarantineError(ValueError):
    pass


_ROOT_ACTIONS = {
    "brand_learnings": "disable_accepted_or_reject_testing",
    "campaigns": "archive", "connector_account_configurations": "register_quarantined_configuration",
    "connector_accounts": "disconnect", "dispatch_items": "cancel_and_clear_approval_claim",
    "durable_jobs": "cancel_queued_job", "editorial_candidates": "register_quarantined_already_abandoned",
    "newsletter_issues": "abandon_and_invalidate_approval",
    "performance_records": "register_quarantined_exclude_from_reads",
    "periodic_schedules": "disable", "posts": "cancel",
    "product_feedback": "quarantine_fixture_feedback", "third_party_source_configs": "disable",
    "sources": "register_quarantined_exclude_from_reads",
    "x_engagement_opportunities": "dismiss",
}
_DEPENDENT_ACTIONS = {
    "approval_snapshot_invalidations": "preserve_immutable_audit",
    "approval_snapshots": "invalidate_if_active",
    "brand_learning_audit": "preserve_immutable_audit",
    "campaign_graph_audit": "preserve_immutable_audit",
    "connector_events": "preserve_immutable_audit",
    "dispatch_audit": "preserve_immutable_audit",
    "dispatch_revisions": "preserve_immutable_revision",
    "editorial_lifecycle_events": "preserve_immutable_audit",
    "feedback_history": "preserve_immutable_audit",
    "newsletter_fact_checks": "preserve_immutable_audit",
    "newsletter_revisions": "preserve_immutable_revision",
    "orchestration_decisions": "preserve_immutable_audit",
    "sync_cursors": "register_quarantined_exclude_from_sync_state",
    "third_party_source_audit": "preserve_immutable_audit",
    "x_engagement_history": "preserve_immutable_audit",
}


def execute_fixture_quarantine(
    database: str | Path,
    manifest_file: str | Path,
    *,
    expected_file_sha256: str,
    confirmation_manifest_sha256: str | None = None,
    backup_path: str | Path | None = None,
    actor: str,
    reason: str,
    apply: bool = False,
) -> dict[str, Any]:
    """Validate or apply an exact manifest without constructing providers.

    Dry-run is the default. Applying requires both digest confirmations and a
    new backup target. A completed manifest replay is an idempotent no-op.
    """
    db = Path(database).expanduser().resolve()
    manifest_path = Path(manifest_file).expanduser().resolve()
    if not actor.strip() or not reason.strip():
        raise FixtureQuarantineError("actor and reason are required")
    if not db.is_file() or not manifest_path.is_file():
        raise FixtureQuarantineError("database and manifest must exist")
    raw = manifest_path.read_bytes()
    file_digest = hashlib.sha256(raw).hexdigest()
    if file_digest != expected_file_sha256:
        raise FixtureQuarantineError("manifest file digest mismatch")
    evidence = json.loads(raw)
    plan = evidence.get("plan") or {}
    inner = _inner_digest(plan)
    if inner != plan.get("manifest_sha256"):
        raise FixtureQuarantineError("inner manifest digest mismatch")
    if plan.get("writes") != 0 or plan.get("external_actions") != 0:
        raise FixtureQuarantineError("manifest is not a read-only forensic plan")
    if plan.get("ambiguous_count") != 0 or plan.get("protected_conflicts"):
        raise FixtureQuarantineError("manifest has ambiguity or protected conflicts")
    _validate_manifest_contract(db, evidence, plan)
    current_digest = _sha256(db)
    _require_operating_profile(db)

    already = _completed_run(db, inner)
    if already:
        return {
            "mode": "idempotent_replay", "manifest_sha256": inner,
            "file_sha256": file_digest, "transitions": 0, "registrations": 0,
            "snapshot_invalidations": 0, "provider_actions": 0,
            "backup": already["backup_path"],
        }
    if current_digest != evidence.get("database_sha256"):
        raise FixtureQuarantineError("database digest does not match manifest input")
    _revalidate_typed_closure(db, evidence, plan)

    preview = {
        "mode": "dry_run", "manifest_sha256": inner, "file_sha256": file_digest,
        "database_sha256": current_digest, "root_count": plan.get("root_count"),
        "dependent_count": plan.get("dependent_count"), "provider_actions": 0,
        "requires_confirmation": inner,
    }
    if not apply:
        return preview
    if confirmation_manifest_sha256 != inner:
        raise FixtureQuarantineError("apply requires the exact manifest digest confirmation")
    if backup_path is None:
        raise FixtureQuarantineError("apply requires a fresh backup path")
    timestamp = store.now()
    counts = {"transitions": 0, "registrations": 0, "snapshot_invalidations": 0}
    connection = sqlite3.connect(db, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        _require_operating_profile_connection(connection)
        # BEGIN IMMEDIATE excludes concurrent writers. Revalidate the exact
        # file fingerprint only after acquiring that lock, then retain the lock
        # through backup and the entire recovery transaction.
        locked_digest = _sha256(db)
        if locked_digest != evidence.get("database_sha256"):
            raise FixtureQuarantineError("database digest changed before locked apply")
        backup = _create_verified_backup_locked(db, backup_path)
        _ensure_recovery_schema(connection)
        for table, keys in (plan.get("root_by_table") or {}).items():
            for key in keys:
                counts["transitions"] += _recover_root(
                    connection, table, key, inner, actor, reason, timestamp,
                )
                counts["registrations"] += _register(
                    connection, table, key, inner, actor, reason, timestamp,
                )
        for key in (plan.get("dependent_by_table") or {}).get("approval_snapshots", []):
            counts["snapshot_invalidations"] += _invalidate_snapshot(
                connection, key, actor, reason, timestamp,
            )
        for adjustment in plan.get("recovery_adjustments") or []:
            counts["transitions"] += _apply_adjustment(
                connection, adjustment, inner, actor, timestamp,
            )
        connection.execute(
            """INSERT INTO fixture_quarantine_runs
               (manifest_sha256,file_sha256,database_sha256_before,backup_path,actor,reason,applied_at)
               VALUES (?,?,?,?,?,?,?)""",
            (inner, file_digest, locked_digest, str(backup), actor, reason, timestamp),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "mode": "applied", "manifest_sha256": inner, "file_sha256": file_digest,
        **counts, "provider_actions": 0, "backup": str(backup),
    }


def _recover_root(c: sqlite3.Connection, table: str, key: list[Any], digest: str,
                  actor: str, reason: str, timestamp: str) -> int:
    if len(key) != 1:
        raise FixtureQuarantineError(f"root {table} must have a single key")
    identity = key[0]
    row = c.execute(f'SELECT * FROM "{table}" WHERE "{_pk(c, table)}"=?', (identity,)).fetchone()
    if row is None:
        raise FixtureQuarantineError(f"manifest root missing: {table} {identity}")
    changed = 0
    if table == "brand_learnings":
        if row["status"] == "accepted" and row["active"]:
            changed = c.execute(
                "UPDATE brand_learnings SET active=0,disabled_at=? WHERE id=? AND active=1",
                (timestamp, identity),
            ).rowcount
            c.execute("""INSERT INTO brand_learning_audit
                (learning_id,brand_id,action,actor,details_json,at) VALUES (?,?,?,?,?,?)""",
                (identity,row["brand_id"],"disabled",actor,json.dumps({"reason":reason,"manifest":digest}),timestamp))
        elif row["status"] == "testing":
            result = c.execute("UPDATE brand_learnings SET status='rejected',active=0,reviewed_at=? WHERE id=? AND status='testing'",
                               (timestamp,identity)); changed = result.rowcount
            c.execute("""INSERT INTO brand_learning_audit
                (learning_id,brand_id,action,actor,details_json,at) VALUES (?,?,?,?,?,?)""",
                (identity,row["brand_id"],"rejected",actor,json.dumps({"reason":reason,"manifest":digest}),timestamp))
    elif table == "periodic_schedules" and row["enabled"]:
        changed = c.execute("UPDATE periodic_schedules SET enabled=0,updated_at=? WHERE id=? AND enabled=1",
                            (timestamp,identity)).rowcount
    elif table == "third_party_source_configs" and row["enabled"]:
        changed = c.execute("UPDATE third_party_source_configs SET enabled=0,updated_at=? WHERE connector_account_id=? AND enabled=1",
                            (timestamp,identity)).rowcount
    elif table == "connector_accounts" and row["status"] not in {"disconnected","disabled"}:
        changed = c.execute("UPDATE connector_accounts SET status='disconnected',updated_at=? WHERE id=? AND status=?",
                            (timestamp,identity,row["status"])).rowcount
    elif table == "durable_jobs" and row["status"] in {"queued","claimed","retry"}:
        changed = c.execute("""UPDATE durable_jobs SET status='cancelled',locked_at=NULL,locked_by=NULL,
            completed_at=?,updated_at=? WHERE id=? AND status=?""",
            (timestamp,timestamp,identity,row["status"])).rowcount
    elif table == "dispatch_items" and row["status"] not in {"rejected","cancelled","published","measured"}:
        if row["external_id"] or row["external_url"]:
            raise FixtureQuarantineError("refusing to cancel a dispatch with provider evidence")
        changed = c.execute("""UPDATE dispatch_items SET status='cancelled',approval_approver=NULL,
            approval_revision=NULL,approval_at=NULL,approval_batch_id=NULL,idempotency_key=NULL,
            dispatch_claim=NULL,updated_at=? WHERE id=? AND status=?""",
            (timestamp,identity,row["status"])).rowcount
        if changed:
            c.execute("INSERT INTO dispatch_audit(item_id,action,actor,at,revision,detail) VALUES (?,?,?,?,?,?)",
                      (identity,"fixture_quarantine_cancelled",actor,timestamp,row["revision"],reason))
    elif table == "posts" and row["status"] in {"draft","scheduled","queued","approved"}:
        if row["external_post_id"]:
            raise FixtureQuarantineError("refusing to cancel a post with provider evidence")
        changed = c.execute("UPDATE posts SET status='cancelled',updated_at=? WHERE id=? AND status=?",
                            (timestamp,identity,row["status"])).rowcount
    elif table == "campaigns" and row["status"] not in {"archived","cancelled","completed"}:
        changed = c.execute("UPDATE campaigns SET status='archived' WHERE id=? AND status=?",
                            (identity,row["status"])).rowcount
    elif table == "newsletter_issues" and row["lifecycle"] not in {"abandoned","archived","published"}:
        if row["beehiiv_external_id"] or row["published_at"]:
            raise FixtureQuarantineError("refusing to abandon a delivered newsletter")
        changed = c.execute("""UPDATE newsletter_issues SET lifecycle='abandoned',approved_revision=NULL,
            approved_by=NULL,approved_at=NULL,updated_at=? WHERE id=? AND lifecycle=?""",
            (timestamp,identity,row["lifecycle"])).rowcount
    elif table == "x_engagement_opportunities" and row["state"] != "dismissed":
        changed = c.execute("UPDATE x_engagement_opportunities SET state='dismissed',updated_at=? WHERE id=? AND state=?",
                            (timestamp,identity,row["state"])).rowcount
    _audit(c,digest,table,key,"state_transition" if changed else "registered_inert",actor,
           {"reason":reason,"from":dict(row)},timestamp)
    return int(bool(changed))


def _register(c: sqlite3.Connection, table: str, key: list[Any], digest: str,
              actor: str, reason: str, timestamp: str) -> int:
    result = c.execute("""INSERT OR IGNORE INTO fixture_quarantine_registry
        (table_name,record_key_json,manifest_sha256,actor,reason,quarantined_at)
        VALUES (?,?,?,?,?,?)""", (table,_json_key(key),digest,actor,reason,timestamp))
    return result.rowcount


def _invalidate_snapshot(c: sqlite3.Connection, key: list[Any], actor: str,
                         reason: str, timestamp: str) -> int:
    snapshot_id = key[0]
    exists = c.execute("SELECT 1 FROM approval_snapshots WHERE id=?",(snapshot_id,)).fetchone()
    if not exists: raise FixtureQuarantineError(f"snapshot missing: {snapshot_id}")
    return c.execute("""INSERT OR IGNORE INTO approval_snapshot_invalidations
        (snapshot_id,actor,reason,invalidated_at) VALUES (?,?,?,?)""",
        (snapshot_id,actor,reason,timestamp)).rowcount


def _apply_adjustment(c: sqlite3.Connection, adjustment: Mapping[str,Any], digest: str,
                      actor: str, timestamp: str) -> int:
    if adjustment.get("table") != "product_feedback" or adjustment.get("operation") != "compare_and_set_with_append_only_recovery_audit":
        raise FixtureQuarantineError("unsupported recovery adjustment")
    identity=adjustment["key"][0]; expected=adjustment["expected"]; target=adjustment["target"]
    row=c.execute("SELECT occurrence_count,last_seen_at,updated_at,status FROM product_feedback WHERE id=?",(identity,)).fetchone()
    if row is None: raise FixtureQuarantineError("recovery adjustment target missing")
    if all(row[k] == target[k] for k in target): return 0
    if not all(row[k] == expected[k] for k in expected):
        raise FixtureQuarantineError("recovery adjustment compare-and-set mismatch")
    result=c.execute("""UPDATE product_feedback SET occurrence_count=?,last_seen_at=?,updated_at=?
        WHERE id=? AND occurrence_count=? AND last_seen_at=? AND updated_at=?""",
        (target["occurrence_count"],target["last_seen_at"],target["updated_at"],identity,
         expected["occurrence_count"],expected["last_seen_at"],expected["updated_at"]))
    if result.rowcount != 1: raise FixtureQuarantineError("recovery adjustment lost compare-and-set")
    c.execute("""INSERT INTO feedback_history(feedback_id,action,actor,at,from_status,to_status,details_json)
        VALUES (?,?,?,?,?,?,?)""",(identity,"fixture_recovery_adjustment",actor,timestamp,row["status"],row["status"],
        json.dumps({"manifest":digest,"expected":expected,"target":target,"preserved_history_sequences":adjustment.get("evidence_history_sequences",[])})))
    _audit(c,digest,"product_feedback",[identity],"compare_and_set_recurrence",actor,
           {"expected":expected,"target":target},timestamp)
    return 1


def _ensure_recovery_schema(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS fixture_quarantine_registry (
      table_name TEXT NOT NULL,record_key_json TEXT NOT NULL,manifest_sha256 TEXT NOT NULL,
      actor TEXT NOT NULL,reason TEXT NOT NULL,quarantined_at TEXT NOT NULL,
      PRIMARY KEY(table_name,record_key_json))""")
    c.execute("""CREATE TABLE IF NOT EXISTS fixture_quarantine_runs (
      manifest_sha256 TEXT PRIMARY KEY,file_sha256 TEXT NOT NULL,database_sha256_before TEXT NOT NULL,
      backup_path TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,applied_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS fixture_quarantine_audit (
      sequence INTEGER PRIMARY KEY AUTOINCREMENT,manifest_sha256 TEXT NOT NULL,table_name TEXT NOT NULL,
      record_key_json TEXT NOT NULL,action TEXT NOT NULL,actor TEXT NOT NULL,
      detail_json TEXT NOT NULL DEFAULT '{}',at TEXT NOT NULL)""")


def _audit(c: sqlite3.Connection,digest: str,table: str,key: list[Any],action: str,
           actor: str,detail: Mapping[str,Any],timestamp: str) -> None:
    c.execute("""INSERT INTO fixture_quarantine_audit
      (manifest_sha256,table_name,record_key_json,action,actor,detail_json,at)
      VALUES (?,?,?,?,?,?,?)""",(digest,table,_json_key(key),action,actor,json.dumps(detail,sort_keys=True,default=str),timestamp))


def _inner_digest(plan: Mapping[str,Any]) -> str:
    material={"candidate_by_table":plan.get("candidate_by_table",{}),
              "root_by_table":plan.get("root_by_table",{}),
              "dependent_by_table":plan.get("dependent_by_table",{}),
              "protected_ids":plan.get("protected_ids",[]),
              "recovery_adjustments":plan.get("recovery_adjustments",[]),
              "ambiguous_count":plan.get("ambiguous_count"),
              "protected_conflicts":plan.get("protected_conflicts",[])}
    return hashlib.sha256(json.dumps(material,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()


def _validate_manifest_contract(path: Path, evidence: Mapping[str,Any],
                                plan: Mapping[str,Any]) -> None:
    candidate = _key_set(plan.get("candidate_by_table"), "candidate")
    roots = _key_set(plan.get("root_by_table"), "root")
    dependents = _key_set(plan.get("dependent_by_table"), "dependent")
    if roots & dependents or roots | dependents != candidate:
        raise FixtureQuarantineError(
            "manifest root/dependent inventories must be disjoint and exactly reconcile candidates"
        )
    if set(plan.get("root_by_table") or {}) - set(_ROOT_ACTIONS):
        raise FixtureQuarantineError("manifest contains an undeclared root table")
    if set(plan.get("dependent_by_table") or {}) - set(_DEPENDENT_ACTIONS):
        raise FixtureQuarantineError("manifest contains an undeclared dependent table")
    if plan.get("root_count") != len(roots) or plan.get("dependent_count") != len(dependents) or plan.get("candidate_count") != len(candidate):
        raise FixtureQuarantineError("manifest inventory counts do not reconcile")
    operations = plan.get("safe_lifecycle_operations")
    if not isinstance(operations,list) or len(operations) != len(candidate):
        raise FixtureQuarantineError("manifest must declare exactly one operation per candidate")
    declared: set[tuple[str,str]] = set()
    for item in operations:
        if not isinstance(item,Mapping) or not isinstance(item.get("table"),str) or not isinstance(item.get("key"),list):
            raise FixtureQuarantineError("manifest operation has an invalid shape")
        token=(item["table"],_json_key(item["key"]))
        if token in declared or token not in candidate:
            raise FixtureQuarantineError("manifest contains a duplicate or undeclared operation")
        declared.add(token)
        is_root=token in roots
        expected_action=(_ROOT_ACTIONS if is_root else _DEPENDENT_ACTIONS).get(item["table"])
        expected_class=("root_recovery_action" if is_root else
            "dependent_append_only_invalidation" if item["table"]=="approval_snapshots"
            else "dependent_preserve_only")
        if item.get("operation") != expected_action or item.get("classification") != expected_class or item.get("requires_audit") is not True:
            raise FixtureQuarantineError("manifest operation violates the allowed table/action contract")
    if declared != candidate:
        raise FixtureQuarantineError("manifest operations do not reconcile candidates")
    with sqlite3.connect(f"file:{path}?mode=ro",uri=True) as c:
        for table,key_json in candidate:
            key=json.loads(key_json)
            pk=[row for row in c.execute(f'PRAGMA table_info("{table}")') if row[5]]
            pk.sort(key=lambda row:row[5])
            if len(pk)!=len(key) or not key:
                raise FixtureQuarantineError("manifest key shape does not match the declared table primary key")
            clauses=" AND ".join(f'"{row[1]}"=?' for row in pk)
            if c.execute(f'SELECT 1 FROM "{table}" WHERE {clauses}',tuple(key)).fetchone() is None:
                raise FixtureQuarantineError("manifest references a missing record")
        for key in (plan.get("root_by_table") or {}).get("product_feedback",[]):
            row=c.execute("SELECT summary FROM product_feedback WHERE id=?",(key[0],)).fetchone()
            if row is None or row[0] != "Approval dead-end blocked: queue":
                raise FixtureQuarantineError("product_feedback root is not an approved fixture signature")
        for key in (plan.get("root_by_table") or {}).get("sources",[]):
            row=c.execute(
                "SELECT title,url,body_summary,external_source_id FROM sources WHERE id=?",
                (key[0],),
            ).fetchone()
            if row is None or not _approved_source_fixture(row):
                raise FixtureQuarantineError("source root is not an approved fixture signature")


def _approved_source_fixture(row: sqlite3.Row | tuple[Any, ...]) -> bool:
    """Admit only unmistakable synthetic sources, never arbitrary explicit IDs."""
    title,url,body_summary,external_source_id=row
    hostname=(urlparse(str(url or "")).hostname or "").lower()
    external_id=str(external_source_id or "").lower()
    description=f'{title or ""} {body_summary or ""}'.lower()
    test_domain=hostname == "example.test" or hostname.endswith(".example.test")
    test_external_id=re.search(r"(?:^|[_-])test(?:$|[_-])",external_id) is not None
    test_description=re.search(r"\btest\b",description) is not None
    return test_domain and test_external_id and test_description


def _revalidate_typed_closure(path:Path,evidence:Mapping[str,Any],plan:Mapping[str,Any])->None:
    from .fixture_quarantine import fixture_quarantine_plan
    regenerated=fixture_quarantine_plan(
        path, plan.get("baseline"),
        preserve_ids=evidence.get("protected_ids_input") or plan.get("protected_ids") or [],
        fixture_ids=evidence.get("fixture_ids") or [],
        recovery_adjustments=plan.get("recovery_adjustments") or [],
    )
    for field in ("candidate_by_table","root_by_table","dependent_by_table"):
        if regenerated.get(field) != plan.get(field):
            raise FixtureQuarantineError("manifest does not match regenerated typed closure")


def _key_set(value:Any,label:str)->set[tuple[str,str]]:
    if not isinstance(value,Mapping):
        raise FixtureQuarantineError(f"manifest {label} inventory must be a table mapping")
    result:set[tuple[str,str]]=set()
    for table,keys in value.items():
        if not isinstance(table,str) or not isinstance(keys,list):
            raise FixtureQuarantineError(f"manifest {label} inventory has an invalid shape")
        for key in keys:
            if not isinstance(key,list):
                raise FixtureQuarantineError(f"manifest {label} key has an invalid shape")
            token=(table,_json_key(key))
            if token in result:
                raise FixtureQuarantineError(f"manifest {label} inventory contains duplicate keys")
            result.add(token)
    return result


def _require_operating_profile(path: Path) -> None:
    with sqlite3.connect(f"file:{path}?mode=ro",uri=True) as c:
        _require_operating_profile_connection(c)


def _require_operating_profile_connection(c: sqlite3.Connection) -> None:
    table = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='database_metadata'"
    ).fetchone()
    if table is None:
        raise FixtureQuarantineError("database profile metadata is missing")
    rows = c.execute("SELECT value FROM database_metadata WHERE key='profile'").fetchall()
    if len(rows) != 1 or rows[0][0] != "operating":
        value = "missing" if not rows else str(rows[0][0])
        raise FixtureQuarantineError(
            f"fixture recovery requires an explicit operating profile; found {value}"
        )


def _completed_run(path: Path,digest: str) -> sqlite3.Row|None:
    with sqlite3.connect(f"file:{path}?mode=ro",uri=True) as c:
        c.row_factory=sqlite3.Row
        exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fixture_quarantine_runs'").fetchone()
        return c.execute("SELECT * FROM fixture_quarantine_runs WHERE manifest_sha256=?",(digest,)).fetchone() if exists else None


def _create_verified_backup_locked(source: Path, target_value: str|Path) -> Path:
    target=Path(target_value).expanduser().resolve()
    target.parent.mkdir(parents=True,exist_ok=True)
    # Reserve the name atomically before SQLite opens it. This prevents a
    # symlink/existing-file race and establishes private permissions up front.
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    # A separate read connection can take SQLite's consistent online snapshot
    # while the caller's BEGIN IMMEDIATE connection continues holding the sole
    # writer reservation. No mutation has occurred on the caller yet.
    source_connection = sqlite3.connect(source)
    target_connection=sqlite3.connect(target)
    try: source_connection.backup(target_connection)
    except BaseException:
        source_connection.close(); target_connection.close(); target.unlink(missing_ok=True); raise
    else:
        source_connection.close(); target_connection.close()
    with sqlite3.connect(f"file:{target}?mode=ro",uri=True) as c:
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            target.unlink(missing_ok=True); raise FixtureQuarantineError("backup verification failed")
    if target.stat().st_mode & 0o077:
        target.unlink(missing_ok=True); raise FixtureQuarantineError("backup permissions are not private")
    return target


def _pk(c:sqlite3.Connection,table:str)->str:
    columns=[r[1] for r in c.execute(f'PRAGMA table_info("{table}")') if r[5]]
    if len(columns)!=1: raise FixtureQuarantineError(f"root table {table} lacks one-column primary key")
    return str(columns[0])


def _json_key(key:list[Any])->str:
    return json.dumps(key,separators=(",",":"),default=str)


def _sha256(path:Path)->str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__=["FixtureQuarantineError","execute_fixture_quarantine"]
