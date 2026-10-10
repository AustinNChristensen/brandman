"""Approved-only Beehiiv draft export at the external connector boundary.

The adapter never schedules, confirms, or publishes.  It receives dependencies
explicitly so credentials, HTTP, reconciliation, waits, and feedback remain
outside the editorial domain and are straightforward to exercise in tests.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html import escape
import json
import re
import time
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from app.connectors import HttpResponse, HttpTransport
from app.editorial import EditorialStore


class FeedbackReporter(Protocol):
    def __call__(self, **fields: Any) -> Any: ...


class BeehiivDeliveryError(RuntimeError):
    """A safe delivery failure that contains no token or provider response body."""

    def __init__(self, category: str, message: str, *, status_code: int | None = None, retryable: bool = False):
        self.category = category
        self.status_code = status_code
        self.retryable = retryable
        suffix = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(message + suffix)


@dataclass(frozen=True, slots=True)
class BeehiivDraftReceipt:
    issue_id: str
    revision: int
    external_id: str
    preview_url: str | None
    idempotency_key: str
    payload_fingerprint: str | None
    recovered: bool = False


ProviderLookup = Callable[[str], Mapping[str, Any] | None]


def render_safe_body(sections: list[Any], cta: Mapping[str, Any] | None = None) -> str:
    """Render structured issue content as conservative, semantic HTML."""

    chunks: list[str] = []
    for section in sections:
        if isinstance(section, str):
            if section.strip():
                chunks.append(f"<p>{escape(section.strip())}</p>")
            continue
        if not isinstance(section, Mapping):
            continue
        heading = str(section.get("heading") or section.get("title") or "").strip()
        body = str(section.get("body") or section.get("content") or "").strip()
        if heading:
            chunks.append(f"<h2>{escape(heading)}</h2>")
        if body:
            chunks.extend(_render_body_blocks(body))
    if cta:
        label = str(cta.get("label") or cta.get("text") or "").strip()
        url = str(cta.get("url") or "").strip()
        parts = urlsplit(url)
        if label and parts.scheme.lower() in {"http", "https"} and parts.netloc:
            chunks.append(f'<p><a href="{escape(url, quote=True)}">{escape(label)}</a></p>')
        elif label:
            chunks.append(f"<p><strong>{escape(label)}</strong></p>")
    return "\n".join(chunks)


_ORDERED_ITEM = re.compile(r"^\s*\d+[.)]\s+(.+)$")
_UNORDERED_ITEM = re.compile(r"^\s*[-*]\s+(.+)$")
_SAFE_MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")


def _render_body_blocks(body: str) -> list[str]:
    blocks: list[str] = []
    paragraph: list[str] = []
    list_kind: str | None = None
    list_items: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{'<br>'.join(_render_inline(line) for line in paragraph)}</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_kind
        if list_items and list_kind:
            blocks.append(f"<{list_kind}>" + "".join(
                f"<li>{_render_inline(item)}</li>" for item in list_items
            ) + f"</{list_kind}>")
            list_items.clear()
        list_kind = None

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            flush_list()
            continue
        ordered = _ORDERED_ITEM.match(line)
        unordered = _UNORDERED_ITEM.match(line)
        next_kind = "ol" if ordered else "ul" if unordered else None
        if next_kind:
            flush_paragraph()
            if list_kind and list_kind != next_kind:
                flush_list()
            list_kind = next_kind
            list_items.append((ordered or unordered).group(1))
            continue
        flush_list()
        paragraph.append(line)
    flush_paragraph()
    flush_list()
    return blocks


def _render_inline(value: str) -> str:
    chunks: list[str] = []
    cursor = 0
    for match in _SAFE_MARKDOWN_LINK.finditer(value):
        chunks.append(escape(value[cursor:match.start()]))
        label, url = match.groups()
        chunks.append(f'<a href="{escape(url, quote=True)}">{escape(label)}</a>')
        cursor = match.end()
    chunks.append(escape(value[cursor:]))
    return "".join(chunks)


class BeehiivDraftDelivery:
    """Export the currently approved revision to a Beehiiv draft exactly once."""

    def __init__(
        self,
        editorial: EditorialStore,
        publication_id: str,
        transport: HttpTransport,
        authorization_header: Callable[[], str],
        lookup_by_idempotency_key: ProviderLookup,
        feedback_reporter: FeedbackReporter,
        *,
        base_url: str = "https://api.beehiiv.com/v2",
        max_async_polls: int = 3,
        max_retry_after_seconds: float = 10,
        wait: Callable[[float], None] = time.sleep,
    ) -> None:
        if not publication_id.strip():
            raise ValueError("publication_id is required")
        if max_async_polls < 0 or max_async_polls > 20:
            raise ValueError("max_async_polls must be between 0 and 20")
        if max_retry_after_seconds < 0:
            raise ValueError("max_retry_after_seconds cannot be negative")
        self.editorial = editorial
        self.publication_id = publication_id
        self.transport = transport
        self._authorization_header = authorization_header
        self.lookup = lookup_by_idempotency_key
        self.feedback = feedback_reporter
        self.base_url = base_url.rstrip("/")
        self.max_async_polls = max_async_polls
        self.max_retry_after_seconds = max_retry_after_seconds
        self.wait = wait

    def export_approved_draft(self, issue_id: str) -> BeehiivDraftReceipt:
        existing_issue = self.editorial.get_issue(issue_id)
        if (
            existing_issue["lifecycle"] in {"exported", "scheduled", "published"}
            and existing_issue.get("beehiiv_external_id")
            and existing_issue.get("approval_valid")
        ):
            # A worker may retry after the durable receipt committed but before
            # its job completion committed.  Treat local state as authoritative
            # and never issue another provider create.
            revision = int(existing_issue["current_revision"])
            return BeehiivDraftReceipt(
                issue_id=issue_id,
                revision=revision,
                external_id=str(existing_issue["beehiiv_external_id"]),
                preview_url=existing_issue.get("beehiiv_preview_url"),
                idempotency_key=f"newsletter-export:beehiiv:{issue_id}:r{revision}",
                payload_fingerprint=None,
                recovered=True,
            )
        prepared = self.editorial.prepare_export(issue_id, connector="beehiiv")
        payload = prepared["payload"]
        key = prepared["idempotency_key"]

        # A preceding attempt may have reached Beehiiv but failed before BrandMan
        # recorded the receipt.  Reconcile before issuing another create.
        reconciled = self._lookup_safely(key, issue_id, report_failure=False)
        if reconciled is not None:
            return self._record(issue_id, prepared, reconciled, recovered=True)

        request_body = {
            "status": "draft",
            "title": payload["title"],
            "subtitle": payload["preview_text"],
            "subject_line": payload["subject"],
            "body_content": render_safe_body(payload["sections"], payload["cta"]),
            "idempotency_key": key,
        }
        # Guard this adapter's central invariant against future payload changes.
        forbidden = {"publish", "published", "scheduled", "send_at", "schedule_at"}
        if request_body["status"] != "draft" or forbidden.intersection(request_body):
            raise AssertionError("Beehiiv delivery may create drafts only")
        try:
            response = self.transport.request(
                "POST",
                f"{self.base_url}/publications/{self.publication_id}/posts",
                headers={
                    "Authorization": self._authorization_header(),
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Idempotency-Key": key,
                },
                json_body=request_body,
            )
        except Exception as exc:
            recovered = self._lookup_safely(key, issue_id, report_failure=False)
            if recovered is not None:
                return self._record(issue_id, prepared, recovered, recovered=True)
            self._report(issue_id, "provider", None, "Create request ended without a response", retryable=True)
            raise BeehiivDeliveryError("provider", "Beehiiv draft creation outcome is unknown", retryable=True) from exc

        try:
            provider = self._resolve_response(response, key, issue_id)
        except BeehiivDeliveryError:
            raise
        except Exception as exc:
            recovered = self._lookup_safely(key, issue_id, report_failure=False)
            if recovered is not None:
                return self._record(issue_id, prepared, recovered, recovered=True)
            self._report(issue_id, "provider", None, "Async draft creation outcome is unknown", retryable=True)
            raise BeehiivDeliveryError("provider", "Beehiiv draft creation outcome is unknown", retryable=True) from exc
        return self._record(issue_id, prepared, provider, recovered=False)

    def _resolve_response(self, response: HttpResponse, key: str, issue_id: str) -> Mapping[str, Any]:
        if response.status_code == 202:
            location = response.headers.get("Location") or response.headers.get("location")
            current = response
            for _ in range(self.max_async_polls):
                self.wait(self._retry_after(current))
                if location:
                    current = self.transport.request(
                        "GET", urljoin(self.base_url + "/", location),
                        headers={"Authorization": self._authorization_header(), "Accept": "application/json"},
                    )
                    if current.status_code != 202:
                        return self._resolve_response(current, key, issue_id)
                    location = current.headers.get("Location") or current.headers.get("location") or location
                else:
                    found = self._lookup_safely(key, issue_id, report_failure=False)
                    if found is not None:
                        return found
            found = self._lookup_safely(key, issue_id, report_failure=False)
            if found is not None:
                return found
            self._report(issue_id, "provider", 202, "Async draft creation did not complete within the bounded poll window", retryable=True)
            raise BeehiivDeliveryError("provider", "Beehiiv draft creation is still pending", status_code=202, retryable=True)

        if not 200 <= response.status_code < 300:
            # Server failures and timeouts can happen after a successful create.
            if response.status_code >= 500:
                found = self._lookup_safely(key, issue_id, report_failure=False)
                if found is not None:
                    return found
            category = self._failure_category(response)
            self._report(issue_id, category, response.status_code, "Beehiiv rejected draft creation", retryable=response.status_code >= 500)
            raise BeehiivDeliveryError(
                category, "Beehiiv draft creation failed", status_code=response.status_code,
                retryable=response.status_code >= 500,
            )
        try:
            document = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            self._report(issue_id, "provider", response.status_code, "Beehiiv returned an unreadable success response", retryable=True)
            raise BeehiivDeliveryError("provider", "Beehiiv returned an invalid response", status_code=response.status_code, retryable=True) from exc
        data = document.get("data", document) if isinstance(document, Mapping) else {}
        if not isinstance(data, Mapping) or not (data.get("id") or data.get("post_id")):
            found = self._lookup_safely(key, issue_id, report_failure=False)
            if found is not None:
                return found
            self._report(issue_id, "provider", response.status_code, "Beehiiv response omitted the draft ID", retryable=True)
            raise BeehiivDeliveryError("provider", "Beehiiv response omitted the draft ID", status_code=response.status_code, retryable=True)
        status = str(data.get("status") or "draft").lower()
        if status != "draft":
            self._report(issue_id, "provider", response.status_code, f"Provider returned forbidden state {status!r}", retryable=False)
            raise BeehiivDeliveryError("provider", "Beehiiv did not create a draft", status_code=response.status_code)
        return data

    def _record(
        self, issue_id: str, prepared: Mapping[str, Any], provider: Mapping[str, Any], *, recovered: bool,
    ) -> BeehiivDraftReceipt:
        status = str(provider.get("status") or "draft").lower()
        if status != "draft":
            self._report(issue_id, "provider", None, f"Reconciliation found forbidden state {status!r}", retryable=False)
            raise BeehiivDeliveryError("provider", "Reconciled Beehiiv artifact is not a draft")
        external_id = str(provider.get("id") or provider.get("post_id") or "").strip()
        if not external_id:
            raise BeehiivDeliveryError("provider", "Reconciled Beehiiv draft has no ID", retryable=True)
        preview = provider.get("preview_url") or provider.get("editor_url") or provider.get("web_url")
        self.editorial.record_export_receipt(
            issue_id,
            expected_revision=int(prepared["payload"]["revision"]),
            idempotency_key=str(prepared["idempotency_key"]),
            external_id=external_id,
            preview_url=str(preview) if preview else None,
            payload_fingerprint=str(prepared["payload_fingerprint"]),
            connector="beehiiv",
        )
        return BeehiivDraftReceipt(
            issue_id=issue_id,
            revision=int(prepared["payload"]["revision"]),
            external_id=external_id,
            preview_url=str(preview) if preview else None,
            idempotency_key=str(prepared["idempotency_key"]),
            payload_fingerprint=str(prepared["payload_fingerprint"]),
            recovered=recovered,
        )

    def _lookup_safely(self, key: str, issue_id: str, *, report_failure: bool) -> Mapping[str, Any] | None:
        try:
            return self.lookup(key)
        except Exception:
            if report_failure:
                self._report(issue_id, "provider", None, "Draft reconciliation lookup failed", retryable=True)
            return None

    def _retry_after(self, response: HttpResponse) -> float:
        raw = response.headers.get("Retry-After") or response.headers.get("retry-after") or "0"
        try:
            seconds = max(0.0, float(raw))
        except (TypeError, ValueError):
            seconds = 0.0
        return min(seconds, self.max_retry_after_seconds)

    @staticmethod
    def _failure_category(response: HttpResponse) -> str:
        body = ""
        try:
            body = response.body.decode("utf-8", errors="ignore").lower()
        except Exception:
            pass
        if response.status_code == 402 or any(word in body for word in ("plan", "upgrade", "entitlement")):
            return "plan"
        if response.status_code in {401, 403}:
            return "permission"
        return "provider"

    def _report(
        self, issue_id: str, category: str, status_code: int | None,
        actual: str, *, retryable: bool,
    ) -> None:
        summary = {
            "plan": "Beehiiv plan does not allow newsletter draft export",
            "permission": "Beehiiv draft export permission is missing",
            "provider": "Beehiiv draft export failed",
        }[category]
        workaround = {
            "plan": "Upgrade the publication plan or use an approved browser-based draft adapter.",
            "permission": "Reconnect Beehiiv with the minimum post-creation permission.",
            "provider": "Reconcile by the stable idempotency key before retrying.",
        }[category]
        self.feedback(
            reporter="beehiiv-delivery",
            summary=summary,
            details="An approved BrandMan newsletter revision could not be safely recorded as a Beehiiv draft.",
            component="beehiiv.newsletter.export",
            severity="high" if category != "provider" or not retryable else "medium",
            fingerprint=f"beehiiv-export:{category}:{status_code or 'transport'}:{issue_id}",
            reproduction=f"Export approved newsletter issue {issue_id} as a Beehiiv draft.",
            expected_behavior="Create exactly one Beehiiv draft and persist its external ID and preview URL.",
            actual_behavior=f"{actual}; status={status_code or 'unavailable'}.",
            workaround=workaround,
            related_ids=[issue_id, self.publication_id],
        )
