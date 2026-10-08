"""Fail-closed composition root for bounded Brand OS connector work.

The factory maps safe, persisted connection metadata to runtime adapters. It
does not start a daemon and performs no network requests while being built.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from brandman import store
from brandman.beehiiv_delivery import BeehiivDraftDelivery
from brandman.beehiiv_metrics import BeehiivCampaignMetricProjector
from brandman.beehiiv_lifecycle import BeehiivNewsletterLifecycleProjector
from brandman.beehiiv_runtime import register_beehiiv_newsletter_export
from brandman.canonical_revalidation import CanonicalPageFetcher, CanonicalSourceRevalidationStore
from brandman.canonical_runtime import LazyHttpTransport, register_canonical_revalidation
from brandman.connectors import BeehiivConnector, HttpTransport, RssConnector, WebsiteAnalyticsConnector
from brandman.credentials import ConnectionMetadata, CredentialStore
from brandman.connector_health import ConnectorHealthStore, register_connector_health
from brandman.dispatch import GovernedDispatcher, SQLiteDispatchStore
from brandman.editorial import EditorialStore
from brandman.engagement import EngagementInbox
from brandman.experiments import (
    EXPERIMENT_WINDOW_COLLECT_JOB_TYPE, EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE,
    make_experiment_window_collection_handler, make_experiment_window_evaluation_handler,
)
from brandman.kpi_projection import build_mission_kpi_projector
from brandman.runtime import BrandOSRuntime, RuntimeRun
from brandman.scheduler import OPERATING_PLAN_JOB_TYPE, PeriodicOrchestrator, make_operating_plan_handler
from brandman.source_campaign import SourceCampaignOperator
from brandman.x_delivery import XDeliveryJobHandler, XPublisherAdapter, register_x_delivery
from brandman.x_read import XReadConnector
from brandman.x_auth import XOAuthTokenSupplier
from brandman.provider_usage import MeteredTransport, ProviderUsageLedger
from brandman.website_metrics import CompositeCampaignMetricProjector, WebsiteCampaignMetricProjector


class ServiceRuntimeConfigurationError(RuntimeError):
    """Safe configuration failure that never includes credentials."""


class TransportFactory(Protocol):
    def __call__(self, connector_account: Mapping[str, Any]) -> HttpTransport: ...


@dataclass(frozen=True, slots=True)
class RuntimeConfiguration:
    """Credential-free summary suitable for APIs, logs, and agent context."""

    read_connector_account_ids: tuple[str, ...]
    beehiiv_write_connector_account_id: str | None
    x_write_connector_account_id: str | None
    skipped_connector_account_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "read_connector_account_ids": list(self.read_connector_account_ids),
            "beehiiv_write_connector_account_id": self.beehiiv_write_connector_account_id,
            "x_write_connector_account_id": self.x_write_connector_account_id,
            "skipped_connector_account_ids": list(self.skipped_connector_account_ids),
        }


class ServiceRuntime:
    """Bounded runtime façade; callers decide when and how often it runs."""

    def __init__(
        self,
        runtime: BrandOSRuntime,
        dispatcher: GovernedDispatcher,
        credential_store: CredentialStore,
        configuration: RuntimeConfiguration,
        scheduler: PeriodicOrchestrator,
        health_store: ConnectorHealthStore,
    ) -> None:
        self.runtime = runtime
        self.dispatcher = dispatcher
        self.credential_store = credential_store
        self.configuration = configuration
        self.scheduler = scheduler
        self.health_store = health_store

    def tick(self, *, as_of: str | None = None, max_decisions: int = 50) -> dict[str, Any]:
        """Discover safe defaults and enqueue one bounded set of due work."""
        self.scheduler.ensure_defaults()
        return self.scheduler.tick(as_of=as_of, max_decisions=max_decisions).as_dict()

    def run_once(
        self, *, as_of: str | None = None, recover_stale_before: str | None = None
    ) -> dict[str, Any] | None:
        return self.runtime.run_once(
            as_of=as_of, recover_stale_before=recover_stale_before
        )

    def run_until_idle(
        self,
        *,
        max_jobs: int = 100,
        as_of: str | None = None,
        recover_stale_before: str | None = None,
    ) -> RuntimeRun:
        return self.runtime.run_until_idle(
            max_jobs=max_jobs,
            as_of=as_of,
            recover_stale_before=recover_stale_before,
        )


def build_service_runtime(
    worker_id: str,
    database: str | Path,
    master_key: str | bytes | None,
    transport_factory: TransportFactory,
    *,
    retry_base_seconds: int = 30,
) -> ServiceRuntime:
    """Build connector and delivery workers from persisted, safe metadata.

    A valid deployment master key is mandatory. Read connectors are registered
    independently from write publishers; an X account becomes writable only
    when both metadata stores say it is healthy and ``tweet.write`` is granted.
    """
    database = Path(database)
    credential_store = CredentialStore(database, master_key)
    return _build_with_credential_store(
        worker_id,
        database,
        credential_store,
        transport_factory,
        retry_base_seconds=retry_base_seconds,
    )


def build_service_runtime_from_environment(
    worker_id: str,
    database: str | Path,
    transport_factory: TransportFactory,
    *,
    retry_base_seconds: int = 30,
) -> ServiceRuntime:
    """Environment-key integration API for the deployed application."""
    credential_store = CredentialStore.from_environment(database)
    # Construction above validates the environment without exposing the key.
    # Reuse its configured instance through the private composition helper.
    return _build_with_credential_store(
        worker_id,
        Path(database),
        credential_store,
        transport_factory,
        retry_base_seconds=retry_base_seconds,
    )


def _build_with_credential_store(
    worker_id: str,
    database: Path,
    credential_store: CredentialStore,
    transport_factory: TransportFactory,
    *,
    retry_base_seconds: int,
) -> ServiceRuntime:
    """Avoid decrypting or extracting a master key from an existing store."""
    # The public keyed factory is the canonical implementation. This helper
    # mirrors its composition while accepting an already validated store.
    if store.database_override() is None:
        store.DATA_PATH = database
    store.init_db()
    accounts = _connector_accounts()
    editorial = EditorialStore(database)
    engagement = EngagementInbox(database)
    runtime = BrandOSRuntime(
        worker_id,
        retry_base_seconds=retry_base_seconds,
        kpi_projector=build_mission_kpi_projector(str(database)),
        source_campaign_operator=SourceCampaignOperator(editorial),
        engagement_inbox=engagement,
        campaign_metric_projector=CompositeCampaignMetricProjector(
            BeehiivCampaignMetricProjector(database),
            WebsiteCampaignMetricProjector(database),
        ),
        canonical_revalidation_store=CanonicalSourceRevalidationStore(database),
        newsletter_lifecycle_projector=BeehiivNewsletterLifecycleProjector(database),
    )
    runtime.worker.register(OPERATING_PLAN_JOB_TYPE, make_operating_plan_handler(database))
    scheduler = PeriodicOrchestrator(database)
    health_store = ConnectorHealthStore(database)
    usage_ledger = ProviderUsageLedger(database)
    dispatcher = GovernedDispatcher(SQLiteDispatchStore(database))
    read_ids: list[str] = []
    skipped_ids: list[str] = []
    x_accounts: list[tuple[dict[str, Any], HttpTransport, Callable[[], str]]] = []
    beehiiv_writers: list[tuple[dict[str, Any], HttpTransport]] = []
    def transport_for(account: Mapping[str, Any]) -> HttpTransport:
        transport = transport_factory(account)
        provider = str(account.get("connector_type") or "").lower()
        if provider in {"x", "beehiiv"}:
            return MeteredTransport(
                transport, usage_ledger, brand_id=str(account["brand_id"]),
                connector_account_id=str(account["id"]), provider=provider,
                owned_user_id=str((account.get("configuration") or {}).get("user_id") or "") or None,
            )
        return transport

    for account in accounts:
        kind = str(account.get("connector_type") or "").lower()
        if not _metadata_healthy(account):
            skipped_ids.append(account["id"])
        elif kind == "rss":
            transport = transport_for(account)
            runtime.register_connector(account["id"], RssConnector(
                account["account_key"], transport,
                canonical_revalidator=CanonicalPageFetcher(transport).safe_snapshot,
            ))
            read_ids.append(account["id"])
        elif kind in {"beehiiv", "x", "website"}:
            metadata = _credential_metadata(credential_store, kind, account["account_key"])
            if kind == "beehiiv":
                can_read = _authorized(account, metadata, "posts.read")
                can_write = _authorized(account, metadata, "posts.write")
                if not can_read and not can_write:
                    skipped_ids.append(account["id"])
                    continue
                transport = transport_for(account)
                authorization = _authorization_supplier(
                    credential_store, kind, account["account_key"]
                )
                if can_read:
                    runtime.register_connector(account["id"], BeehiivConnector(
                        account["account_key"], transport, authorization,
                        include_publication_stats=_authorized(
                            account, metadata, "publications.read",
                        ),
                    ))
                    read_ids.append(account["id"])
                if can_write:
                    beehiiv_writers.append((account, transport))
            elif kind == "x":
                can_read = _authorized_scopes(
                    account, metadata, {"tweet.read", "users.read", "offline.access"}
                )
                can_write = _authorized_scopes(
                    account, metadata,
                    {"tweet.read", "users.read", "tweet.write", "offline.access"},
                )
                transport = transport_for(account) if can_read or can_write else None
                authorization = (
                    XOAuthTokenSupplier(
                        credential_store, account["account_key"], transport,
                    ) if transport is not None else None
                )
                if can_read:
                    configuration = account.get("configuration") or {}
                    if not all(configuration.get(key) for key in ("user_id", "username")):
                        skipped_ids.append(account["id"])
                    else:
                        runtime.register_connector(account["id"], XReadConnector(
                            str(configuration["user_id"]), str(configuration["username"]),
                            transport, authorization,
                            granted_scopes=metadata.granted_scopes,
                            searches=configuration.get("searches") or (),
                            target_user_ids=configuration.get("target_user_ids") or (),
                            include_profile_metrics=configuration.get("include_profile_metrics", True),
                            include_tweet_metrics=configuration.get("include_tweet_metrics", True),
                            include_mentions=configuration.get("include_mentions", True),
                            include_replies=configuration.get("include_replies", True),
                        ))
                        read_ids.append(account["id"])
                if can_write:
                    x_accounts.append((account, transport, authorization))
                if not can_read and not can_write:
                    skipped_ids.append(account["id"])
            else:
                can_read = _authorized_scopes(account, metadata, {"analytics.read"})
                configuration = account.get("configuration") or {}
                if can_read and configuration.get("endpoint_url"):
                    runtime.register_connector(account["id"], WebsiteAnalyticsConnector(
                        str(configuration["endpoint_url"]), transport_for(account),
                        _authorization_supplier(
                            credential_store, "website", account["account_key"]
                        ),
                        granted_scopes=metadata.granted_scopes,
                    ))
                    read_ids.append(account["id"])
                else:
                    skipped_ids.append(account["id"])
    if len(beehiiv_writers) > 1:
        raise ServiceRuntimeConfigurationError(
            "multiple writable Beehiiv accounts require account-scoped export routing"
        )
    if len(x_accounts) > 1:
        raise ServiceRuntimeConfigurationError(
            "multiple writable X accounts require account-scoped dispatch routing"
        )
    x_writer_id = x_accounts[0][0]["id"] if x_accounts else None
    beehiiv_writer_id = beehiiv_writers[0][0]["id"] if beehiiv_writers else None
    if beehiiv_writers:
        account, transport = beehiiv_writers[0]
        delivery = BeehiivDraftDelivery(
            editorial,
            account["account_key"],
            transport,
            _authorization_supplier(
                credential_store, "beehiiv", account["account_key"]
            ),
            # Beehiiv receives the stable idempotency key on every create. Local
            # export receipts are reconciled by BeehiivDraftDelivery before any
            # subsequent network request. A provider lookup can be added when a
            # supported idempotency-query endpoint is available.
            lambda _idempotency_key: None,
            store.report_product_feedback,
        )
        register_beehiiv_newsletter_export(runtime.worker, delivery.editorial, delivery)
    if x_accounts:
        account, transport, authorization = x_accounts[0]
        dispatcher.publishers["x"] = XPublisherAdapter(
            transport, authorization,
        )
        dispatcher.set_connector_gate("x", healthy=True, write_enabled=True)
        register_x_delivery(runtime.worker, XDeliveryJobHandler(dispatcher, account["id"]))
    readable_x_ids = [
        account_id for account_id in read_ids
        if any(account["id"] == account_id and account["connector_type"] == "x" for account in accounts)
    ]
    runtime.worker.register(
        EXPERIMENT_WINDOW_COLLECT_JOB_TYPE,
        make_experiment_window_collection_handler(database, readable_x_ids),
    )
    runtime.worker.register(
        EXPERIMENT_WINDOW_EVALUATE_JOB_TYPE,
        make_experiment_window_evaluation_handler(database),
    )
    register_canonical_revalidation(
        runtime.worker, database, LazyHttpTransport(transport_for, {
            "id": "canonical-public-pages", "brand_id": "runtime",
            "connector_type": "rss", "account_key": "canonical-pages",
            "configuration": {},
        }),
    )
    register_connector_health(runtime.worker, runtime.connectors, health_store)
    return ServiceRuntime(
        runtime, dispatcher, credential_store,
        RuntimeConfiguration(
            tuple(sorted(read_ids)), beehiiv_writer_id, x_writer_id,
            tuple(sorted(set(skipped_ids))),
        ),
        scheduler, health_store,
    )


def _connector_accounts() -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    for brand in store.rows("SELECT id FROM brands ORDER BY id"):
        accounts.extend(store.list_connector_accounts(brand["id"]))
    return accounts


def _credential_metadata(
    credentials: CredentialStore, provider: str, account_key: str
) -> ConnectionMetadata | None:
    try:
        return credentials.get(provider, account_key)
    except KeyError:
        return None


def _metadata_healthy(account: Mapping[str, Any]) -> bool:
    return account.get("status") in {"healthy", "connected"}


def _authorized(
    account: Mapping[str, Any],
    credential: ConnectionMetadata | None,
    required_scope: str,
) -> bool:
    return bool(
        credential is not None
        and credential.status == "connected"
        and credential.has_credentials
        and not credential.missing_scopes
        and not credential.excessive_scopes
        and required_scope in set(credential.granted_scopes)
        and required_scope in set(account.get("scopes") or [])
    )


def _authorized_scopes(
    account: Mapping[str, Any],
    credential: ConnectionMetadata | None,
    required_scopes: set[str],
) -> bool:
    """Require an exact, single-purpose credential for read/write separation."""
    return bool(
        credential is not None
        and credential.status == "connected"
        and credential.has_credentials
        and not credential.missing_scopes
        and not credential.excessive_scopes
        and set(credential.granted_scopes) == required_scopes
        and required_scopes <= set(account.get("scopes") or [])
    )


def _authorization_supplier(
    credentials: CredentialStore, provider: str, account_key: str
) -> Callable[[], str]:
    """Reveal a token only while the connector constructs Authorization."""
    def authorization_header() -> str:
        values = credentials.secret(provider, account_key).reveal()
        token = next(
            (
                values.get(name)
                for name in ("access_token", "token", "api_key")
                if isinstance(values.get(name), str) and values.get(name)
            ),
            None,
        )
        if token is None:
            raise ServiceRuntimeConfigurationError(
                f"{provider} credential payload has no supported token field"
            )
        return token if token.startswith("Bearer ") else f"Bearer {token}"

    return authorization_header


__all__ = [
    "RuntimeConfiguration",
    "ServiceRuntime",
    "ServiceRuntimeConfigurationError",
    "build_service_runtime",
    "build_service_runtime_from_environment",
]
