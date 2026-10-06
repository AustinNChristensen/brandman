"""Read-only X synchronization for metrics and engagement opportunities."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

from app.connectors import (
    ConnectorError,
    ConnectorEvent,
    ConnectorKind,
    ConnectorResult,
    EventKind,
    HttpTransport,
    SyncCursor,
    dedup_identity,
    normalize_timestamp,
)


READ_SCOPES = frozenset({"tweet.read", "users.read", "offline.access"})
_ALLOWED_SCOPES = READ_SCOPES
_WRITE_SCOPE_RE = re.compile(r"(?:^|\.)write$")
_TWEET_FIELDS = ",".join((
    "id", "text", "author_id", "created_at", "conversation_id",
    "in_reply_to_user_id", "referenced_tweets", "public_metrics",
    "non_public_metrics", "organic_metrics", "promoted_metrics",
))


class XReadScopeError(PermissionError):
    pass


class XReadCursorError(ValueError):
    pass


class XRateLimitError(ConnectorError):
    """Credential-safe 429 response with optional numeric retry metadata."""

    def __init__(self, *, retry_after_seconds: int | None, reset_at: int | None):
        self.retry_after_seconds = retry_after_seconds
        self.reset_at = reset_at
        super().__init__(ConnectorKind.X, "read sync rate limited", 429)


@dataclass(frozen=True, slots=True)
class _Endpoint:
    key: str
    path: str
    event_type: str
    params: Mapping[str, str]


class XReadConnector:
    """Incrementally read one X account without exposing any write operation.

    One endpoint page is fetched per ``sync`` call. The opaque cursor identifies
    the next endpoint and provider pagination token, allowing the durable worker
    to bound each request and resume safely.
    """

    kind = ConnectorKind.X

    def __init__(
        self,
        user_id: str,
        username: str,
        transport: HttpTransport,
        authorization_header: Callable[[], str],
        *,
        granted_scopes: Sequence[str],
        searches: Sequence[str] = (),
        target_user_ids: Sequence[str] = (),
        include_profile_metrics: bool = True,
        include_tweet_metrics: bool = True,
        include_mentions: bool = True,
        include_replies: bool = True,
        base_url: str = "https://api.x.com/2",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not user_id.strip() or not username.strip():
            raise ValueError("X user_id and username are required")
        scopes = frozenset(scope.strip() for scope in granted_scopes if scope.strip())
        missing = sorted(READ_SCOPES - scopes)
        write_scopes = sorted(scope for scope in scopes if _WRITE_SCOPE_RE.search(scope))
        excessive = sorted(scopes - _ALLOWED_SCOPES - set(write_scopes))
        if missing or write_scopes or excessive:
            parts = []
            if missing:
                parts.append(f"missing read scopes: {', '.join(missing)}")
            if write_scopes:
                parts.append("write scopes are not allowed on the read connector")
            if excessive:
                parts.append(f"unnecessary read scopes: {', '.join(excessive)}")
            raise XReadScopeError("; ".join(parts))
        self.user_id = user_id.strip()
        self.username = username.strip().lstrip("@")
        self.transport = transport
        self._authorization_header = authorization_header
        self.base_url = base_url.rstrip("/")
        self.clock = clock
        self._endpoints = self._build_endpoints(
            searches=searches,
            target_user_ids=target_user_ids,
            include_profile_metrics=include_profile_metrics,
            include_tweet_metrics=include_tweet_metrics,
            include_mentions=include_mentions,
            include_replies=include_replies,
        )
        if not self._endpoints:
            raise ValueError("at least one X read stream must be configured")

    def sync(self, cursor: SyncCursor | None = None) -> ConnectorResult:
        endpoint_index, pagination_token = self._decode_cursor(cursor)
        endpoint = self._endpoints[endpoint_index]
        params = dict(endpoint.params)
        if pagination_token:
            params["pagination_token"] = pagination_token
        response = self.transport.request(
            "GET",
            f"{self.base_url}{endpoint.path}",
            headers={
                "Authorization": self._authorization_header(),
                "Accept": "application/json",
            },
            params=params,
        )
        if response.status_code == 429:
            raise XRateLimitError(
                retry_after_seconds=_safe_int(response.headers.get("retry-after")),
                reset_at=_safe_int(response.headers.get("x-rate-limit-reset")),
            )
        if not 200 <= response.status_code < 300:
            raise ConnectorError(self.kind, f"{endpoint.event_type} read", response.status_code)
        document = response.json()
        events = self._normalize(endpoint, document)
        next_token = str((document.get("meta") or {}).get("next_token") or "") or None
        if next_token:
            next_cursor = self._cursor(endpoint_index, next_token)
            return ConnectorResult(tuple(events), next_cursor, True)
        next_index = endpoint_index + 1
        if next_index < len(self._endpoints):
            return ConnectorResult(tuple(events), self._cursor(next_index, None), True)
        return ConnectorResult(tuple(events), None, False)

    def _normalize(
        self, endpoint: _Endpoint, document: Mapping[str, Any]
    ) -> list[ConnectorEvent]:
        observed_at = self.clock().astimezone(UTC).isoformat()
        if endpoint.event_type == "profile_metrics":
            data = document.get("data") or {}
            return [self._profile_metric(data, observed_at)] if data else []
        data = document.get("data") or []
        if not isinstance(data, list):
            raise ConnectorError(self.kind, f"{endpoint.event_type} parse")
        includes = document.get("includes") or {}
        users = {
            str(user.get("id")): user
            for user in includes.get("users", [])
            if isinstance(user, Mapping) and user.get("id") is not None
        }
        included_tweets = {
            str(tweet.get("id")): tweet
            for tweet in includes.get("tweets", [])
            if isinstance(tweet, Mapping) and tweet.get("id") is not None
        }
        if endpoint.event_type == "tweet_metrics":
            return [
                self._tweet_metric(tweet, users, observed_at)
                for tweet in data if isinstance(tweet, Mapping)
            ]
        return [
            self._opportunity(endpoint, tweet, users, included_tweets)
            for tweet in data if isinstance(tweet, Mapping)
        ]

    def _profile_metric(
        self, user: Mapping[str, Any], observed_at: str
    ) -> ConnectorEvent:
        metrics = _numeric_mapping(user.get("public_metrics"))
        identity = _snapshot_identity("profile", str(user.get("id") or self.user_id), metrics)
        return ConnectorEvent(
            self.kind,
            EventKind.METRIC_OBSERVED,
            dedup_identity(self.kind, external_id=identity),
            observed_at,
            str(user.get("id") or self.user_id),
            {
                "evidence_type": "x_profile_metrics",
                "metric": "x_followers",
                "value": metrics.get("followers_count", 0),
                "followers": metrics.get("followers_count", 0),
                "following": metrics.get("following_count", 0),
                "tweet_count": metrics.get("tweet_count", 0),
                "listed_count": metrics.get("listed_count", 0),
                "username": user.get("username") or self.username,
            },
        )

    def _tweet_metric(
        self,
        tweet: Mapping[str, Any],
        users: Mapping[str, Mapping[str, Any]],
        observed_at: str,
    ) -> ConnectorEvent:
        tweet_id = str(tweet["id"])
        groups = {
            name: _numeric_mapping(tweet.get(name))
            for name in (
                "public_metrics", "non_public_metrics", "organic_metrics",
                "promoted_metrics",
            )
            if tweet.get(name) is not None
        }
        public = groups.get("public_metrics", {})
        private = groups.get("non_public_metrics", {})
        clicks = private.get("url_link_clicks", 0)
        engagements = sum(
            public.get(name, 0)
            for name in ("like_count", "reply_count", "retweet_count", "quote_count", "bookmark_count")
        )
        signature = _snapshot_identity("tweet", tweet_id, groups)
        author = users.get(str(tweet.get("author_id")), {})
        return ConnectorEvent(
            self.kind,
            EventKind.METRIC_OBSERVED,
            dedup_identity(self.kind, external_id=signature),
            observed_at,
            tweet_id,
            {
                "evidence_type": "x_tweet_metrics",
                "post_id": None,
                "provider_post_id": tweet_id,
                "post_created_at": normalize_timestamp(tweet.get("created_at")),
                "impressions": public.get("impression_count", 0),
                "clicks": clicks,
                "engagements": engagements,
                "public_metrics": public,
                "private_metrics": private,
                "organic_metrics": groups.get("organic_metrics", {}),
                "promoted_metrics": groups.get("promoted_metrics", {}),
                "author": _safe_user(author),
            },
        )

    def _opportunity(
        self,
        endpoint: _Endpoint,
        tweet: Mapping[str, Any],
        users: Mapping[str, Mapping[str, Any]],
        included_tweets: Mapping[str, Mapping[str, Any]],
    ) -> ConnectorEvent:
        tweet_id = str(tweet["id"])
        references = [
            {"type": ref.get("type"), "id": str(ref.get("id"))}
            for ref in tweet.get("referenced_tweets", [])
            if isinstance(ref, Mapping) and ref.get("id") is not None
        ]
        parent_ids = [ref["id"] for ref in references if ref["type"] in {"replied_to", "quoted"}]
        parent_context = [
            _safe_tweet(included_tweets[parent_id])
            for parent_id in parent_ids if parent_id in included_tweets
        ]
        author = users.get(str(tweet.get("author_id")), {})
        return ConnectorEvent(
            self.kind,
            EventKind.POST_PUBLISHED,
            dedup_identity(self.kind, external_id=f"{endpoint.event_type}:{tweet_id}"),
            normalize_timestamp(tweet.get("created_at")),
            tweet_id,
            {
                "evidence_type": "x_engagement_opportunity",
                "opportunity_type": endpoint.event_type,
                "text": str(tweet.get("text") or ""),
                "author": _safe_user(author),
                "conversation_id": tweet.get("conversation_id"),
                "in_reply_to_user_id": tweet.get("in_reply_to_user_id"),
                "referenced_tweets": references,
                "parent_context": parent_context,
                "public_metrics": _numeric_mapping(tweet.get("public_metrics")),
                "source_query": endpoint.params.get("query"),
                "target_user_id": (
                    endpoint.key.removeprefix("target:")
                    if endpoint.key.startswith("target:") else None
                ),
                "requires_approval": True,
                "external_url": f"https://x.com/i/web/status/{tweet_id}",
            },
        )

    def _build_endpoints(
        self,
        *,
        searches: Sequence[str],
        target_user_ids: Sequence[str],
        include_profile_metrics: bool,
        include_tweet_metrics: bool,
        include_mentions: bool,
        include_replies: bool,
    ) -> tuple[_Endpoint, ...]:
        common = {
            "max_results": "100",
            "tweet.fields": _TWEET_FIELDS,
            "expansions": "author_id,referenced_tweets.id",
            "user.fields": "id,name,username,verified,public_metrics",
        }
        endpoints: list[_Endpoint] = []
        if include_profile_metrics:
            endpoints.append(_Endpoint(
                "profile", f"/users/{quote(self.user_id)}", "profile_metrics",
                {"user.fields": "id,name,username,verified,public_metrics"},
            ))
        if include_tweet_metrics:
            endpoints.append(_Endpoint(
                "tweet-metrics", f"/users/{quote(self.user_id)}/tweets",
                "tweet_metrics", common,
            ))
        if include_mentions:
            endpoints.append(_Endpoint(
                "mentions", f"/users/{quote(self.user_id)}/mentions", "mention", common,
            ))
        if include_replies:
            endpoints.append(_Endpoint(
                "replies", "/tweets/search/recent", "reply",
                {**common, "query": f"to:{self.username} -from:{self.username}"},
            ))
        for query in dict.fromkeys(value.strip() for value in searches if value.strip()):
            digest = hashlib.sha256(query.encode()).hexdigest()[:16]
            endpoints.append(_Endpoint(
                f"search:{digest}", "/tweets/search/recent", "search",
                {**common, "query": query},
            ))
        for target in dict.fromkeys(value.strip() for value in target_user_ids if value.strip()):
            endpoints.append(_Endpoint(
                f"target:{target}", f"/users/{quote(target)}/tweets", "target_account",
                common,
            ))
        return tuple(endpoints)

    def _decode_cursor(self, cursor: SyncCursor | None) -> tuple[int, str | None]:
        if cursor is None:
            return 0, None
        try:
            document = json.loads(cursor.value)
            key = document["endpoint"]
            token = document.get("token")
            index = next(i for i, endpoint in enumerate(self._endpoints) if endpoint.key == key)
            if token is not None and not isinstance(token, str):
                raise ValueError
            return index, token
        except (json.JSONDecodeError, KeyError, StopIteration, TypeError, ValueError) as exc:
            raise XReadCursorError("X read cursor is invalid for this configuration") from exc

    def _cursor(self, endpoint_index: int, token: str | None) -> SyncCursor:
        return SyncCursor(json.dumps(
            {"endpoint": self._endpoints[endpoint_index].key, "token": token},
            sort_keys=True, separators=(",", ":"),
        ))


def required_x_read_scopes() -> tuple[str, ...]:
    return tuple(sorted(READ_SCOPES))


def _numeric_mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): int(number)
        for key, number in value.items()
        if isinstance(number, (int, float)) and not isinstance(number, bool)
    }


def _safe_user(user: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: user.get(key)
        for key in ("id", "name", "username", "verified", "public_metrics")
        if key in user
    }


def _safe_tweet(tweet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: tweet.get(key)
        for key in ("id", "text", "author_id", "created_at", "conversation_id")
        if key in tweet
    }


def _snapshot_identity(kind: str, external_id: str, metrics: Any) -> str:
    signature = hashlib.sha256(
        json.dumps(metrics, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"{kind}:{external_id}:{signature}"


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "READ_SCOPES",
    "XRateLimitError",
    "XReadConnector",
    "XReadCursorError",
    "XReadScopeError",
    "required_x_read_scopes",
]
