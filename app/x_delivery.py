"""Durable, approval-gated delivery of BrandMan dispatch items to X.

This module is an application composition boundary.  It deliberately keeps
credentials and HTTP behind injected callables, while reusing the governed
dispatcher for exact-revision approval, atomic claims, and publish-once state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from app import store
from app.connectors import (
    DispatchReceipt,
    HttpTransport,
    XConnector,
    XPostRequest,
)
from app.dispatch import (
    ApprovalRequired,
    DispatchBlocked,
    DispatchItem,
    GovernedDispatcher,
    InvalidTransition,
    Lifecycle,
    PublishResult,
)
from app.jobs import JobWorker
from app.operational_feedback import mark_feedback_reported


X_DELIVERY_JOB_TYPE = "x.dispatch"
X_MEASUREMENT_JOB_TYPE = "x.metrics.sync"


class XDeliveryError(RuntimeError):
    """A delivery attempt did not produce a durable published result."""


class XConnectionBlocked(XDeliveryError):
    """The persisted X account is not currently authorized for writes."""


class ReceiptReconciler(Protocol):
    """Resolve an ambiguous request using its stable provider identity."""

    def __call__(
        self,
        idempotency_key: str,
        expected_external_id: str | None = None,
    ) -> DispatchReceipt | PublishResult | None: ...


class FeedbackRepository(Protocol):
    def enqueue_job(
        self,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]: ...

    def report_product_feedback(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_connector_accounts(self, brand_id: str) -> list[dict[str, Any]]: ...


class XPublisherAdapter:
    """Translate a governed dispatch item into an approved X API request.

    ``reconcile`` is invoked only after the provider boundary raises.  A host
    can implement it with an idempotency lookup or a known external ID; this
    closes the ambiguous-success gap without issuing a second POST.
    """

    def __init__(
        self,
        transport: HttpTransport,
        credential_supplier: Callable[[], str],
        *,
        reconciler: ReceiptReconciler | None = None,
        base_url: str = "https://api.x.com/2",
    ) -> None:
        self.connector = XConnector(
            transport,
            credential_supplier,
            base_url=base_url,
        )
        self.reconciler = reconciler

    def __call__(self, item: DispatchItem, idempotency_key: str) -> PublishResult:
        if item.connector != "x":
            raise ValueError("X publisher received a non-X dispatch item")
        if item.status not in {Lifecycle.QUEUED, Lifecycle.FAILED, Lifecycle.NEEDS_ATTENTION}:
            raise ApprovalRequired("X delivery requires a queued dispatch item")
        if item.approval is None or item.approval.revision != item.revision:
            raise ApprovalRequired("X delivery requires approval for the exact revision")

        text = item.payload.get("text", item.payload.get("body"))
        if not isinstance(text, str) or not text.strip():
            raise ValueError("X dispatch payload requires non-empty text or body")
        reply_to = item.payload.get("reply_to_post_id")
        if reply_to is not None and not isinstance(reply_to, str):
            raise ValueError("reply_to_post_id must be a string")

        request = XPostRequest(
            text=text,
            idempotency_key=idempotency_key,
            approved=True,
            reply_to_post_id=reply_to,
        )
        try:
            receipt = self.connector.publish(request)
        except Exception:
            receipt = self._reconcile(item, idempotency_key)
            if receipt is None:
                raise
        return PublishResult(receipt.external_id, receipt.external_url)

    def _reconcile(
        self, item: DispatchItem, idempotency_key: str
    ) -> DispatchReceipt | PublishResult | None:
        if self.reconciler is None:
            return None
        expected = item.payload.get("expected_external_id")
        expected_external_id = expected if isinstance(expected, str) else None
        return self.reconciler(idempotency_key, expected_external_id)


@dataclass(slots=True)
class XDeliveryJobHandler:
    """Execute one durable X delivery job and queue its measurement exactly once."""

    dispatcher: GovernedDispatcher
    connector_account_id: str
    repository: FeedbackRepository = store
    actor: str = "x-delivery-worker"

    def __call__(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job.get("payload") or {}
        item_id = payload.get("dispatch_item_id")
        expected_revision = payload.get("revision")
        if not isinstance(item_id, str) or not item_id:
            error = XDeliveryError("X delivery job is missing dispatch_item_id")
            feedback = self._feedback(
                job,
                summary="X delivery job is missing its dispatch item",
                actual="dispatch_item_id is absent from the durable job payload.",
                fingerprint=f"x-delivery:missing-item:{job.get('id', 'unknown')}",
            )
            mark_feedback_reported(error, feedback)
            raise error

        try:
            item = self.dispatcher.store.get(item_id)
        except KeyError as error:
            failure = XDeliveryError(str(error))
            feedback = self._feedback(
                job,
                summary="X delivery dispatch item no longer exists",
                actual=str(error),
                fingerprint=f"x-delivery:unknown-item:{item_id}",
                related_ids=[item_id],
            )
            mark_feedback_reported(failure, feedback)
            raise failure from error

        self._validate_item(job, item, expected_revision)
        self._validate_connection(job, item)

        try:
            published = self.dispatcher.dispatch(item.id, actor=self.actor)
        except (DispatchBlocked, ApprovalRequired, InvalidTransition) as error:
            failure = XDeliveryError(str(error))
            feedback = self._feedback(
                job,
                summary="X delivery is blocked by governance",
                actual=str(error),
                fingerprint=f"x-delivery:governance:{item.id}:{item.revision}:{type(error).__name__}",
                related_ids=[item.id, self.connector_account_id],
            )
            mark_feedback_reported(failure, feedback)
            raise failure from error

        if published.status is not Lifecycle.PUBLISHED or not published.external_id:
            message = published.last_error or "X provider did not return a publication receipt"
            category = "permission" if _looks_like_permission_failure(message) else "provider"
            safe_actual = (
                "The X provider rejected the authorized request."
                if category == "permission"
                else "The X provider did not produce a publication receipt."
            )
            error = XDeliveryError(message)
            feedback = self._feedback(
                job,
                summary=f"X delivery {category} failure",
                actual=safe_actual,
                fingerprint=f"x-delivery:{category}:{item.id}:{item.revision}",
                related_ids=[item.id, self.connector_account_id],
                severity="high",
            )
            # Keep retriable work in the only externally executable lifecycle
            # state. The approval and idempotency key remain unchanged; after
            # the final durable attempt the item stays visibly failed.
            if int(job.get("attempt_count") or 0) < int(job.get("max_attempts") or 0):
                self.dispatcher.queue(published.id, actor=self.actor)
            mark_feedback_reported(error, feedback)
            raise error

        measurement = self.repository.enqueue_job(
            X_MEASUREMENT_JOB_TYPE,
            f"x-measure:{published.id}:revision:{published.revision}:external:{published.external_id}",
            {
                "dispatch_item_id": published.id,
                "external_id": published.external_id,
                "external_url": published.external_url,
                "connector_account_id": self.connector_account_id,
                "stream": "metrics",
            },
            brand_id=published.brand_id or job.get("brand_id"),
            connector_account_id=self.connector_account_id,
        )
        return {
            "dispatch_item_id": published.id,
            "revision": published.revision,
            "external_id": published.external_id,
            "external_url": published.external_url,
            "measurement_job_id": measurement["id"],
        }

    def _validate_item(
        self, job: Mapping[str, Any], item: DispatchItem, expected_revision: Any
    ) -> None:
        reason: str | None = None
        if item.connector != "x":
            reason = "dispatch item is not an X action"
        elif item.status is not Lifecycle.QUEUED:
            reason = f"dispatch item is {item.status.value}, not queued"
        elif not isinstance(expected_revision, int) or expected_revision != item.revision:
            reason = f"job revision {expected_revision!r} does not match item revision {item.revision}"
        elif item.approval is None or item.approval.revision != item.revision:
            reason = f"dispatch item lacks approval for revision {item.revision}"
        elif not item.idempotency_key:
            reason = "queued dispatch item has no stable idempotency key"
        if reason is None:
            return
        error = XDeliveryError(reason)
        feedback = self._feedback(
            job,
            summary="X delivery approval state is invalid",
            actual=reason,
            fingerprint=f"x-delivery:approval:{item.id}:{item.revision}:{reason}",
            related_ids=[item.id, self.connector_account_id],
        )
        mark_feedback_reported(error, feedback)
        raise error

    def _validate_connection(self, job: Mapping[str, Any], item: DispatchItem) -> None:
        accounts = self.repository.list_connector_accounts(
            item.brand_id or str(job.get("brand_id") or "")
        )
        account = next(
            (candidate for candidate in accounts if candidate.get("id") == self.connector_account_id),
            None,
        )
        reason: str | None = None
        if account is None:
            reason = "persisted X connector account was not found"
        elif account.get("connector_type") != "x":
            reason = "connector account is not an X account"
        elif account.get("status") not in {"healthy", "connected"}:
            reason = f"connector account is {account.get('status', 'unknown')}"
        elif not {"tweet.read", "users.read", "tweet.write", "offline.access"} <= set(
            account.get("scopes") or []
        ):
            reason = "connector account is missing the exact durable X writer scopes"
        if reason is None:
            return
        error = XConnectionBlocked(reason)
        feedback = self._feedback(
            job,
            summary="X connection cannot publish",
            actual=reason,
            fingerprint=f"x-delivery:connection:{self.connector_account_id}:{reason}",
            related_ids=[item.id, self.connector_account_id],
            severity="high",
            workaround=(
                "Reconnect X with tweet.read, users.read, tweet.write, and offline.access; "
                "verify health, then retry the job."
            ),
        )
        mark_feedback_reported(error, feedback)
        raise error

    def _feedback(
        self,
        job: Mapping[str, Any],
        *,
        summary: str,
        actual: str,
        fingerprint: str,
        related_ids: list[str] | None = None,
        severity: str = "high",
        workaround: str = "Correct the condition and retry the durable job.",
    ) -> dict[str, Any]:
        return self.repository.report_product_feedback(
            brand_id=job.get("brand_id"),
            reporter=self.actor,
            summary=summary,
            details=actual,
            component="x.delivery",
            severity=severity,
            fingerprint=fingerprint,
            reproduction=f"Run X delivery job {job.get('id', 'unknown')}.",
            expected_behavior="A healthy authorized X connection publishes the approved revision exactly once.",
            actual_behavior=actual,
            workaround=workaround,
            related_ids=related_ids or [str(job.get("id", "unknown"))],
        )


def enqueue_x_delivery(
    dispatch_item: DispatchItem,
    connector_account_id: str,
    *,
    repository: FeedbackRepository = store,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Durably schedule one exact dispatch-item revision for X delivery."""
    if dispatch_item.connector != "x":
        raise ValueError("only X dispatch items can be enqueued for X delivery")
    if dispatch_item.status is not Lifecycle.QUEUED:
        raise ValueError("X dispatch item must be queued before delivery is enqueued")
    if dispatch_item.approval is None or dispatch_item.approval.revision != dispatch_item.revision:
        raise ApprovalRequired("X dispatch item lacks exact-revision approval")
    if not dispatch_item.idempotency_key:
        raise ValueError("queued X dispatch item has no idempotency key")
    return repository.enqueue_job(
        X_DELIVERY_JOB_TYPE,
        f"x-delivery:{dispatch_item.id}:revision:{dispatch_item.revision}",
        {
            "dispatch_item_id": dispatch_item.id,
            "revision": dispatch_item.revision,
            "connector_account_id": connector_account_id,
        },
        brand_id=dispatch_item.brand_id,
        connector_account_id=connector_account_id,
        max_attempts=max_attempts,
    )


def register_x_delivery(worker: JobWorker, handler: XDeliveryJobHandler) -> None:
    """Register X delivery on an existing durable worker/runtime."""
    worker.register(X_DELIVERY_JOB_TYPE, handler)


def _looks_like_permission_failure(message: str) -> bool:
    lowered = message.casefold()
    return any(token in lowered for token in ("http 401", "http 403", "permission", "scope", "forbidden"))


__all__ = [
    "XConnectionBlocked",
    "XDeliveryError",
    "XDeliveryJobHandler",
    "XPublisherAdapter",
    "X_DELIVERY_JOB_TYPE",
    "X_MEASUREMENT_JOB_TYPE",
    "enqueue_x_delivery",
    "register_x_delivery",
]
