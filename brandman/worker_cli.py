"""Bounded Brand OS worker command suitable for cron or a process supervisor."""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from . import store
from .beehiiv_assisted_pull import (
    ASSISTED_BEEHIIV_PULL_JOB_TYPE,
    make_assisted_beehiiv_pull_handler,
)
from .connectors import RssConnector, UrllibTransport
from .canonical_revalidation import CanonicalPageFetcher, CanonicalSourceRevalidationStore
from .canonical_runtime import LazyHttpTransport, register_canonical_revalidation
from .connector_health import ConnectorHealthStore, register_connector_health
from .editorial import EditorialStore
from .engagement import EngagementInbox
from .experiments import (
    EXPERIMENT_WINDOW_COLLECT_JOB_TYPE, EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE,
    make_experiment_window_collection_handler, make_experiment_window_evaluation_handler,
)
from .kpi_projection import build_mission_kpi_projector
from .runtime import BrandOSRuntime
from .scheduler import OPERATING_PLAN_JOB_TYPE, PeriodicOrchestrator, make_operating_plan_handler
from .service_runtime import build_service_runtime_from_environment
from .source_campaign import SourceCampaignOperator


def build_secretless_assisted_runtime(
    worker_id: str, database: str | Path, *, transport_factory=None,
) -> tuple[BrandOSRuntime, PeriodicOrchestrator, dict]:
    """Compose only public-feed/internal jobs when no credential key is present."""
    database = Path(database)
    store.DATA_PATH = database
    store.init_db()
    editorial = EditorialStore(database)
    runtime = BrandOSRuntime(
        worker_id, retry_base_seconds=30,
        kpi_projector=build_mission_kpi_projector(str(database)),
        source_campaign_operator=SourceCampaignOperator(editorial),
        engagement_inbox=EngagementInbox(database),
        canonical_revalidation_store=CanonicalSourceRevalidationStore(database),
    )
    runtime.worker.register(
        OPERATING_PLAN_JOB_TYPE, make_operating_plan_handler(database),
    )
    runtime.worker.register(
        ASSISTED_BEEHIIV_PULL_JOB_TYPE,
        make_assisted_beehiiv_pull_handler(database),
    )
    read_ids = []
    transport_factory = transport_factory or (
        lambda _account: UrllibTransport(timeout_seconds=20)
    )
    for brand in store.rows("SELECT id FROM brands ORDER BY id"):
        for account in store.list_connector_accounts(brand["id"]):
            if account["connector_type"] != "rss" or account["status"] not in {"healthy", "connected"}:
                continue
            runtime.register_connector(
                account["id"], _rss_with_canonical(account, transport_factory(account)),
            )
            read_ids.append(account["id"])
    runtime.worker.register(
        EXPERIMENT_WINDOW_COLLECT_JOB_TYPE,
        make_experiment_window_collection_handler(database, ()),
    )
    register_canonical_revalidation(
        runtime.worker, database, LazyHttpTransport(transport_factory, {
            "id": "canonical-public-pages", "brand_id": "runtime",
            "connector_type": "rss", "account_key": "canonical-pages",
            "configuration": {},
        }),
    )
    runtime.worker.register(
        EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE,
        make_experiment_window_evaluation_handler(database),
    )
    health = ConnectorHealthStore(database)
    register_connector_health(runtime.worker, runtime.connectors, health)
    return runtime, PeriodicOrchestrator(database), {
        "mode": "assisted_secretless",
        "read_connector_account_ids": sorted(read_ids),
        "beehiiv_write_connector_account_id": None,
        "x_write_connector_account_id": None,
    }


def _rss_with_canonical(account: dict, transport) -> RssConnector:
    return RssConnector(
        account["account_key"], transport,
        canonical_revalidator=CanonicalPageFetcher(transport).safe_snapshot,
    )


def main() -> None:
    max_jobs = int(os.getenv("BRAND_OS_WORKER_MAX_JOBS", "100"))
    if max_jobs < 1 or max_jobs > 1000:
        raise SystemExit("BRAND_OS_WORKER_MAX_JOBS must be between 1 and 1000")
    mode = os.getenv("BRAND_OS_WORKER_MODE", "auto")
    if mode not in {"auto", "assisted_secretless", "native_api"}:
        raise SystemExit(
            "BRAND_OS_WORKER_MODE must be auto, assisted_secretless, or native_api"
        )
    master_key = os.getenv("BRAND_OS_CREDENTIAL_MASTER_KEY")
    if mode == "native_api" and not master_key:
        raise SystemExit(
            "BRAND_OS_WORKER_MODE=native_api requires BRAND_OS_CREDENTIAL_MASTER_KEY"
        )
    if mode == "native_api" or (mode == "auto" and master_key):
        service = build_service_runtime_from_environment(
            "brand-os-worker", store.DATA_PATH,
            lambda _account: UrllibTransport(timeout_seconds=20),
        )
        tick_runner = service
        runtime = service.runtime
        configuration = {"mode": "native_api", **service.configuration.as_dict()}
    else:
        runtime, tick_runner, configuration = build_secretless_assisted_runtime(
            "brand-os-worker", store.DATA_PATH,
        )
    max_decisions = int(os.getenv("BRAND_OS_SCHEDULER_MAX_DECISIONS", "50"))
    if max_decisions < 1 or max_decisions > 500:
        raise SystemExit("BRAND_OS_SCHEDULER_MAX_DECISIONS must be between 1 and 500")
    tick_runner.ensure_defaults() if isinstance(tick_runner, PeriodicOrchestrator) else None
    tick_result = tick_runner.tick(max_decisions=max_decisions)
    tick = tick_result.as_dict() if hasattr(tick_result, "as_dict") else tick_result
    result = runtime.run_until_idle(max_jobs=max_jobs)
    print(json.dumps({
        "tick": tick,
        "run": result.as_dict(),
        "configuration": configuration,
        "supervision": {
            "invocation_id": str(uuid4()),
            "pid": os.getpid(),
            "database": str(Path(store.DATA_PATH).expanduser().resolve()),
            "database_profile": store.database_profile(store.DATA_PATH),
            "worker_mode": mode,
        },
    }, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = ["build_secretless_assisted_runtime", "main"]
