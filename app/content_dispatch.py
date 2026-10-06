"""Canonical content-to-dispatch composition services."""
from __future__ import annotations

from dataclasses import replace
import re
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app import store
from app.attribution_store import AttributionStore
from app.dispatch import DispatchItem, GovernedDispatcher, Lifecycle, validate_payload


class ContentRepository(Protocol):
    def row(self, query: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None: ...


class CanonicalPostNotFound(LookupError):
    pass


class CanonicalPostDispatchService:
    """Create or refresh the single governed X action for a canonical post.

    Calling this repeatedly with unchanged canonical content returns the same
    dispatch item. A changed body advances the dispatch revision and therefore
    invalidates any earlier approval. Published revisions remain immutable and
    cause a new linked dispatch revision to be created.
    """

    def __init__(
        self,
        dispatcher: GovernedDispatcher,
        repository: ContentRepository = store,
        attribution: AttributionStore | None = None,
    ) -> None:
        self.dispatcher = dispatcher
        self.repository = repository
        self.attribution = attribution

    def create_from_post(
        self, canonical_post_id: str, *, actor: str = "content-dispatch"
    ) -> DispatchItem:
        canonical = self.repository.row(
            """SELECT p.*, c.brand_id, c.id AS canonical_campaign_id, b.slug AS brand_slug
               FROM posts p
               JOIN campaigns c ON c.id = p.campaign_id
               JOIN brands b ON b.id = c.brand_id
               WHERE p.id = ?""",
            (canonical_post_id,),
        )
        if canonical is None:
            raise CanonicalPostNotFound(canonical_post_id)
        if canonical["channel"] != "x":
            raise ValueError("only canonical X posts can create X dispatch items")
        authored_body = canonical.get("body")
        if not isinstance(authored_body, str):
            raise ValueError("canonical post body must be text")
        reconciliation = self.reconcile_legacy_attribution_duplicate(
            canonical, authored_body, actor=actor, apply=True,
        )
        if reconciliation is not None:
            return self.dispatcher.store.get(reconciliation["retained_dispatch_id"])
        body = self._tracked_body(canonical, authored_body, actor=actor)

        linked = [
            item
            for item in self.dispatcher.store.list_items(brand_id=canonical["brand_id"])
            if item.connector == "x" and item.canonical_post_id == canonical_post_id
        ]
        latest = max(linked, key=lambda item: item.revision) if linked else None
        legacy_matches = [
            item for item in self.dispatcher.store.list_items(brand_id=canonical["brand_id"])
            if item.connector == "x" and item.canonical_post_id is None
            and item.payload in ({"body": authored_body}, {"body": body})
            and item.status not in {Lifecycle.PUBLISHED, Lifecycle.MEASURED, Lifecycle.CANCELLED}
        ]
        if latest is None and len(legacy_matches) == 1:
            latest = self.dispatcher.link_canonical_post(
                legacy_matches[0].id, canonical_post_id, actor=actor,
            )
            if latest.payload != {"body": body}:
                latest = self.dispatcher.edit(latest.id, {"body": body}, actor=actor)
        elif latest is not None:
            for duplicate in legacy_matches:
                self.dispatcher.cancel(duplicate.id, actor=f"{actor}:duplicate-reconciliation")
        if latest is not None and latest.payload == {"body": body}:
            return latest

        if latest is not None and latest.status not in {
            Lifecycle.PUBLISHED,
            Lifecycle.MEASURED,
            Lifecycle.CANCELLED,
        }:
            return self.dispatcher.edit(latest.id, {"body": body}, actor=actor)

        revision = 1 if latest is None else latest.revision + 1
        item_id = f"dispatch:x:{canonical_post_id}:revision:{revision}"
        try:
            return self.dispatcher.create(
                "x",
                {"body": body},
                item_id=item_id,
                brand_id=canonical["brand_id"],
                canonical_post_id=canonical_post_id,
                revision=revision,
                actor=actor,
            )
        except KeyError:
            # A concurrent caller may have won the unique connector/post/revision
            # insert. Return that canonical result rather than making a duplicate.
            existing = self.dispatcher.store.get(item_id)
            if (
                existing.canonical_post_id == canonical_post_id
                and existing.connector == "x"
                and existing.revision == revision
                and existing.payload == {"body": body}
            ):
                return existing
            raise

    def reconcile_legacy_attribution_duplicate(
        self, canonical: dict[str, Any], authored_body: str, *, actor: str,
        apply: bool,
    ) -> dict[str, Any] | None:
        """Merge one unambiguous payload-linked attributed legacy draft."""
        active = {
            Lifecycle.DRAFT, Lifecycle.AWAITING_APPROVAL,
            Lifecycle.APPROVED, Lifecycle.QUEUED,
        }
        items = [
            item for item in self.dispatcher.store.list_items(brand_id=canonical["brand_id"])
            if item.connector == "x" and item.status in active
        ]
        linked = [item for item in items if item.canonical_post_id == canonical["id"]]
        legacy = [
            item for item in items
            if item.canonical_post_id is None
            and item.payload.get("canonical_post_id") == canonical["id"]
            and item.payload.get("campaign_id") == canonical["canonical_campaign_id"]
            and _valid_attributed_legacy_body(item.payload, authored_body, canonical)
        ]
        if len(linked) != 1 or len(legacy) != 1:
            return None
        retained, duplicate = linked[0], legacy[0]
        desired_payload = {"body": str(duplicate.payload["body"])}
        if not validate_payload("x", desired_payload).valid:
            return None
        material_change = retained.payload != desired_payload
        plan = {
            "canonical_post_id": canonical["id"],
            "retained_dispatch_id": retained.id,
            "cancelled_dispatch_id": duplicate.id,
            "material_change": material_change,
            "approval_required": material_change,
            "applied": apply,
        }
        if not apply:
            return plan

        def mutation(current):
            keep = current[retained.id]
            discard = current[duplicate.id]
            if keep.status not in active or discard.status not in active:
                raise ValueError("dispatch duplicate changed during reconciliation")
            if material_change:
                keep = replace(
                    keep, payload=desired_payload, revision=keep.revision + 1,
                    status=Lifecycle.DRAFT, approval=None, idempotency_key=None,
                    dispatch_claim=None, last_error=None,
                    updated_at=self.dispatcher.clock(),
                )
            discard = replace(
                discard, status=Lifecycle.CANCELLED, dispatch_claim=None,
                updated_at=self.dispatcher.clock(),
            )
            return {retained.id: keep, duplicate.id: discard}

        updated = self.dispatcher.store.mutate_many([retained.id, duplicate.id], mutation)
        keep, discard = updated
        if material_change:
            self.dispatcher._audit(
                keep, "legacy attribution preserved; approval invalidated", actor,
            )
        self.dispatcher._audit(
            discard, "cancelled attributed legacy duplicate", actor, retained.id,
        )
        return plan

    def _tracked_body(self, canonical: dict[str, Any], body: str, *, actor: str) -> str:
        if self.attribution is None:
            return body
        matches = list(re.finditer(r"https?://[^\s<>]+", body))
        if not matches:
            return body
        replacements: list[tuple[int, int, str]] = []
        for index, match in enumerate(matches, start=1):
            trailing = match.group(0)[-1:] if match.group(0)[-1:] in ".,;:!?)]" else ""
            destination = match.group(0)[:-1] if trailing else match.group(0)
            link = self.attribution.create_tracked_link(
                brand_id=canonical["brand_id"], brand_slug=canonical.get("brand_slug"),
                campaign_id=canonical["canonical_campaign_id"], artifact_id=canonical["id"],
                cta_id=f"url-{index}", source="x", medium="organic-social",
                destination=destination, actor=actor,
            )
            replacements.append((match.start(), match.end(), link["tracked_url"] + trailing))
        for start, end, replacement in reversed(replacements):
            body = body[:start] + replacement + body[end:]
        return body


_TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content",
    "utm_cta", "utm_brand", "bos_cta",
}


