"""Bounded Brand OS worker runtime.

This is an application boundary, not a daemon. Process managers or schedulers may
call ``run_once`` or ``run_until_idle``; both return control predictably and make
no network connections unless a registered connector does so while handling a
claimed job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from brandman import store
from brandman.connectors import ReadConnector
from brandman.jobs import JobWorker
from brandman.operational_feedback import mark_feedback_reported
from brandman.sync import SYNC_JOB_TYPE, SyncOrchestrator


class MissingConnectorError(LookupError):
    """A durable sync job has no runtime connector registration."""


@dataclass(frozen=True, slots=True)
class RuntimeRun:
    processed: int
    completed: int
    retrying: int
    needs_attention: int
    reached_limit: bool
    jobs: tuple[dict, ...]

    def as_dict(self) -> dict:
        return {
            "processed": self.processed,
            "completed": self.completed,
            "retrying": self.retrying,
            "needs_attention": self.needs_attention,
            "reached_limit": self.reached_limit,
            "jobs": list(self.jobs),
        }


class BrandOSRuntime:
    """Compose the durable worker with account-scoped connector instances."""

    def __init__(
        self,
        worker_id: str,
        connectors: Mapping[str, ReadConnector] | None = None,
        *,
        retry_base_seconds: int = 30,
        kpi_projector: object | None = None,
        source_campaign_operator: object | None = None,
        engagement_inbox: object | None = None,
        campaign_metric_projector: object | None = None,
        canonical_revalidation_store: object | None = None,
        newsletter_lifecycle_projector: object | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id cannot be empty")
        self.connectors: dict[str, ReadConnector] = dict(connectors or {})
        self.orchestrator = SyncOrchestrator(
            kpi_projector=kpi_projector,
            source_campaign_operator=source_campaign_operator,
            engagement_inbox=engagement_inbox,
            campaign_metric_projector=campaign_metric_projector,
            canonical_revalidation_store=canonical_revalidation_store,
            newsletter_lifecycle_projector=newsletter_lifecycle_projector,
        )
        self.worker = JobWorker(worker_id, retry_base_seconds=retry_base_seconds)
        self.worker.register(SYNC_JOB_TYPE, self._handle_sync)

    def register_connector(self, connector_account_id: str, connector: ReadConnector) -> None:
        """Add or replace the connector used for one persisted account."""
        if not connector_account_id.strip():
            raise ValueError("connector_account_id cannot be empty")
        self.connectors[connector_account_id] = connector

    def recover_stale_jobs(self, stale_before: str) -> int:
        """Release jobs abandoned before an explicit ISO-8601 cutoff."""
        return store.recover_stale_jobs(stale_before)

    def run_once(
        self, *, as_of: str | None = None, recover_stale_before: str | None = None
    ) -> dict | None:
        """Optionally recover stale leases, then process at most one job."""
        if recover_stale_before is not None:
            self.recover_stale_jobs(recover_stale_before)
        return self.worker.run_once(as_of=as_of)

    def run_until_idle(
        self,
        *,
        max_jobs: int = 100,
        as_of: str | None = None,
        recover_stale_before: str | None = None,
    ) -> RuntimeRun:
        """Process a bounded number of currently runnable jobs, then return."""
        if max_jobs < 1:
            raise ValueError("max_jobs must be at least 1")
        if recover_stale_before is not None:
            self.recover_stale_jobs(recover_stale_before)

        handled: list[dict] = []
        for _ in range(max_jobs):
            job = self.worker.run_once(as_of=as_of)
            if job is None:
                break
            handled.append(job)

        statuses = [job.get("status") for job in handled]
        return RuntimeRun(
            processed=len(handled),
            completed=statuses.count("completed"),
            retrying=statuses.count("retry"),
            needs_attention=statuses.count("needs_attention"),
            reached_limit=len(handled) == max_jobs,
            jobs=tuple(handled),
        )

    def _handle_sync(self, job: dict) -> dict:
        payload = job.get("payload") or {}
        account_id = payload.get("connector_account_id") or job.get("connector_account_id")
        brand_id = payload.get("brand_id") or job.get("brand_id")
        stream = payload.get("stream") or "content"
        account = store.row(
            "SELECT brand_id,status FROM connector_accounts WHERE id=?", (account_id,),
        ) if account_id else None
        if account is None or account["brand_id"] != brand_id:
            raise MissingConnectorError(f"no persisted connector account {account_id or 'unknown'}")
        if account["status"] not in {"healthy", "connected"}:
            # A disable can race with a worker claim. Complete the already
            # claimed job as a safe no-op so it cannot retry into a network call.
            return {
                "connector_account_id": account_id,
                "stream": stream,
                "skipped": True,
                "reason": "connector_account_disabled",
            }
        connector = self.connectors.get(account_id)
        if connector is None:
            # Report immediately rather than waiting for retry exhaustion. The
            # stable fingerprint turns repeated attempts into one visible issue.
            feedback = store.report_product_feedback(
                brand_id=brand_id,
                reporter="runtime",
                summary="Sync connector is not registered",
                details="A durable sync job cannot run on this worker because its account connector is unavailable.",
                component="connector.runtime",
                severity="high",
                fingerprint=f"runtime-missing-connector:{account_id or 'unknown'}",
                reproduction=f"Run durable job {job['id']} on worker {self.worker.worker_id}.",
                expected_behavior="The worker resolves the persisted connector account to a configured connector.",
                actual_behavior="No connector registration was found.",
                workaround="Configure the connector for this account and retry the job.",
                related_ids=[value for value in (job.get("id"), account_id) if value],
            )
            error = MissingConnectorError(
                f"no connector registered for account {account_id or 'unknown'}"
            )
            mark_feedback_reported(error, feedback)
            raise error
        if not brand_id:
            raise ValueError("sync job is missing brand_id")
        return self.orchestrator.sync_once(
            connector,
            brand_id=brand_id,
            connector_account_id=account_id,
            stream=stream,
        ).as_dict()


def build_runtime(
    worker_id: str,
    connectors: Mapping[str, ReadConnector] | None = None,
    *,
    retry_base_seconds: int = 30,
    kpi_projector: object | None = None,
    source_campaign_operator: object | None = None,
    engagement_inbox: object | None = None,
    campaign_metric_projector: object | None = None,
    canonical_revalidation_store: object | None = None,
) -> BrandOSRuntime:
    """Convenience factory for hosts that construct dependencies at startup."""
    return BrandOSRuntime(
        worker_id,
        connectors,
        retry_base_seconds=retry_base_seconds,
        kpi_projector=kpi_projector,
        source_campaign_operator=source_campaign_operator,
        engagement_inbox=engagement_inbox,
        campaign_metric_projector=campaign_metric_projector,
        canonical_revalidation_store=canonical_revalidation_store,
    )
