"""Offline operator utilities for a standalone BrandMan installation."""

from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable

from app import store
from app.attribution_store import AttributionStore
from app.approval_snapshots import ApprovalSnapshotStore
from app.bootstrap_cli import bootstrap
from app.connector_health import ConnectorHealthStore
from app.canonical_revalidation import CanonicalSourceRevalidationStore
from app.content_dispatch import CanonicalPostDispatchService
from app.dispatch import GovernedDispatcher, SQLiteDispatchStore
from app.distribution_package import DistributionPackageStore
from app.editorial import EditorialStore
from app.engagement import EngagementInbox
from app.execution_agents import ExecutionAgentRegistry
from app.execution_handoff import ExecutionHandoffStore
from app.experiments import ExperimentStore
from app.feedback import FeedbackStore
from app.provider_usage import ProviderUsageLedger
from app.performance_planning import PerformancePlanningEngine
from app.readiness import LiveReadinessService
from app.scheduler import PeriodicOrchestrator
from app.third_party_sources import ThirdPartySourceService


def initialize_all(database: str | Path, *, profile: str | None = "operating") -> dict[str, Any]:
    """Apply every idempotent local schema initializer without provider access."""
    path = Path(database).expanduser().resolve()
    store.DATA_PATH = path
    store.init_db(profile=profile)
    editorial = EditorialStore(path)
    CanonicalSourceRevalidationStore(path)
    PerformancePlanningEngine(path)
    dispatch_store = SQLiteDispatchStore(path)
    dispatcher = GovernedDispatcher(dispatch_store)
    DistributionPackageStore(path, editorial, dispatcher)
    ApprovalSnapshotStore(path)
    AttributionStore(path)
    FeedbackStore(path)
    EngagementInbox(path)
    ExperimentStore(path)
    ConnectorHealthStore(path)
    ProviderUsageLedger(path)
    ExecutionAgentRegistry(path)
    ThirdPartySourceService(path)
    ExecutionHandoffStore(path, editorial, dispatcher)
    PeriodicOrchestrator(path).ensure_defaults()
    return database_audit(path)


def beehiiv_private_draft_manifest(
    database: str | Path, *, task_id: str, asset_path: str | Path,
    existing_draft_id: str, profile: str = "operating",
    public_asset_url: str | None = None,
    allowed_asset_hosts: set[str] | None = None,
) -> dict[str, Any]:
    """Prepare the exact approved Beehiiv browser manifest without an HTTP service."""

    from app.main import initialize_application_services

    services = initialize_application_services(database, profile=profile)
    manifest = services.execution_handoff_store.beehiiv_private_draft_manifest(
        task_id, asset_path=asset_path, existing_draft_id=existing_draft_id,
    )
    if public_asset_url:
        if not allowed_asset_hosts:
            raise ValueError("--allowed-asset-host is required with --public-asset-url")
        from app.beehiiv_assisted_publisher import attach_verified_public_asset
        manifest = attach_verified_public_asset(
            manifest, public_asset_url, allowed_hosts=allowed_asset_hosts,
        )
    return manifest