def _without_tracking(url: str) -> str:
    parsed = urlsplit(url)
    query = [
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in _TRACKING_KEYS
    ]
    return urlunsplit((
        parsed.scheme.lower(), parsed.netloc.lower(), parsed.path,
        urlencode(query), "",
    ))


def _normalize_tracking_in_body(body: str) -> str:
    return re.sub(
        r"https?://[^\s<>]+", lambda match: _without_tracking(match.group(0)), body,
    )


def _valid_attributed_legacy_body(
    payload: Any, authored_body: str, canonical: dict[str, Any],
) -> bool:
    body = payload.get("body")
    tracked_url = payload.get("tracked_url")
    if not isinstance(body, str) or not isinstance(tracked_url, str) or tracked_url not in body:
        return False
    parsed = urlsplit(tracked_url)
    parameters = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return (
        parsed.scheme in {"http", "https"} and bool(parsed.netloc)
        and parameters.get("utm_campaign") == canonical["canonical_campaign_id"]
        and parameters.get("utm_content") == canonical["id"]
        and parameters.get("utm_source") == "x"
        and parameters.get("utm_medium") == "organic-social"
        and _normalize_tracking_in_body(body) == _normalize_tracking_in_body(authored_body)
    )


def create_dispatch_from_post(
    dispatcher: GovernedDispatcher,
    canonical_post_id: str,
    *,
    actor: str = "content-dispatch",
    repository: ContentRepository = store,
    attribution: AttributionStore | None = None,
) -> DispatchItem:
    """Functional integration API for REST/MCP application boundaries."""
    return CanonicalPostDispatchService(dispatcher, repository, attribution).create_from_post(
        canonical_post_id, actor=actor
    )


__all__ = [
    "CanonicalPostDispatchService",
    "CanonicalPostNotFound",
    "create_dispatch_from_post",
]
