"""Durable runtime integration for canonical-page metadata revalidation."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

from . import store
from .canonical_revalidation import (
    CanonicalPageFetcher, CanonicalRevalidationError,
    CanonicalSourceRevalidationStore,
)
from .connectors import HttpTransport, canonical_url
from .jobs import JobWorker


CANONICAL_REVALIDATE_JOB_TYPE = "source.canonical_revalidate"


class LazyHttpTransport:
    """Delay transport construction until a canonical job actually performs I/O."""
    def __init__(self, factory, metadata: Mapping[str, Any]) -> None:
        self.factory = factory
        self.metadata = dict(metadata)
        self._transport = None

    def request(self, method: str, url: str, **kwargs):
        if self._transport is None:
            self._transport = self.factory(self.metadata)
        return self._transport.request(method, url, **kwargs)


def enqueue_canonical_revalidation(
    *, brand_id: str, source_id: str, idempotency_key: str,
    actor: str, repository: Any = store, run_after: str | None = None,
) -> dict[str, Any]:
    """Queue a source-bound public-page read; this performs no network call."""
    key = idempotency_key.strip()
    if not key or len(key) > 200:
        raise CanonicalRevalidationError("idempotency key must contain 1-200 characters")
    source = repository.row(
        "SELECT * FROM sources WHERE id=? AND brand_id=?", (source_id, brand_id),
    )
    if source is None:
        raise CanonicalRevalidationError("canonical source was not found for this brand")
    if source["source_type"] not in {"manual", "rss"}:
        raise CanonicalRevalidationError(
            "on-demand canonical revalidation supports governed manual or RSS sources"
        )
    url = canonical_url(source.get("url"))
    if not url or not url.startswith("https://"):
        raise CanonicalRevalidationError("canonical source requires a public HTTPS URL")
    job_key = f"{brand_id}:{key}"
    existing = repository.row(
        "SELECT * FROM durable_jobs WHERE job_type=? AND idempotency_key=?",
        (CANONICAL_REVALIDATE_JOB_TYPE, job_key),
    )
    requested_by = actor.strip() or "operator"
    if existing is not None:
        bound = json.loads(existing["payload"])
        expected = {
            "brand_id": brand_id, "source_id": source_id, "canonical_url": url,
            "requested_by": requested_by, "evidence_scope": "canonical_metadata_only",
        }
        if existing.get("brand_id") != brand_id or any(
            bound.get(name) != value for name, value in expected.items()
        ):
            raise CanonicalRevalidationError(
                "canonical revalidation idempotency key is bound to another request"
            )
        return repository.enqueue_job(
            CANONICAL_REVALIDATE_JOB_TYPE, job_key, bound,
            brand_id=brand_id, run_after=existing.get("run_after"),
        )
    observed_at = repository.now()
    payload = {
        "brand_id": brand_id, "source_id": source_id, "canonical_url": url,
        "observed_at": observed_at, "requested_by": requested_by,
        "evidence_scope": "canonical_metadata_only",
    }
    queued = repository.enqueue_job(
        CANONICAL_REVALIDATE_JOB_TYPE, job_key, payload,
        brand_id=brand_id, run_after=run_after,
    )
    # The shared durable queue is globally keyed. Bind a replay to every
    # immutable request field instead of returning another source's work.
    if queued.get("brand_id") != brand_id or queued.get("payload") != payload:
        raise CanonicalRevalidationError(
            "canonical revalidation idempotency key is bound to another request"
        )
    return queued


class CanonicalRevalidationJobHandler:
    def __init__(self, database: str | Path, transport: HttpTransport) -> None:
        self.records = CanonicalSourceRevalidationStore(database)
        self.fetcher = CanonicalPageFetcher(transport)

    def __call__(self, job: Mapping[str, Any]) -> dict[str, Any]:
        payload = job.get("payload") or {}
        brand_id = str(payload.get("brand_id") or job.get("brand_id") or "")
        source_id = str(payload.get("source_id") or "")
        source = store.row(
            "SELECT * FROM sources WHERE id=? AND brand_id=?", (source_id, brand_id),
        )
        if source is None:
            raise CanonicalRevalidationError("canonical source was not found for this brand")
        source_url = canonical_url(source.get("url"))
        if source_url != payload.get("canonical_url"):
            raise CanonicalRevalidationError(
                "canonical source URL changed after this job was queued; enqueue a fresh review"
            )
        snapshot = self.fetcher.safe_snapshot(
            source_url,
            feed_title=str(source.get("title") or ""),
            feed_summary=str(source.get("body_summary") or ""),
            feed_fingerprint="sha256:" + sha256(
                f"{source.get('title') or ''}\n{source.get('body_summary') or ''}".encode()
            ).hexdigest(),
            observed_at=str(payload.get("observed_at") or ""),
        )
        snapshot["idempotency_key"] = f"job:{job['id']}"
        evidence = self.records.record(
            brand_id, source_id, snapshot, actor="canonical-revalidation-worker",
        )
        return {
            "evidence": evidence,
            "evidence_scope": "canonical_metadata_only",
            "semantic_fact_check": False,
            "operator_message": (
                "Canonical page metadata was captured. Claims still require governed fact checking."
            ),
        }


def register_canonical_revalidation(
    worker: JobWorker, database: str | Path, transport: HttpTransport,
) -> CanonicalRevalidationJobHandler:
    handler = CanonicalRevalidationJobHandler(database, transport)
    worker.register(CANONICAL_REVALIDATE_JOB_TYPE, handler)
    return handler


__all__ = [
    "CANONICAL_REVALIDATE_JOB_TYPE", "CanonicalRevalidationJobHandler",
    "LazyHttpTransport", "enqueue_canonical_revalidation", "register_canonical_revalidation",
]
