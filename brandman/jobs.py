from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from brandman import store
from brandman.operational_feedback import report_terminal_job_failure


JobHandler = Callable[[dict[str, Any]], dict[str, Any] | None]


class JobWorker:
    """Small database-backed worker with durable attempts and bounded retries."""

    def __init__(self, worker_id: str, *, retry_base_seconds: int = 30) -> None:
        self.worker_id = worker_id
        self.retry_base_seconds = retry_base_seconds
        self.handlers: dict[str, JobHandler] = {}

    def register(self, job_type: str, handler: JobHandler) -> None:
        self.handlers[job_type] = handler

    def run_once(self, *, as_of: str | None = None) -> dict[str, Any] | None:
        claimed = store.claim_next_job(
            self.worker_id,
            job_types=list(self.handlers),
            as_of=as_of,
        )
        if not claimed:
            return None
        attempt_id = claimed.pop("attempt_id")
        try:
            result = self.handlers[claimed["job_type"]](claimed)
        except Exception as exc:
            retry_at = None
            if claimed["attempt_count"] < claimed["max_attempts"]:
                delay = self.retry_base_seconds * (2 ** (claimed["attempt_count"] - 1))
                retry_clock = datetime.fromisoformat(as_of) if as_of else datetime.now(UTC)
                retry_at = (retry_clock + timedelta(seconds=delay)).isoformat()
            failed = store.fail_job(claimed["id"], attempt_id, str(exc), retry_at=retry_at)
            if (
                failed and failed["status"] == "needs_attention"
                and not getattr(exc, "brand_os_feedback_reported", False)
            ):
                # ``fail_job`` reloads the raw SQLite row; preserve the decoded,
                # claimed payload so feedback can link only allow-listed resource IDs.
                report_terminal_job_failure({**failed, "payload": claimed.get("payload") or {}}, exc)
            return failed
        return store.complete_job(claimed["id"], attempt_id, result)
