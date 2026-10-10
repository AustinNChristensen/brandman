"""Connector boundaries and normalized external events for BrandMan.

This module intentionally contains no persistence and performs no network calls at
import time. Connectors that need authentication receive a transport and a callable
that supplies an authorization header, which keeps credentials out of models,
results, exceptions, and object representations.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from html import unescape
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree


class ConnectorKind(StrEnum):
    BEEHIIV = "beehiiv"
    X = "x"
    WEBSITE = "website"
    RSS = "rss"


class EventKind(StrEnum):
    SOURCE_ITEM = "source_item"
    POST_PUBLISHED = "post_published"
    METRIC_OBSERVED = "metric_observed"
    SUBSCRIBER_CHANGED = "subscriber_changed"
    DEPLOYMENT_CHANGED = "deployment_changed"


@dataclass(frozen=True, slots=True)
class SyncCursor:
    """Opaque connector-owned position for incremental reads."""

    value: str


@dataclass(frozen=True, slots=True)
class ConnectorEvent:
    connector: ConnectorKind
    kind: EventKind
    dedup_key: str
    occurred_at: str | None
    external_id: str | None
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ConnectorResult:
    events: tuple[ConnectorEvent, ...] = ()
    next_cursor: SyncCursor | None = None
    has_more: bool = False


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    ok: bool
    checked_at: str
    message: str = ""


class ReadConnector(Protocol):
    kind: ConnectorKind

    def sync(self, cursor: SyncCursor | None = None) -> ConnectorResult: ...


class Connector(Protocol):
    kind: ConnectorKind

    def health(self) -> ConnectorHealth: ...


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class HttpTransport(Protocol):
    """Small injectable HTTP seam. Implementations must redact auth in logs."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
    ) -> HttpResponse: ...


class ConnectorError(RuntimeError):
    """Safe connector failure that never embeds request headers or credentials."""

    def __init__(self, connector: ConnectorKind, operation: str, status_code: int | None = None):
        self.connector = connector
        self.operation = operation
        self.status_code = status_code
        suffix = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"{connector.value} {operation} failed{suffix}")


_TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref"}
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def canonical_url(url: str | None) -> str | None:
    """Canonicalize a URL for identity, removing fragments and tracking values."""

    if not url:
        return None
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMETERS
    ]
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), ""))


def plain_text(value: str | None) -> str:
    return _SPACE_RE.sub(" ", unescape(_TAG_RE.sub(" ", value or ""))).strip()


def content_fingerprint(*parts: str | None) -> str:
    """Return a stable fingerprint after normalizing markup and whitespace."""

    content = "\n".join(plain_text(part).casefold() for part in parts if part)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def dedup_identity(
    connector: ConnectorKind,
    *,
    external_id: str | None = None,
    url: str | None = None,
    fingerprint: str | None = None,
) -> str:
    """Build a deterministic identity, preferring provider IDs over URLs/content."""

    identity = external_id or canonical_url(url) or fingerprint
    if not identity:
        raise ValueError("dedup identity requires an external ID, URL, or fingerprint")
    digest = hashlib.sha256(f"{connector.value}:{identity}".encode("utf-8")).hexdigest()
    return f"{connector.value}:{digest}"


def normalize_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0 or str(value).strip() not in {str(parsed), f"{parsed}.0"}:
        return None
    return parsed


