"""Durable runtime integration for approved Beehiiv newsletter draft export."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path
from typing import Any, Protocol

from brandman import store
from brandman.beehiiv_delivery import BeehiivDraftDelivery, BeehiivDraftReceipt
from brandman.credentials import CredentialStore
from brandman.editorial import EditorialStore
from brandman.jobs import JobWorker
from brandman.operational_feedback import mark_feedback_reported


BEEHIIV_NEWSLETTER_EXPORT_JOB = "beehiiv.newsletter.export_draft"


class NewsletterExportJobError(RuntimeError):
    """The persisted export intent is no longer safe to execute."""


class FeedbackReporter(Protocol):
    def __call__(self, **fields: Any) -> Any: ...


ReconciliationHook = Callable[[BeehiivDraftReceipt, Mapping[str, Any]], Mapping[str, Any] | None]


def resolve_beehiiv_export_account(
    brand_id: str, connector_account_id: str | None = None,
    *, credentials: CredentialStore | None = None,
) -> dict[str, Any]:
    """Select one healthy, explicitly write-scoped Beehiiv account."""

    candidates = [
        account for account in store.list_connector_accounts(brand_id)
        if account.get("connector_type") == "beehiiv"
        and account.get("status") in {"healthy", "connected"}
        and "posts.write" in set(account.get("scopes") or [])
    ]
    if connector_account_id is not None:
        selected = next(
            (account for account in candidates if account["id"] == connector_account_id),
            None,
        )
        if selected is None:
            raise NewsletterExportJobError(
                "requested connector is not a healthy, write-scoped Beehiiv account for this brand"
            )
        return _require_export_credentials(selected, credentials)
    if not candidates:
        raise NewsletterExportJobError(
            "no healthy Beehiiv account with posts.write is configured for this brand"
        )
    if len(candidates) > 1:
        raise NewsletterExportJobError(
            "multiple writable Beehiiv accounts are configured; connector_account_id is required"
        )
    return _require_export_credentials(candidates[0], credentials)


def _require_export_credentials(
    account: dict[str, Any], credentials: CredentialStore | None
) -> dict[str, Any]:
    if credentials is None:
        return account
    try:
        metadata = credentials.get("beehiiv", account["account_key"])
    except KeyError as error:
        raise NewsletterExportJobError(
            "the Beehiiv draft-export credential is not connected"
        ) from error
    if (
        metadata.status != "connected"
        or not metadata.has_credentials
        or metadata.missing_scopes
        or metadata.excessive_scopes
        or "posts.write" not in metadata.granted_scopes
    ):
        raise NewsletterExportJobError(
            "the Beehiiv credential is not connected with least-privilege posts.write access"
        )
    return account


def get_newsletter_export_job(job_id: str) -> dict[str, Any]:
    """Return one safe, decoded newsletter export job."""

    job = store.row(
        "SELECT * FROM durable_jobs WHERE id=? AND job_type=?",
        (job_id, BEEHIIV_NEWSLETTER_EXPORT_JOB),
    )
    if job is None:
        raise KeyError(f"unknown newsletter export job: {job_id}")
    for field in ("payload", "result"):
        if isinstance(job.get(field), str):
            job[field] = json.loads(job[field])
    return job


def list_newsletter_export_jobs(issue_id: str) -> list[dict[str, Any]]:
    """List durable draft-export attempts for an issue, newest first."""

    jobs = store.rows(
        "SELECT * FROM durable_jobs WHERE job_type=? ORDER BY created_at DESC, id DESC",
        (BEEHIIV_NEWSLETTER_EXPORT_JOB,),
    )
    decoded: list[dict[str, Any]] = []
    for job in jobs:
        for field in ("payload", "result"):
            if isinstance(job.get(field), str):
                job[field] = json.loads(job[field])
        if (job.get("payload") or {}).get("issue_id") == issue_id:
            decoded.append(job)
    return decoded


def export_job_idempotency_key(issue_id: str, revision: int) -> str:
    if not issue_id.strip() or revision < 1:
        raise ValueError("issue_id and a positive revision are required")
    return f"beehiiv-newsletter-export:{issue_id}:r{revision}"


def enqueue_newsletter_export(
    editorial: EditorialStore,
    issue_id: str,
    *,
    brand_id: str | None = None,
    connector_account_id: str | None = None,
    run_after: str | None = None,
    priority: int = 10,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Persist exactly one export job for the currently approved revision."""

    if Path(editorial.database).resolve() != Path(store.DATA_PATH).resolve():
        raise NewsletterExportJobError("editorial store and durable job store must use the same database")
    issue = editorial.get_issue(issue_id)
    if issue["lifecycle"] != "approved" or not issue["approval_valid"]:
        raise NewsletterExportJobError("only a currently approved newsletter issue can be enqueued")
    if brand_id is not None and issue["brand_id"] != brand_id:
        raise NewsletterExportJobError("newsletter issue does not belong to the requested brand")
    revision = int(issue["current_revision"])
    key = export_job_idempotency_key(issue_id, revision)
    job = store.enqueue_job(
        BEEHIIV_NEWSLETTER_EXPORT_JOB,
        key,
        {"issue_id": issue_id, "revision": revision, "approval_revision": issue["approved_revision"]},
        brand_id=issue["brand_id"],
        connector_account_id=connector_account_id,
        run_after=run_after,
        priority=priority,
        max_attempts=max_attempts,
    )
    expected_payload = {"issue_id": issue_id, "revision": revision, "approval_revision": issue["approved_revision"]}
    if (
        job.get("brand_id") != issue["brand_id"]
        or job.get("connector_account_id") != connector_account_id
        or job.get("payload") != expected_payload
    ):
        raise NewsletterExportJobError("existing export job conflicts with the requested issue or connector")
    return job


