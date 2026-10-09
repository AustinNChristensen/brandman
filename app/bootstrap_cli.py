"""Safe, bounded local bootstrap for the BrandMan MVP."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import os
from pathlib import Path
import sys
from typing import Any

from cryptography.fernet import Fernet

from app import store
from app.connector_health import HEALTH_CHECK_JOB_TYPE, make_connector_health_handler
from app.connectors import UrllibTransport
from app.jobs import JobWorker
from app.experiments import (
    EXPERIMENT_WINDOW_COLLECT_JOB_TYPE, EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE,
    make_experiment_window_collection_handler, make_experiment_window_evaluation_handler,
)
from app.readiness import LiveReadinessService
from app.scheduler import OPERATING_PLAN_JOB_TYPE, PeriodicOrchestrator, make_operating_plan_handler
from app.service_runtime import build_service_runtime
from app.sync import SYNC_JOB_TYPE
from app.execution_agents import ExecutionAgentRegistry
from app.beehiiv_assisted_pull import (
    ASSISTED_BEEHIIV_PULL_JOB_TYPE, make_assisted_beehiiv_pull_handler,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAFE_BOOTSTRAP_JOB_TYPES = frozenset({
    SYNC_JOB_TYPE, OPERATING_PLAN_JOB_TYPE, HEALTH_CHECK_JOB_TYPE,
    EXPERIMENT_WINDOW_COLLECT_JOB_TYPE, EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE,
    ASSISTED_BEEHIIV_PULL_JOB_TYPE,
})


def bootstrap(
    *,
    database: str | Path,
    slug: str = "demo-brand",
    environment: Mapping[str, str] | None = None,
    max_jobs: int = 10,
    max_decisions: int = 20,
    generated_master_key: str | None = None,
    execution_agent: str | None = None,
    execution_channel: str | None = None,
    transport_factory=None,
) -> dict[str, Any]:
    """Initialize and exercise safe read/internal jobs; never run delivery jobs."""
    if not 1 <= max_jobs <= 100:
        raise ValueError("max_jobs must be between 1 and 100")
    if not 1 <= max_decisions <= 500:
        raise ValueError("max_decisions must be between 1 and 500")
    env = dict(environment if environment is not None else os.environ)
    if generated_master_key is not None:
        env["BRAND_OS_CREDENTIAL_MASTER_KEY"] = generated_master_key
    if bool(execution_agent) != bool(execution_channel):
        raise ValueError("execution_agent and execution_channel must be provided together")
    database_path = Path(database).expanduser().resolve()
    store.DATA_PATH = database_path
    # Bind schema initialization to the explicitly supplied runtime identity.
    # Pytest deliberately has a process-wide ``test`` profile, while smoke and
    # proof harnesses may safely operate on a development/proof scratch DB.
    store.init_db(profile=env.get("BRAND_OS_DATABASE_PROFILE"))
    brand = store.get_brand(slug)
    if brand is None:
        raise ValueError(f"unknown brand: {slug}")
    if slug == "demo-brand":
        store.ensure_demo_brand_growth_mission()
    execution_agent_state = None
    if execution_agent and execution_channel:
        registry = ExecutionAgentRegistry(database_path)
        registry.configure(brand["id"], execution_agent, execution_channel)
        execution_agent_state = registry.heartbeat(brand["id"], execution_agent)

    scheduler = PeriodicOrchestrator(database_path)
    schedules = scheduler.ensure_defaults()
    tick = scheduler.tick(max_decisions=max_decisions).as_dict()
    before = LiveReadinessService(database_path, environment=env).inspect(slug)
    key_ready = _check(before, "credential_master_key")["status"] == "ready"
    worker = JobWorker("brand-os-bootstrap", retry_base_seconds=30)
    runtime_configuration = None
    runtime_error = None
    if key_ready:
        try:
            runtime = build_service_runtime(
                "brand-os-bootstrap-runtime", database_path,
                env["BRAND_OS_CREDENTIAL_MASTER_KEY"],
                transport_factory or (lambda _account: UrllibTransport(timeout_seconds=20)),
            )
            # Deliberately construct a restricted worker. The production runtime
            # also knows delivery jobs, but bootstrap must never execute them.
            worker.register(SYNC_JOB_TYPE, runtime.runtime._handle_sync)
            worker.register(
                HEALTH_CHECK_JOB_TYPE,
                make_connector_health_handler(
                    runtime.runtime.connectors, runtime.health_store,
                ),
            )
            runtime_configuration = runtime.configuration.as_dict()
        except Exception as exc:
            # Type only: arbitrary connector/config errors may contain secrets.
            runtime_error = f"runtime_configuration.{type(exc).__name__}"
    worker.register(
        OPERATING_PLAN_JOB_TYPE, make_operating_plan_handler(database_path),
    )
    readable_x_ids = [] if runtime_configuration is None else [
        account_id for account_id in runtime_configuration["read_connector_account_ids"]
        if (store.row("SELECT connector_type FROM connector_accounts WHERE id=?", (account_id,)) or {}).get("connector_type") == "x"
    ]
    worker.register(
        EXPERIMENT_WINDOW_COLLECT_JOB_TYPE,
        make_experiment_window_collection_handler(database_path, readable_x_ids),
    )
    worker.register(
        EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE,
        make_experiment_window_evaluation_handler(database_path),
    )
    worker.register(
        ASSISTED_BEEHIIV_PULL_JOB_TYPE,
        make_assisted_beehiiv_pull_handler(database_path),
    )
    jobs = []
    for _ in range(max_jobs):
        job = worker.run_once()
        if job is None:
            break
        jobs.append(job)
    after = LiveReadinessService(database_path, environment=env).inspect(slug)
    next_actions = _next_actions(after)
    if runtime_error:
        next_actions.insert(
            0, "Correct the public runtime account configuration, then rerun bootstrap."
        )
    optional_api_actions = list(dict.fromkeys(
        action for check in after["checks"]
        if not check.get("required_for_live", True)
        for action in check["actions"]
    ))
    return {
        "status": "ready" if after["ready"] else "needs_action",
        "database": str(database_path),
        "environment": {
            "preview_password_configured": bool(env.get("BRAND_OS_PREVIEW_PASSWORD")),
            "credential_master_key_configured": bool(env.get("BRAND_OS_CREDENTIAL_MASTER_KEY")),
        },
        "initialization": {
            "brand": slug,
            "schedules_discovered": len(schedules),
            "tick": tick,
        },
        "worker": {
            "safe_job_types": sorted(SAFE_BOOTSTRAP_JOB_TYPES),
            "jobs_processed": len(jobs),
            "jobs": jobs,
            "runtime_configuration": runtime_configuration,
            "runtime_error_code": runtime_error,
        },
        "assisted_execution": execution_agent_state,
        "readiness": after,
        "next_actions": next_actions,
        "optional_api_actions": optional_api_actions,
    }


def generate_master_key_file(path: str | Path) -> str:
    """Create one raw Fernet key with mode 0600; return it only in memory."""
    target = Path(path).expanduser().resolve()
    if target.name == ".env" or target.name.startswith(".env."):
        raise ValueError("credential master key cannot be written to an .env file")
    try:
        target.relative_to(PROJECT_ROOT)
    except ValueError:
        pass
    else:
        raise ValueError("credential master key file must be outside the project repository")
    if not target.parent.is_dir():
        raise ValueError("credential master key parent directory must already exist")
    key = Fernet.generate_key().decode("ascii")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, (key + "\n").encode("ascii"))
    finally:
        os.close(descriptor)
    os.chmod(target, 0o600)
    return key


def _check(report: dict[str, Any], check_id: str) -> dict[str, Any]:
    return next(item for item in report["checks"] if item["id"] == check_id)


def _next_actions(report: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(
        action for check in report["checks"]
        if check.get("required_for_live", True)
        for action in check["actions"]
    ))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brandman-bootstrap",
        description="Initialize BrandMan, run one bounded safe cycle, and report readiness.",
    )
    parser.add_argument("--database", default=str(store.DATA_PATH))
    parser.add_argument("--slug", default="demo-brand")
    parser.add_argument("--max-jobs", type=int, default=10)
    parser.add_argument("--max-decisions", type=int, default=20)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--execution-agent")
    parser.add_argument("--execution-channel", choices=("browser", "mcp"))
    parser.add_argument(
        "--generate-master-key", metavar="FILE",
        help="Explicitly generate a Fernet key in this new mode-0600 file outside the repository.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    generated_key = None
    try:
        if args.generate_master_key:
            generated_key = generate_master_key_file(args.generate_master_key)
        report = bootstrap(
            database=args.database, slug=args.slug, max_jobs=args.max_jobs,
            max_decisions=args.max_decisions, generated_master_key=generated_key,
            execution_agent=args.execution_agent,
            execution_channel=args.execution_channel,
        )
    except (OSError, ValueError) as error:
        # Validation messages are authored above and contain no secret values.
        raise SystemExit(f"brand-os-bootstrap: {error}") from None
    if args.generate_master_key:
        report["generated_master_key_file"] = str(
            Path(args.generate_master_key).expanduser().resolve()
        )
    if args.json_output:
        print(json.dumps(report, sort_keys=True))
        return
    readiness = report["readiness"]["summary"]
    print(
        f"BrandMan bootstrap: {report['status']}\n"
        f"Code ready: {readiness['code_ready_percent']}%\n"
        f"Live ready: {readiness['live_ready_percent']}%\n"
        f"Safe jobs processed: {report['worker']['jobs_processed']}"
    )
    if report["next_actions"]:
        print("Next actions:")
        for action in report["next_actions"]:
            print(f"- {action}")


if __name__ == "__main__":
    main(sys.argv[1:])