class UrllibTransport:
    """Pinned-IP HTTPS transport with per-hop SSRF validation.

    The validated address is the address used for the socket, preventing a DNS
    rebind between policy evaluation and connection. Redirects are handled
    explicitly and independently re-resolved. Socket/TLS timeouts bound normal
    I/O, though Python cannot forcibly cancel an already-running OS resolver.
    """

    def __init__(
        self, *, timeout_seconds: float = 15, max_bytes: int = 5_000_000,
        max_redirects: int = 3,
        resolver: Callable[[str, int], Sequence[str]] | None = None,
        connection_factory: Callable[[str, str, int, float], Any] | None = None,
    ):
        if not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 0 and 60")
        if not 0 <= max_redirects <= 10:
            raise ValueError("max_redirects must be between 0 and 10")
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self._resolver = resolver or _resolve_addresses
        self._connection_factory = connection_factory or _PinnedHTTPSConnection

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params)}"
        if json_body is not None and form_body is not None:
            raise ValueError("request cannot contain both JSON and form bodies")
        body = (
            json.dumps(json_body).encode("utf-8") if json_body is not None
            else urlencode(form_body).encode("utf-8") if form_body is not None
            else None
        )
        safe_headers = {"User-Agent": "BrandOS/0.1", **(headers or {})}
        if body is not None:
            safe_headers.setdefault(
                "Content-Type",
                "application/x-www-form-urlencoded" if form_body is not None else "application/json",
            )
        current_url = url
        original_origin: tuple[str, int] | None = None
        for redirect_count in range(self.max_redirects + 1):
            parts, addresses = self._validated_target(current_url)
            port = parts.port or 443
            origin = (parts.hostname or "", port)
            if original_origin is None:
                original_origin = origin
            request_headers = dict(safe_headers)
            if origin != original_origin:
                request_headers.pop("Authorization", None)
                request_headers.pop("authorization", None)
            path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
            connection = self._connection_factory(
                parts.hostname or "", addresses[0], port, self.timeout_seconds,
            )
            try:
                connection.request(method, path, body=body, headers=request_headers)
                response = connection.getresponse()
                payload = response.read(self.max_bytes + 1)
                response_headers = dict(response.getheaders())
                status = int(response.status)
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                raise ConnectorError(ConnectorKind.RSS, "bounded HTTPS request") from exc
            finally:
                connection.close()
            if len(payload) > self.max_bytes:
                raise ConnectorError(ConnectorKind.RSS, "fetch: response too large")
            if status not in {301, 302, 303, 307, 308}:
                return HttpResponse(status, payload, response_headers)
            location = next(
                (value for key, value in response_headers.items() if key.lower() == "location"),
                None,
            )
            if not location or redirect_count >= self.max_redirects:
                raise ConnectorError(ConnectorKind.RSS, "redirect policy")
            current_url = urljoin(current_url, location)
            if status == 303:
                method, body = "GET", None
        raise ConnectorError(ConnectorKind.RSS, "redirect policy")

    def _validated_target(self, url: str):
        try:
            parts = urlsplit(url)
            port = parts.port or 443
        except ValueError as exc:
            raise ConnectorError(ConnectorKind.RSS, "public URL validation") from exc
        if (
            parts.scheme.lower() != "https" or not parts.hostname
            or parts.username is not None or parts.password is not None
            or port != 443
        ):
            raise ConnectorError(ConnectorKind.RSS, "public HTTPS URL required")
        try:
            addresses = tuple(dict.fromkeys(self._resolver(parts.hostname, port)))
        except (OSError, ValueError) as exc:
            raise ConnectorError(ConnectorKind.RSS, "DNS resolution") from exc
        if not addresses:
            raise ConnectorError(ConnectorKind.RSS, "DNS resolution")
        try:
            parsed = [ipaddress.ip_address(address) for address in addresses]
        except ValueError as exc:
            raise ConnectorError(ConnectorKind.RSS, "DNS resolution") from exc
        if any(
            not address.is_global
            or address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
            for address in parsed
        ):
            raise ConnectorError(ConnectorKind.RSS, "non-public target blocked")
        return parts, tuple(str(address) for address in parsed)