class BeehiivNewsletterExportHandler:
    """Validate persisted intent, export one draft, then run reconciliation hooks."""

    def __init__(
        self,
        editorial: EditorialStore,
        delivery: BeehiivDraftDelivery,
        *,
        reconciliation_hooks: tuple[ReconciliationHook, ...] = (),
        feedback_reporter: FeedbackReporter = store.report_product_feedback,
    ) -> None:
        self.editorial = editorial
        self.delivery = delivery
        self.reconciliation_hooks = reconciliation_hooks
        self.feedback = feedback_reporter

    def __call__(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job.get("payload") or {}
        issue_id = str(payload.get("issue_id") or "")
        revision = payload.get("revision")
        if not issue_id or not isinstance(revision, int) or revision < 1:
            error = NewsletterExportJobError("export job is missing issue_id or revision")
            self._report(job, issue_id or "unknown", error)
            raise error
        expected_key = export_job_idempotency_key(issue_id, revision)
        if job.get("idempotency_key") != expected_key:
            error = NewsletterExportJobError("export job idempotency key does not match its issue revision")
            self._report(job, issue_id, error)
            raise error
        issue = self.editorial.get_issue(issue_id)
        valid_state = issue["lifecycle"] == "approved" or (
            issue["lifecycle"] in {"exported", "scheduled", "published"}
            and issue.get("beehiiv_external_id")
        )
        if (
            not valid_state
            or not issue["approval_valid"]
            or issue["current_revision"] != revision
            or issue["approved_revision"] != revision
        ):
            error = NewsletterExportJobError("approved newsletter revision changed before export execution")
            self._report(job, issue_id, error)
            raise error
        try:
            receipt = self.delivery.export_approved_draft(issue_id)
            hook_results = []
            for hook in self.reconciliation_hooks:
                result = hook(receipt, job)
                if result is not None:
                    hook_results.append(dict(result))
        except Exception as exc:
            self._report(job, issue_id, exc)
            raise
        return {
            "issue_id": receipt.issue_id,
            "revision": receipt.revision,
            "external_id": receipt.external_id,
            "preview_url": receipt.preview_url,
            "idempotency_key": receipt.idempotency_key,
            "payload_fingerprint": receipt.payload_fingerprint,
            "recovered": receipt.recovered,
            "reconciliation": hook_results,
        }

    def _report(self, job: Mapping[str, Any], issue_id: str, error: Exception) -> None:
        job_id = str(job.get("id") or "unknown")
        account_id = job.get("connector_account_id")
        feedback = self.feedback(
            brand_id=job.get("brand_id"),
            reporter="beehiiv-export-runtime",
            summary="Durable Beehiiv newsletter draft export failed",
            details="The approved newsletter export job failed and will follow its configured retry policy.",
            component=BEEHIIV_NEWSLETTER_EXPORT_JOB,
            severity="high",
            fingerprint=f"beehiiv-newsletter-export-job:{job_id}",
            reproduction=f"Run durable job {job_id} for newsletter issue {issue_id}.",
            expected_behavior="Export the exact approved revision to one Beehiiv draft and reconcile its external identity.",
            actual_behavior=(
                f"{type(error).__name__}; category={getattr(error, 'category', 'runtime')}; "
                f"status={getattr(error, 'status_code', None) or 'unavailable'}"
            ),
            workaround="Inspect connector feedback, reconcile by the stable export key, then retry the same job.",
            related_ids=[value for value in (job_id, issue_id, account_id) if value],
        )
        mark_feedback_reported(error, feedback)


def register_beehiiv_newsletter_export(
    target: JobWorker | Any,
    editorial: EditorialStore,
    delivery: BeehiivDraftDelivery,
    *,
    reconciliation_hooks: tuple[ReconciliationHook, ...] = (),
    feedback_reporter: FeedbackReporter = store.report_product_feedback,
) -> BeehiivNewsletterExportHandler:
    """Register on either a JobWorker or a BrandOSRuntime and return the handler."""

    worker = target.worker if hasattr(target, "worker") else target
    if not isinstance(worker, JobWorker):
        raise TypeError("target must be a JobWorker or BrandOSRuntime")
    handler = BeehiivNewsletterExportHandler(
        editorial,
        delivery,
        reconciliation_hooks=reconciliation_hooks,
        feedback_reporter=feedback_reporter,
    )
    worker.register(BEEHIIV_NEWSLETTER_EXPORT_JOB, handler)
    return handler


__all__ = [
    "BEEHIIV_NEWSLETTER_EXPORT_JOB", "NewsletterExportJobError",
    "BeehiivNewsletterExportHandler", "export_job_idempotency_key",
    "enqueue_newsletter_export", "get_newsletter_export_job",
    "list_newsletter_export_jobs", "register_beehiiv_newsletter_export",
    "resolve_beehiiv_export_account",
]