def database_audit(database: str | Path) -> dict[str, Any]:
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise ValueError("database file does not exist")
    with _connect_readonly(path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        counts = {
            table: connection.execute(
                f'SELECT COUNT(*) FROM "{table.replace(chr(34), chr(34) * 2)}"'
            ).fetchone()[0]
            for table in tables
        }
    return {
        "database": str(path), "integrity": integrity,
        "healthy": integrity == "ok", "tables": len(tables),
        "row_counts": counts, "profile": store.database_profile(path),
    }


def operability_audit(
    database: str | Path, *, brand_slug: str = "demo-brand",
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    result = database_audit(database)
    path = Path(database).expanduser().resolve()
    with _connect_readonly(path) as connection:
        brand = connection.execute(
            "SELECT id FROM brands WHERE slug=?", (brand_slug,),
        ).fetchone()
    if brand is None:
        raise ValueError(f"unknown brand: {brand_slug}")
    readiness = LiveReadinessService(
        path, environment=environment if environment is not None else os.environ,
    ).inspect(brand_slug)
    result["brand"] = brand_slug
    result["readiness"] = {
        "ready": readiness["ready"], **readiness["summary"],
        "next_actions": list(dict.fromkeys(
            action for check in readiness["checks"]
            if check.get("required_for_live", True)
            for action in check["actions"]
        )),
    }
    if "provider_usage_events" in result["row_counts"]:
        with _connect_readonly(path) as connection:
            usage = connection.execute(
                """SELECT COUNT(*) AS request_count,
                          SUM(CASE WHEN estimated_cost IS NULL THEN 1 ELSE 0 END)
                            AS unpriced_request_count
                   FROM provider_usage_events WHERE brand_id=?""", (brand[0],),
            ).fetchone()
        result["provider_usage"] = {
            "request_count": usage[0],
            "unpriced_request_count": usage[1] or 0,
        }
    else:
        result["provider_usage"] = {
            "status": "schema_missing", "action": "Run brandman-ops migrate.",
        }
    return result


def create_backup(database: str | Path, destination: str | Path) -> dict[str, Any]:
    source = Path(database).expanduser().resolve()
    target = _new_output(destination)
    if not source.is_file():
        target.unlink(missing_ok=True)
        raise ValueError("database file does not exist")
    try:
        with sqlite3.connect(source) as source_connection, sqlite3.connect(target) as target_connection:
            source_connection.backup(target_connection)
        os.chmod(target, 0o600)
        result = database_audit(target)
        if not result["healthy"]:
            raise ValueError("backup integrity check failed")
        return {"backup": str(target), **result}
    except Exception:
        target.unlink(missing_ok=True)
        raise


def restore_backup(backup: str | Path, destination: str | Path) -> dict[str, Any]:
    source = Path(backup).expanduser().resolve()
    verified = database_audit(source)
    if not verified["healthy"]:
        raise ValueError("backup integrity check failed")
    # Restore is intentionally create-only. Operators must choose a new target,
    # verify it, and switch service configuration separately.
    result = create_backup(source, destination)
    result["restored_database"] = result.pop("backup")
    return result


def export_usage(
    database: str | Path, brand_slug: str, destination: str | Path, format: str,
) -> dict[str, Any]:
    path = Path(database).expanduser().resolve()
    store.DATA_PATH = path
    store.init_db(profile=store.database_profile(path))
    brand = store.get_brand(brand_slug)
    if not brand:
        raise ValueError(f"unknown brand: {brand_slug}")
    report = ProviderUsageLedger(path).report(brand["id"])
    target = _new_output(destination)
    try:
        with target.open("w", encoding="utf-8", newline="") as output:
            if format == "json":
                json.dump(report, output, sort_keys=True)
                output.write("\n")
            elif format == "csv":
                fields = [
                    "provider", "method", "endpoint", "billable_category",
                    "status_code", "outcome", "units", "unit_name",
                    "estimated_cost", "currency", "pricing_version_id",
                    "provider_request_id", "response_resource_count",
                    "connector_account_id", "observed_at",
                ]
                writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
                writer.writeheader(); writer.writerows(report["events"])
            else:
                raise ValueError("format must be json or csv")
        os.chmod(target, 0o600)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {"file": str(target), "format": format, "rows": report["request_count"]}


def local_soak(database: str | Path, *, cycles: int) -> dict[str, Any]:
    if not 1 <= cycles <= 1000:
        raise ValueError("cycles must be between 1 and 1000")
    path = Path(database).expanduser().resolve()
    if not path.exists():
        initialize_all(path, profile="development")
    profile = store.require_database_profile(
        path, {"development", "test", "proof"}, operation="smoke/soak harness",
    )
    runs = []
    for _ in range(cycles):
        runs.append(bootstrap(
            database=path, environment={"BRAND_OS_DATABASE_PROFILE": profile},
            max_jobs=25, max_decisions=25,
        ))
    audit = database_audit(path)
    return {
        "cycles": cycles, "database": str(path), "profile": profile,
        "integrity": audit["integrity"],
        "healthy": audit["healthy"],
        "jobs_processed": sum(run["worker"]["jobs_processed"] for run in runs),
        "external_delivery_job_types_executed": sorted({
            job["job_type"] for run in runs for job in run["worker"]["jobs"]
            if job["job_type"] in {"x.dispatch", "beehiiv.newsletter_export"}
        }),
        "last_readiness": runs[-1]["readiness"]["summary"],
    }


def reconcile_legacy_dispatches(
    database: str | Path, *, apply: bool = False,
) -> dict[str, Any]:
    """Plan or atomically reconcile payload-linked attributed X duplicates."""
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise ValueError("database file does not exist")
    store.DATA_PATH = path
    store.init_db(profile=store.database_profile(path))
    dispatcher = GovernedDispatcher(SQLiteDispatchStore(path))
    service = CanonicalPostDispatchService(dispatcher)
    canonical_ids = sorted({
        str(item.payload.get("canonical_post_id"))
        for item in dispatcher.store.list_items()
        if item.canonical_post_id is None
        and isinstance(item.payload.get("canonical_post_id"), str)
    })
    plans = []
    for canonical_post_id in canonical_ids:
        canonical = store.row(
            """SELECT p.*,c.brand_id,c.id AS canonical_campaign_id,b.slug AS brand_slug
               FROM posts p JOIN campaigns c ON c.id=p.campaign_id
               JOIN brands b ON b.id=c.brand_id WHERE p.id=?""",
            (canonical_post_id,),
        )
        if canonical is None or canonical.get("channel") != "x":
            continue
        body = canonical.get("body")
        if not isinstance(body, str):
            continue
        plan = service.reconcile_legacy_attribution_duplicate(
            canonical, body, actor="ops:dispatch-reconciliation", apply=apply,
        )
        if plan is not None:
            plans.append(plan)
    return {
        "database": str(path), "dry_run": not apply,
        "candidates": len(plans), "reconciliations": plans,
        "external_actions": 0,
    }


def _new_output(path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    if not target.parent.is_dir():
        raise ValueError("output parent directory does not exist")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    return target


def _connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return connection


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brandman-ops")
    parser.add_argument("--database", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    migrate = sub.add_parser("migrate")
    migrate.add_argument(
        "--profile", choices=sorted(store.DATABASE_PROFILES), default="operating",
        help="Persist the database role on first initialization (default: operating).",
    )
    sub.add_parser("audit")
    backup = sub.add_parser("backup"); backup.add_argument("destination")
    verify = sub.add_parser("backup-verify"); verify.add_argument("backup")
    restore = sub.add_parser("restore-to-new"); restore.add_argument("backup"); restore.add_argument("destination")
    rate = sub.add_parser("rate-card-add")
    rate.add_argument(
        "--brand", default="demo-brand",
        help="Brand slug that owns this customer-supplied rate card.",
    )
    for name in ("version", "provider", "method", "endpoint_pattern", "billable_category", "unit_name", "unit_price", "currency", "effective_at", "actor"):
        rate.add_argument("--" + name.replace("_", "-"), required=True)
    usage = sub.add_parser("usage-export")
    usage.add_argument("--brand", default="demo-brand")
    usage.add_argument("--format", choices=("json", "csv"), required=True)
    usage.add_argument("destination")
    smoke = sub.add_parser("smoke"); smoke.add_argument("--cycles", type=int, default=1)
    soak = sub.add_parser("soak"); soak.add_argument("--cycles", type=int, default=100)
    proof = sub.add_parser("supervisor-proof")
    proof.add_argument(
        "--report",
        help="Create this new mode-0600 JSON evidence report (optional).",
    )
    proof_verify = sub.add_parser("supervisor-proof-verify")
    proof_verify.add_argument("report")
    reconcile = sub.add_parser("reconcile-dispatches")
    reconcile.add_argument("--apply", action="store_true")
    beehiiv_manifest = sub.add_parser("beehiiv-draft-manifest")
    beehiiv_manifest.add_argument("--task-id", required=True)
    beehiiv_manifest.add_argument("--asset-path", required=True)
    beehiiv_manifest.add_argument("--existing-draft-id", required=True)
    beehiiv_manifest.add_argument("--public-asset-url")
    beehiiv_manifest.add_argument("--allowed-asset-host", action="append", default=[])
    beehiiv_manifest.add_argument(
        "--profile", choices=sorted(store.DATABASE_PROFILES), default="operating",
    )
    quarantine = sub.add_parser("fixture-quarantine-plan")
    quarantine.add_argument("--baseline", required=True)
    quarantine.add_argument("--preserve-id", action="append", default=[])
    quarantine.add_argument(
        "--fixture-id", action="append", default=[],
        help="Exact, independently verified fixture ID (repeatable).",
    )
    quarantine_apply = sub.add_parser("fixture-quarantine-apply")
    quarantine_apply.add_argument("manifest")
    quarantine_apply.add_argument("--file-sha256", required=True)
    quarantine_apply.add_argument("--confirm-manifest-sha256")
    quarantine_apply.add_argument("--backup")
    quarantine_apply.add_argument("--actor", required=True)
    quarantine_apply.add_argument("--reason", required=True)
    quarantine_apply.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.command == "migrate": result = initialize_all(args.database, profile=args.profile)
        elif args.command == "audit": result = operability_audit(args.database)
        elif args.command == "backup": result = create_backup(args.database, args.destination)
        elif args.command == "backup-verify": result = database_audit(args.backup)
        elif args.command == "restore-to-new": result = restore_backup(args.backup, args.destination)
        elif args.command == "usage-export": result = export_usage(args.database, args.brand, args.destination, args.format)
        elif args.command in {"smoke", "soak"}: result = local_soak(args.database, cycles=args.cycles)
        elif args.command == "supervisor-proof":
            from app.supervisor_proof import run_supervisor_proof
            result = run_supervisor_proof(args.database, args.report)
        elif args.command == "supervisor-proof-verify":
            from app.supervisor_proof import verify_supervisor_evidence
            result = verify_supervisor_evidence(args.report, args.database)
            if not result["valid"]:
                # Verification failures are expected operator outcomes, not parser
                # failures. Preserve the safe structured diagnosis so automation
                # can distinguish a changed report from a changed/missing database.
                print(json.dumps(result, sort_keys=True))
                raise SystemExit(1)
        elif args.command == "reconcile-dispatches":
            result = reconcile_legacy_dispatches(args.database, apply=args.apply)
        elif args.command == "beehiiv-draft-manifest":
            result = beehiiv_private_draft_manifest(
                args.database, task_id=args.task_id, asset_path=args.asset_path,
                existing_draft_id=args.existing_draft_id, profile=args.profile,
                public_asset_url=args.public_asset_url,
                allowed_asset_hosts=set(args.allowed_asset_host),
            )
        elif args.command == "fixture-quarantine-plan":
            from app.fixture_quarantine import fixture_quarantine_plan
            result = fixture_quarantine_plan(
                args.database, args.baseline, preserve_ids=args.preserve_id,
                fixture_ids=args.fixture_id,
            )
        elif args.command == "fixture-quarantine-apply":
            from app.fixture_quarantine_apply import execute_fixture_quarantine
            result = execute_fixture_quarantine(
                args.database, args.manifest,
                expected_file_sha256=args.file_sha256,
                confirmation_manifest_sha256=args.confirm_manifest_sha256,
                backup_path=args.backup, actor=args.actor, reason=args.reason,
                apply=args.apply,
            )
        elif args.command == "rate-card-add":
            database = Path(args.database).expanduser().resolve()
            with _connect_readonly(database) as connection:
                brand = connection.execute(
                    "SELECT id FROM brands WHERE slug=?", (args.brand,),
                ).fetchone()
            if brand is None:
                raise ValueError(f"unknown brand: {args.brand}")
            result = ProviderUsageLedger(args.database).configure_price(
                brand_id=brand[0],
                version=args.version, provider=args.provider, method=args.method,
                endpoint_pattern=args.endpoint_pattern,
                billable_category=args.billable_category, unit_name=args.unit_name,
                unit_price=args.unit_price, currency=args.currency,
                effective_at=args.effective_at, actor=args.actor,
            )
        else: raise ValueError("unsupported command")
    except (OSError, sqlite3.Error, ValueError) as error:
        raise SystemExit(f"brandman-ops: {error}") from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1:])


__all__ = [
    "beehiiv_private_draft_manifest", "create_backup", "database_audit", "export_usage", "initialize_all",
    "local_soak", "main", "operability_audit", "reconcile_legacy_dispatches",
    "restore_backup",
]