def _resolve_addresses(hostname: str, port: int) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            result[4][0]
            for result in socket.getaddrinfo(
                hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
            )
        )
    )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS connection whose socket target is the already-validated IP."""

    def __init__(self, hostname: str, address: str, port: int, timeout: float):
        super().__init__(hostname, port=port, timeout=timeout)
        self._validated_address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._validated_address, self.port), self.timeout,
        )
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)


class RssConnector:
    """Fetch and normalize RSS 2.x and Atom feeds into source-item events."""

    kind = ConnectorKind.RSS

    def __init__(
        self, feed_url: str, transport: HttpTransport | None = None,
        canonical_revalidator: Callable[..., Mapping[str, Any]] | None = None,
    ):
        self.feed_url = feed_url
        self.transport = transport or UrllibTransport()
        self.canonical_revalidator = canonical_revalidator

    def health(self) -> ConnectorHealth:
        try:
            self.sync()
        except Exception:
            return ConnectorHealth(False, datetime.now(UTC).isoformat(), "feed unavailable")
        return ConnectorHealth(True, datetime.now(UTC).isoformat())

    def sync(self, cursor: SyncCursor | None = None) -> ConnectorResult:
        response = self.transport.request(
            "GET", self.feed_url, headers={"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"}
        )
        if not 200 <= response.status_code < 300:
            raise ConnectorError(self.kind, "sync", response.status_code)
        try:
            root = ElementTree.fromstring(response.body)
        except ElementTree.ParseError as exc:
            raise ConnectorError(self.kind, "parse") from exc

        entries = self._entries(root)
        normalized = tuple(self._normalize_entry(entry) for entry in entries)
        # Feed reads are snapshots. The newest item identity is an opaque high-water mark.
        next_cursor = SyncCursor(normalized[0].dedup_key) if normalized else cursor
        events = normalized
        if cursor:
            # Preserve all entries preceding the last-seen item, discarding older entries.
            seen_index = next((i for i, event in enumerate(normalized) if event.dedup_key == cursor.value), None)
            if seen_index is not None:
                events = normalized[:seen_index]
        return ConnectorResult(events=events, next_cursor=next_cursor)

    @staticmethod
    def _entries(root: ElementTree.Element) -> Sequence[ElementTree.Element]:
        name = _local_name(root.tag)
        if name == "feed":
            return [child for child in root if _local_name(child.tag) == "entry"]
        channel = next((child for child in root if _local_name(child.tag) == "channel"), root)
        return [child for child in channel if _local_name(child.tag) == "item"]

    def _normalize_entry(self, entry: ElementTree.Element) -> ConnectorEvent:
        title = _child_text(entry, "title")
        external_id = _child_text(entry, "guid", "id")
        published = _child_text(entry, "pubDate", "published", "updated", "date")
        summary = _child_text(entry, "description", "summary", "content", "encoded")
        url = _entry_link(entry)
        fingerprint = content_fingerprint(title, summary)
        key = dedup_identity(self.kind, external_id=external_id, url=url, fingerprint=fingerprint)
        payload = {
            "title": plain_text(title),
            "url": canonical_url(url),
            "summary": plain_text(summary),
            "published_at": normalize_timestamp(published),
            "content_fingerprint": fingerprint,
            "feed_url": canonical_url(self.feed_url),
        }
        if self.canonical_revalidator is not None and payload["url"]:
            try:
                payload["canonical_revalidation"] = dict(self.canonical_revalidator(
                    payload["url"], feed_title=payload["title"],
                    feed_summary=payload["summary"], feed_fingerprint=fingerprint,
                    # A feed item's publication instant is stable across a durable
                    # job replay, so its evidence request remains idempotent.
                    observed_at=normalize_timestamp(published),
                ))
            except (ConnectorError, TimeoutError, OSError, ssl.SSLError):
                # One unavailable canonical page must not discard the rest of an
                # otherwise valid feed batch. Keep the evidence sanitized.
                from .canonical_revalidation import unavailable_snapshot
                payload["canonical_revalidation"] = unavailable_snapshot(
                    payload["url"], observed_at=normalize_timestamp(published),
                    feed_fingerprint=fingerprint,
                )
        return ConnectorEvent(
            connector=self.kind,
            kind=EventKind.SOURCE_ITEM,
            dedup_key=key,
            occurred_at=normalize_timestamp(published),
            external_id=external_id,
            payload=payload,
        )


class BeehiivConnector:
    """Incremental Beehiiv content and measurement reader.

    Post statistics are normalized when Beehiiv includes them in the list
    response.  Publication subscriber statistics use a separate, explicitly
    enabled read scope so installations that only grant ``posts.read`` do not
    receive a surprise permission failure.
    """

    kind = ConnectorKind.BEEHIIV

    def __init__(
        self,
        publication_id: str,
        transport: HttpTransport,
        authorization_header: Callable[[], str],
        *,
        base_url: str = "https://api.beehiiv.com/v2",
        include_publication_stats: bool = False,
        clock: Callable[[], datetime] | None = None,
    ):
        self.publication_id = publication_id
        self.transport = transport
        self._authorization_header = authorization_header
        self.base_url = base_url.rstrip("/")
        self.include_publication_stats = include_publication_stats
        self.clock = clock or (lambda: datetime.now(UTC))

    def sync(self, cursor: SyncCursor | None = None) -> ConnectorResult:
        params = {"limit": "100", "expand[]": "free_web_content"}
        if cursor:
            params["page"] = cursor.value
        response = self.transport.request(
            "GET",
            f"{self.base_url}/publications/{self.publication_id}/posts",
            headers={"Authorization": self._authorization_header(), "Accept": "application/json"},
            params=params,
        )
        if not 200 <= response.status_code < 300:
            raise ConnectorError(self.kind, "post sync", response.status_code)
        document = response.json()
        posts = document.get("data", [])
        events: list[ConnectorEvent] = []
        observed_at = self._now()
        for post in posts:
            events.append(self._normalize_post(post, observed_at=observed_at))
            metric = self._normalize_post_metrics(post, observed_at=observed_at)
            if metric is not None:
                events.append(metric)
        pagination = document.get("pagination") or {}
        cursor_page = cursor.value if cursor else "1"
        current = int(pagination.get("page") or pagination.get("current_page") or cursor_page)
        total = int(pagination.get("total_pages") or current)
        has_more = current < total
        if self.include_publication_stats and cursor is None:
            events.append(self._read_publication_stats(observed_at=observed_at))
        return ConnectorResult(
            events=tuple(events),
            next_cursor=SyncCursor(str(current + 1)) if has_more else None,
            has_more=has_more,
        )

    def _read_publication_stats(self, *, observed_at: str) -> ConnectorEvent:
        response = self.transport.request(
            "GET", f"{self.base_url}/publications/{self.publication_id}",
            headers={"Authorization": self._authorization_header(), "Accept": "application/json"},
            params={"expand[]": "stats"},
        )
        if not 200 <= response.status_code < 300:
            raise ConnectorError(self.kind, "publication stats sync", response.status_code)
        data = response.json().get("data") or {}
        stats = data.get("stats") or {}
        event = normalize_beehiiv_publication_stats(
            self.publication_id, stats, observed_at=observed_at,
        )
        if event is None:
            raise ConnectorError(self.kind, "publication stats response")
        return event

    def _normalize_post_metrics(
        self, post: Mapping[str, Any], *, observed_at: str,
    ) -> ConnectorEvent | None:
        try:
            return normalize_beehiiv_post_metrics(post, observed_at=observed_at)
        except ValueError as error:
            raise ConnectorError(self.kind, "post stats parse") from error

    def _now(self) -> str:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("Beehiiv connector clock must be timezone aware")
        return value.astimezone(UTC).isoformat()

    def _normalize_post(self, post: Mapping[str, Any], *, observed_at: str) -> ConnectorEvent:
        external_id = str(post["id"])
        published = post.get("publish_date") or post.get("displayed_date") or post.get("created")
        provider_published = post.get("publish_date") or post.get("displayed_date")
        body = post.get("free_web_content") or post.get("content") or post.get("subtitle") or ""
        url = post.get("web_url") or post.get("url")
        fingerprint = content_fingerprint(str(post.get("title") or ""), str(body))
        return ConnectorEvent(
            connector=self.kind,
            kind=EventKind.SOURCE_ITEM,
            dedup_key=dedup_identity(self.kind, external_id=external_id),
            occurred_at=normalize_timestamp(str(published)) if published is not None else None,
            external_id=external_id,
            payload={
                "title": plain_text(str(post.get("title") or "")),
                "subtitle": plain_text(str(post.get("subtitle") or "")),
                "url": canonical_url(str(url)) if url else None,
                "summary": plain_text(str(body)),
                "status": post.get("status"),
                "scheduled_for": normalize_timestamp(post.get("scheduled_at")),
                # A creation timestamp is not publication evidence. Bound issue
                # reconciliation therefore receives only the provider's actual
                # publish/display timestamp and fails closed when it is absent.
                "published_at": normalize_timestamp(str(provider_published)) if provider_published is not None else None,
                "provider_observed_at": observed_at,
                "content_fingerprint": fingerprint,
            },
        )


def normalize_beehiiv_publication_stats(
    publication_id: str, stats: Mapping[str, Any], *, observed_at: str,
) -> ConnectorEvent | None:
    value = _nonnegative_integer(stats.get("active_subscriptions"))
    if value is None:
        return None
    snapshot = {
        "active_subscriptions": value,
        "active_free_subscriptions": _nonnegative_integer(
            stats.get("active_free_subscriptions")
        ),
        "active_premium_subscriptions": _nonnegative_integer(
            stats.get("active_premium_subscriptions")
        ),
    }
    fingerprint = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ConnectorEvent(
        connector=ConnectorKind.BEEHIIV, kind=EventKind.SUBSCRIBER_CHANGED,
        dedup_key=f"beehiiv:publication-stats:{publication_id}:{fingerprint}",
        occurred_at=normalize_timestamp(observed_at), external_id=publication_id,
        payload={
            "metric": "active_beehiiv_subscribers", "value": value,
            "evidence_type": "beehiiv_publication_stats",
            "active_free_subscriptions": snapshot["active_free_subscriptions"],
            "active_premium_subscriptions": snapshot["active_premium_subscriptions"],
        },
    )


def normalize_beehiiv_post_metrics(
    post: Mapping[str, Any], *, observed_at: str,
) -> ConnectorEvent | None:
    stats = post.get("stats")
    if not isinstance(stats, Mapping):
        return None
    external_id = str(post.get("id") or "")
    if not re.fullmatch(r"post_[A-Za-z0-9-]{1,200}", external_id):
        raise ValueError("Beehiiv post metrics require a canonical post_ provider ID")
    email = stats.get("email")
    web = stats.get("web")
    email = email if isinstance(email, Mapping) else {}
    web = web if isinstance(web, Mapping) else {}
    recognized = {
        **{f"email.{key}": value for key, value in email.items() if key in {
            "delivered", "unique_opens", "opens", "unique_clicks", "clicks",
            "unsubscribes",
        }},
        **{f"web.{key}": value for key, value in web.items() if key in {"views", "clicks"}},
        **({"upgrades": stats.get("upgrades")} if "upgrades" in stats else {}),
    }
    invalid = [key for key, value in recognized.items() if _nonnegative_integer(value) is None]
    if invalid:
        raise ValueError(
            "Beehiiv post statistics must be nonnegative integers: " + ", ".join(sorted(invalid))
        )

    def count(values: Mapping[str, Any], preferred: str, fallback: str | None = None) -> int | None:
        if preferred in values:
            return _nonnegative_integer(values[preferred])
        return _nonnegative_integer(values.get(fallback)) if fallback and fallback in values else None

    native = {
        "delivered": _nonnegative_integer(email.get("delivered")),
        "opens": count(email, "unique_opens"),
        "clicks": count(email, "unique_clicks"),
        "unsubscribes": _nonnegative_integer(email.get("unsubscribes")),
        "web_views": _nonnegative_integer(web.get("views")),
        "web_clicks": _nonnegative_integer(web.get("clicks")),
        "upgrades": _nonnegative_integer(stats.get("upgrades")),
    }
    native = {key: value for key, value in native.items() if value is not None}
    if not native:
        return None
    delivered = native.get("delivered")
    if delivered is not None and any(
        native.get(key, 0) > delivered for key in ("opens", "clicks", "unsubscribes")
    ):
        raise ValueError("Beehiiv unique opens, clicks, and unsubscribes cannot exceed delivered")
    if native.get("web_views") is not None and native.get("web_clicks", 0) > native["web_views"]:
        raise ValueError("Beehiiv web clicks cannot exceed web views")
    fingerprint = hashlib.sha256(
        json.dumps(native, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ConnectorEvent(
        connector=ConnectorKind.BEEHIIV, kind=EventKind.METRIC_OBSERVED,
        dedup_key=f"beehiiv:post-stats:{external_id}:{fingerprint}",
        occurred_at=normalize_timestamp(observed_at), external_id=external_id,
        payload={
            "evidence_type": "beehiiv_post_stats", "provider_post_id": external_id,
            "native_metrics": native,
            "impressions": native.get("delivered", 0),
            "clicks": native.get("clicks", 0) + native.get("web_clicks", 0),
            "engagements": native.get("opens", 0),
            "conversions": native.get("upgrades", 0),
        },
    )

@dataclass(frozen=True, slots=True)
class XPostRequest:
    text: str
    idempotency_key: str
    approved: bool = False
    reply_to_post_id: str | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("X post text cannot be empty")
        if not self.idempotency_key.strip():
            raise ValueError("idempotency key cannot be empty")


@dataclass(frozen=True, slots=True)
class DispatchReceipt:
    connector: ConnectorKind
    external_id: str
    external_url: str | None
    dedup_key: str
    text: str


class XConnector:
    """Approved-only X publishing adapter with an injected authenticated transport."""

    kind = ConnectorKind.X

    def __init__(
        self,
        transport: HttpTransport,
        authorization_header: Callable[[], str],
        *,
        base_url: str = "https://api.x.com/2",
    ):
        self.transport = transport
        self._authorization_header = authorization_header
        self.base_url = base_url.rstrip("/")

    def publish(self, request: XPostRequest) -> DispatchReceipt:
        if not request.approved:
            raise PermissionError("explicit approval is required before publishing to X")
        body: dict[str, Any] = {"text": request.text}
        if request.reply_to_post_id:
            body["reply"] = {"in_reply_to_tweet_id": request.reply_to_post_id}
        response = self.transport.request(
            "POST",
            f"{self.base_url}/tweets",
            headers={
                "Authorization": self._authorization_header(),
                "Content-Type": "application/json",
                "Idempotency-Key": request.idempotency_key,
            },
            json_body=body,
        )
        if not 200 <= response.status_code < 300:
            raise ConnectorError(self.kind, "post dispatch", response.status_code)
        data = response.json().get("data") or {}
        external_id = str(data["id"])
        text = str(data.get("text") or request.text)
        return DispatchReceipt(
            connector=self.kind,
            external_id=external_id,
            external_url=f"https://x.com/i/web/status/{external_id}",
            dedup_key=dedup_identity(self.kind, external_id=external_id),
            text=text,
        )


@dataclass(frozen=True, slots=True)
class WebsiteMetric:
    """Normalized analytics observation accepted from a website adapter."""

    observed_at: str
    metric: str
    value: float
    campaign_id: str | None = None
    post_id: str | None = None
    source: str | None = None
    evidence_type: str | None = None
    tracked_link_id: str | None = None
    attribution_confidence: str | None = None
    event_type: str = "metric"
    brand_id: str | None = None
    session_ref: str | None = None
    authenticated: bool | None = None
    tool_key: str | None = None
    asset_id: str | None = None
    cta_id: str | None = None
    deployment_id: str | None = None
    deployment_revision: str | None = None
    environment: str | None = None

    def as_event(self, external_id: str) -> ConnectorEvent:
        kind = (
            EventKind.DEPLOYMENT_CHANGED
            if self.event_type == "deployment_changed"
            else EventKind.METRIC_OBSERVED
        )
        return ConnectorEvent(
            connector=ConnectorKind.WEBSITE,
            kind=kind,
            dedup_key=dedup_identity(ConnectorKind.WEBSITE, external_id=external_id),
            occurred_at=normalize_timestamp(self.observed_at),
            external_id=external_id,
            payload={
                "metric": self.metric,
                "value": self.value,
                "campaign_id": self.campaign_id,
                "post_id": self.post_id,
                "source": self.source,
                "evidence_type": self.evidence_type,
                "tracked_link_id": self.tracked_link_id,
                "attribution_confidence": self.attribution_confidence,
                "event_type": self.event_type,
                "brand_id": self.brand_id,
                "session_ref": self.session_ref,
                "authenticated": self.authenticated,
                "tool_key": self.tool_key,
                "asset_id": self.asset_id or self.post_id,
                "cta_id": self.cta_id,
                "deployment_id": self.deployment_id,
                "deployment_revision": self.deployment_revision,
                "environment": self.environment,
            },
        )


class WebsiteAnalyticsConnector:
    """Read normalized analytics snapshots from an authenticated HTTPS endpoint.

    The endpoint contract is intentionally small: ``data`` is a list of metric
    objects accepted by :class:`WebsiteMetric`; ``next_cursor`` and ``has_more``
    provide bounded pagination. This adapter never sends mutation requests.
    """

    kind = ConnectorKind.WEBSITE
    required_scopes = frozenset({"analytics.read"})

    def __init__(
        self,
        endpoint_url: str,
        transport: HttpTransport,
        authorization_header: Callable[[], str],
        *,
        granted_scopes: Sequence[str],
    ) -> None:
        endpoint = canonical_url(endpoint_url)
        if endpoint is None or not endpoint.startswith("https://"):
            raise ValueError("website analytics endpoint_url must use HTTPS")
        scopes = {scope.strip() for scope in granted_scopes if scope.strip()}
        missing = sorted(self.required_scopes - scopes)
        excessive = sorted(scopes - self.required_scopes)
        if missing or excessive:
            parts = []
            if missing:
                parts.append("missing read scopes: " + ", ".join(missing))
            if excessive:
                parts.append("unnecessary scopes: " + ", ".join(excessive))
            raise PermissionError("; ".join(parts))
        self.endpoint_url = endpoint
        self.transport = transport
        self._authorization_header = authorization_header

    def sync(self, cursor: SyncCursor | None = None) -> ConnectorResult:
        params = {"cursor": cursor.value} if cursor else None
        response = self.transport.request(
            "GET",
            self.endpoint_url,
            headers={
                "Authorization": self._authorization_header(),
                "Accept": "application/json",
            },
            params=params,
        )
        if not 200 <= response.status_code < 300:
            raise ConnectorError(self.kind, "analytics sync", response.status_code)
        document = response.json()
        records = document.get("data") if isinstance(document, Mapping) else None
        if not isinstance(records, list):
            raise ConnectorError(self.kind, "analytics parse")
        events: list[ConnectorEvent] = []
        for record in records:
            if not isinstance(record, Mapping):
                raise ConnectorError(self.kind, "analytics parse")
            try:
                external_id = str(record["id"]).strip()
                event_type = str(record.get("event_type") or "metric").strip()
                raw_value = record.get("value", 0 if event_type == "deployment_changed" else 1)
                raw_session = record.get("session_id")
                post_id = _optional_text(record.get("post_id"))
                asset_id = _optional_text(record.get("asset_id"))
                if post_id and asset_id and post_id != asset_id:
                    raise ValueError("post_id and asset_id disagree")
                metric = WebsiteMetric(
                    observed_at=str(record["observed_at"]),
                    metric=str(record.get("metric") or _website_default_metric(event_type)),
                    value=float(raw_value),
                    campaign_id=_optional_text(record.get("campaign_id")),
                    post_id=post_id or asset_id,
                    source=_optional_text(record.get("source")),
                    evidence_type=_optional_text(record.get("evidence_type")),
                    tracked_link_id=_optional_text(record.get("tracked_link_id")),
                    attribution_confidence=_optional_text(
                        record.get("attribution_confidence")
                    ),
                    event_type=event_type,
                    brand_id=_optional_text(record.get("brand_id")),
                    session_ref=_website_session_ref(raw_session) if raw_session is not None else None,
                    authenticated=record.get("authenticated"),
                    tool_key=_optional_text(record.get("tool_key")),
                    asset_id=post_id or asset_id,
                    cta_id=_optional_text(record.get("cta_id")),
                    deployment_id=_optional_text(record.get("deployment_id")),
                    deployment_revision=_optional_text(record.get("deployment_revision")),
                    environment=_optional_text(record.get("environment")),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ConnectorError(self.kind, "analytics parse") from exc
            if not external_id:
                raise ConnectorError(self.kind, "analytics parse")
            if any(key in record for key in (
                "email", "user_email", "user_id", "authorization", "cookie", "token",
            )):
                raise ConnectorError(self.kind, "analytics parse")
            _validate_website_record(metric, raw_value)
            events.append(metric.as_event(external_id))
        has_more = document.get("has_more") is True
        next_value = document.get("next_cursor")
        if has_more and (not isinstance(next_value, str) or not next_value.strip()):
            raise ConnectorError(self.kind, "analytics pagination parse")
        return ConnectorResult(
            tuple(events), SyncCursor(next_value) if has_more else None, has_more,
        )


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _website_default_metric(event_type: str) -> str:
    return {
        "authenticated_session": "authenticated_session",
        "tool_use": "tool_use",
        "conversion": "conversion",
        "deployment_changed": "deployment_changed",
    }.get(event_type, "")


def _website_session_ref(value: Any) -> str:
    raw = str(value).strip()
    if not raw or len(raw) > 500:
        raise ValueError("session_id must be a non-empty opaque value")
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _validate_website_record(metric: WebsiteMetric, raw_value: Any) -> None:
    event_type = metric.event_type
    supported = {"metric", "authenticated_session", "tool_use", "conversion", "deployment_changed"}
    if event_type not in supported:
        raise ConnectorError(ConnectorKind.WEBSITE, "analytics parse")
    try:
        observed = datetime.fromisoformat(metric.observed_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ConnectorError(ConnectorKind.WEBSITE, "analytics parse") from None
    if observed.tzinfo is None:
        raise ConnectorError(ConnectorKind.WEBSITE, "analytics parse")
    if event_type == "deployment_changed":
        if not all((metric.deployment_id, metric.deployment_revision, metric.environment)):
            raise ConnectorError(ConnectorKind.WEBSITE, "analytics parse")
        if any((
            metric.brand_id, metric.campaign_id, metric.post_id, metric.tracked_link_id,
            metric.cta_id, metric.source, metric.session_ref, metric.tool_key,
        )):
            raise ConnectorError(ConnectorKind.WEBSITE, "analytics parse")
        return
    if isinstance(raw_value, bool) or metric.value < 0:
        raise ConnectorError(ConnectorKind.WEBSITE, "analytics parse")
    governed_count = event_type in {"authenticated_session", "tool_use", "conversion"} or metric.evidence_type == "website_conversion"
    if governed_count and not metric.value.is_integer():
        raise ConnectorError(ConnectorKind.WEBSITE, "analytics parse")
    if event_type == "authenticated_session":
        if metric.authenticated is not True or not metric.session_ref or metric.value != 1:
            raise ConnectorError(ConnectorKind.WEBSITE, "analytics parse")
        if any((
            metric.brand_id, metric.campaign_id, metric.post_id, metric.tracked_link_id,
            metric.cta_id, metric.source,
        )):
            raise ConnectorError(ConnectorKind.WEBSITE, "analytics parse")
        return
    governed_attribution = event_type in {"tool_use", "conversion"} or metric.evidence_type == "website_conversion"
    if not governed_attribution:
        return
    if event_type == "tool_use" and (not metric.tool_key or not metric.session_ref or metric.authenticated is not True):
        raise ConnectorError(ConnectorKind.WEBSITE, "analytics parse")
    if event_type == "conversion" and metric.evidence_type not in {None, "website_conversion"}:
        raise ConnectorError(ConnectorKind.WEBSITE, "analytics parse")
    if metric.metric not in {"tool_use", "conversion", "newsletter_conversion", "revenue"}:
        raise ConnectorError(ConnectorKind.WEBSITE, "analytics parse")
    identifiers = (
        (metric.campaign_id, metric.post_id, metric.tracked_link_id)
        if event_type == "metric"
        else (
            metric.brand_id, metric.campaign_id, metric.post_id,
            metric.tracked_link_id, metric.cta_id, metric.source,
        )
    )
    if metric.attribution_confidence == "tracked_link_exact" and not all(identifiers):
        raise ConnectorError(ConnectorKind.WEBSITE, "analytics parse")
    if metric.attribution_confidence == "unattributed" and any(identifiers):
        raise ConnectorError(ConnectorKind.WEBSITE, "analytics parse")
    if metric.attribution_confidence not in {"tracked_link_exact", "unattributed"}:
        raise ConnectorError(ConnectorKind.WEBSITE, "analytics parse")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _child_text(element: ElementTree.Element, *names: str) -> str | None:
    wanted = set(names)
    for child in element:
        if _local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return None


def _entry_link(entry: ElementTree.Element) -> str | None:
    for child in entry:
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        rel = child.attrib.get("rel", "alternate")
        if href and rel == "alternate":
            return href
        if child.text and child.text.strip():
            return child.text.strip()
    return None
