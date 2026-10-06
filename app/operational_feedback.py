"""Safe, deduplicated feedback projection for operational failures.

Operational exceptions may contain provider bodies, URLs, or credential material.
This boundary deliberately persists only structural diagnostics and allow-listed
resource identities.  Feedback lifecycle remains governed by ``FeedbackStore``;
reporting a recurrence increments evidence and never resolves or reopens an item.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Mapping
from uuid import UUID

from app import store


_RESOURCE_ID_KEYS = ("issue_id", "dispatch_item_id", "mission_id")


def mark_feedback_reported(error: Exception, result: Any) -> None:
    """Mark an exception only when feedback was durably persisted."""

    if isinstance(result, Mapping) and result.get("id"):
        try:
            setattr(error, "brand_os_feedback_reported", True)
        except Exception:
            pass


def report_terminal_job_failure(
    job: Mapping[str, Any], error: Exception, *, repository: Any = store,
) -> dict[str, Any]:
    """Project one needs-attention job into stable, credential-safe feedback."""

    job_type = str(job.get("job_type") or "unknown-job")
    payload = job.get("payload") if isinstance(job.get("payload"), Mapping) else {}
    account_id = str(job.get("connector_account_id") or payload.get("connector_account_id") or "")
    brand_id = job.get("brand_id") or payload.get("brand_id")
    resource_parts = [
        f"{key}={payload[key]}" for key in _RESOURCE_ID_KEYS
        if isinstance(payload.get(key), (str, int)) and str(payload[key]).strip()
    ]
    stream = payload.get("stream")
    if isinstance(stream, str) and stream.strip():
        resource_parts.append(f"stream={stream.strip()}")
    identity = "|".join([job_type, account_id or str(brand_id or "global"), *resource_parts])
    fingerprint = f"job-exhausted:{job_type}:" + sha256(identity.encode()).hexdigest()
    related_ids = _related_ids(job, payload, account_id)
    diagnostic = _safe_diagnostic(error)
    return repository.report_product_feedback(
        brand_id=brand_id,
        reporter="job-worker",
        summary=f"Operational job needs attention: {job_type}",
        details="A durable operation exhausted its retry policy and requires operator attention.",
        component=job_type,
        severity="high",
        fingerprint=fingerprint,
        reproduction=f"Retry durable {job_type} job {_safe_reference(job.get('id'))} after correcting its connection or input.",
        expected_behavior="The durable operation completes once or safely reconciles an earlier provider result.",
        actual_behavior=diagnostic,
        workaround="Inspect the connection and governed source record, then retry the same durable operation.",
        related_ids=related_ids,
    )


def report_orchestration_failure(
    decision: Mapping[str, Any], *, repository: Any = store,
) -> dict[str, Any]:
    """Project a scheduler enqueue failure without serializing its exception."""

    schedule_id = str(decision.get("schedule_id") or "unknown")
    account_id = str(decision.get("connector_account_id") or "")
    job_type = str(decision.get("job_type") or "unknown-job")
    fingerprint = f"orchestration-enqueue:{schedule_id}:{job_type}"
    related = _safe_references((decision.get("id"), schedule_id, account_id))
    return repository.report_product_feedback(
        brand_id=decision.get("brand_id"),
        reporter="periodic-orchestrator",
        summary="Periodic work could not be enqueued",
        details="A persisted schedule decision remains planned because its durable job could not be created.",
        component="orchestration.scheduler",
        severity="high",
        fingerprint=fingerprint,
        reproduction=f"Reconcile planned schedule decision {_safe_reference(decision.get('id'))} for {job_type}.",
        expected_behavior="Every due schedule decision creates or reconciles one idempotent durable job.",
        actual_behavior="Durable enqueue failed; provider or exception text was intentionally omitted.",
        workaround="Restore the job store, then run another bounded orchestration tick.",
        related_ids=related,
    )


def report_approval_dead_end(
    *, brand_id: str | None, resource_id: str, resource_type: str,
    operation: str, repository: Any = store,
) -> dict[str, Any]:
    resource_type = _safe_label(resource_type); operation = _safe_label(operation)
    return repository.report_product_feedback(
        brand_id=brand_id, reporter="approval-policy",
        summary=f"Approval dead-end blocked: {operation}",
        details="A governed action was requested without a usable exact-revision approval.",
        component=f"approval.{resource_type}", severity="medium",
        fingerprint=f"approval-dead-end:{resource_type}:{operation}:{_safe_reference(resource_id)}",
        reproduction=f"Retry {operation} for {_safe_reference(resource_id)} only after exact approval.",
        expected_behavior="Only the current materially approved revision can advance toward execution.",
        actual_behavior="Policy rejected the transition before any provider action.",
        workaround="Return the current revision to Chris for fact-check and explicit approval.",
        related_ids=_safe_references((resource_id,)),
    )


def report_missing_permission(
    *, brand_id: str | None, connector_account_id: str, provider: str,
    operation: str, missing_scopes: list[str], repository: Any = store,
) -> dict[str, Any]:
    provider = _safe_label(provider); operation = _safe_label(operation)
    scopes = sorted({_safe_label(scope) for scope in missing_scopes})
    identity = ",".join(scopes)
    return repository.report_product_feedback(
        brand_id=brand_id, reporter="connector-permission-policy",
        summary=f"Connector permission missing: {provider} {operation}",
        details="The configured connector cannot perform the requested capability with its declared scopes.",
        component=f"connector.{provider}.permissions", severity="high",
        fingerprint=f"missing-permission:{provider}:{operation}:{sha256(identity.encode()).hexdigest()}",
        reproduction=f"Inspect declared scopes for connector {_safe_reference(connector_account_id)}.",
        expected_behavior="The purpose-specific connector has every exact required scope and no broader role.",
        actual_behavior="Missing declared scopes: " + ", ".join(scopes),
        workaround="Reconnect the purpose-specific account with the exact displayed scopes.",
        related_ids=_safe_references((connector_account_id,)),
    )


def report_stale_metric(
    *, brand_id: str | None, metric: str, evidence_window: str,
    related_ids: list[str], repository: Any = store,
) -> dict[str, Any]:
    metric = _safe_label(metric); window = _safe_label(evidence_window)
    return repository.report_product_feedback(
        brand_id=brand_id, reporter="measurement-policy",
        summary=f"Measurement evidence is missing or stale: {metric}",
        details="A governed evaluation reached its review point without sufficient connector-backed evidence.",
        component="measurement.freshness", severity="medium",
        fingerprint=f"stale-metric:{metric}:{window}:" + sha256("|".join(sorted(related_ids)).encode()).hexdigest(),
        reproduction=f"Re-run the configured {window} measurement window for {metric}.",
        expected_behavior="The review window has current connector-backed observations for every compared artifact.",
        actual_behavior="The evidence guardrail was not met; no winner or learning was applied.",
        workaround="Restore measurement sync and retry recommendation after fresh evidence arrives.",
        related_ids=_safe_references(related_ids),
    )


def report_successful_workaround(
    *, brand_id: str | None, component: str, workaround_code: str,
    related_ids: list[str], repository: Any = store,
) -> dict[str, Any]:
    component = _safe_label(component); code = _safe_label(workaround_code)
    return repository.report_product_feedback(
        brand_id=brand_id, reporter="recovery-policy",
        summary=f"Operational workaround succeeded: {code}",
        details="A governed recovery path completed the immediate operation, but the triggering workflow gap remains visible.",
        component=component, severity="low",
        fingerprint=f"successful-workaround:{component}:{code}:" + sha256("|".join(sorted(related_ids)).encode()).hexdigest(),
        reproduction=f"Inspect recovery path {code} and its related durable records.",
        expected_behavior="The normal path commits canonical and operational state together without recovery.",
        actual_behavior="The recovery path safely reconciled already-completed canonical state.",
        workaround=f"Continue using governed recovery {code} until the underlying atomicity gap is removed.",
        related_ids=_safe_references(related_ids),
    )


def _safe_diagnostic(error: Exception) -> str:
    fields: dict[str, Any] = {"error_type": type(error).__name__}
    for name in ("category", "operation", "status_code", "retryable"):
        value = getattr(error, name, None)
        if isinstance(value, (str, int, bool)) or value is None:
            if value is not None:
                fields[name] = value
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))


def _related_ids(
    job: Mapping[str, Any], payload: Mapping[str, Any], account_id: str,
) -> list[str]:
    return _safe_references(
        (job.get("id"), account_id, *(payload.get(key) for key in _RESOURCE_ID_KEYS))
    )


def _safe_references(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, (str, int)) or not str(value).strip():
            continue
        reference = _safe_reference(value)
        if reference not in result:
            result.append(reference)
    return result


def _safe_reference(value: Any) -> str:
    raw = str(value or "unknown").strip()
    try:
        parsed = UUID(raw)
    except (ValueError, AttributeError):
        return "ref:sha256:" + sha256(raw.encode()).hexdigest()
    return str(parsed)


def _safe_label(value: Any) -> str:
    raw = str(value or "unknown").strip().casefold()
    if raw and len(raw) <= 80 and all(character.isalnum() or character in "._:-" for character in raw):
        return raw
    return "ref-" + sha256(raw.encode()).hexdigest()[:16]


__all__ = [
    "mark_feedback_reported", "report_approval_dead_end", "report_missing_permission",
    "report_orchestration_failure", "report_stale_metric", "report_successful_workaround",
    "report_terminal_job_failure",
]
