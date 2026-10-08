"""Deterministic browser-assisted Beehiiv private-draft contract.

This module deliberately contains no generic click or keyboard operations.  A
browser integration implements the small driver protocol below, while BrandOS
owns reconciliation, exact field material, read-back verification, and receipt
fingerprints.  Scheduling and sending are not part of the protocol.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import ipaddress
from pathlib import Path
import re
import socket
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from brandman.beehiiv_delivery import render_safe_body


class BeehiivAssistedPublisherError(ValueError):
    """Fail-closed assisted-draft validation error."""


class BeehiivPrivateDraftDriver(Protocol):
    """Stable browser boundary; implementations hide provider UI primitives."""

    def reconcile_private_draft(
        self, *, existing_draft_id: str | None, idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def set_text_field(self, field: str, value: str) -> None: ...

    def replace_rich_text(self, html: str) -> None: ...

    def upload_thumbnail(self, path: str, expected_sha256: str) -> None: ...

    def set_web_thumbnail_enabled(self, enabled: bool) -> None: ...

    def read_back_private_draft(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class BeehiivAssistedDraftReceipt:
    issue_id: str
    revision: int
    external_id: str
    preview_url: str
    content_fingerprint: str
    asset_fingerprint: str
    idempotency_key: str
    status: str = "draft"

    def as_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "revision": self.revision,
            "external_id": self.external_id,
            "preview_url": self.preview_url,
            "content_fingerprint": self.content_fingerprint,
            "asset_fingerprint": self.asset_fingerprint,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
        }


def build_private_draft_manifest(
    prepared: Mapping[str, Any], *, asset_path: str | Path,
    existing_draft_id: str | None = None,
) -> dict[str, Any]:
    """Build one exact, retry-safe browser instruction from approved material."""

    payload = prepared.get("payload")
    if not isinstance(payload, Mapping):
        raise BeehiivAssistedPublisherError("approved export payload is required")
    if payload.get("connector") != "beehiiv":
        raise BeehiivAssistedPublisherError("assisted publisher accepts only Beehiiv material")
    issue_id = _required_text(payload, "issue_id")
    revision = payload.get("revision")
    if not isinstance(revision, int) or revision < 1:
        raise BeehiivAssistedPublisherError("an exact positive newsletter revision is required")
    content_fingerprint = _required_text(prepared, "payload_fingerprint")
    idempotency_key = _required_text(prepared, "idempotency_key")
    expected_key = f"newsletter-export:beehiiv:{issue_id}:r{revision}"
    if idempotency_key != expected_key:
        raise BeehiivAssistedPublisherError("idempotency key does not match the exact revision")
    if existing_draft_id is not None and not existing_draft_id.strip():
        raise BeehiivAssistedPublisherError("existing draft ID cannot be blank")
    if existing_draft_id is not None and _BEEHIIV_ID.fullmatch(existing_draft_id.strip()) is None:
        raise BeehiivAssistedPublisherError("existing draft ID is not a canonical Beehiiv post ID")

    asset = Path(asset_path).expanduser().resolve()
    if not asset.is_file():
        raise BeehiivAssistedPublisherError("approved thumbnail asset does not exist")
    if asset.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        raise BeehiivAssistedPublisherError("approved thumbnail must be a supported image file")
    if asset.stat().st_size > 20 * 1024 * 1024:
        raise BeehiivAssistedPublisherError("approved thumbnail exceeds the 20 MB safety limit")
    asset_fingerprint = "sha256:" + sha256(asset.read_bytes()).hexdigest()
    fields = {
        "title": _required_text(payload, "title"),
        "subject": _required_text(payload, "subject"),
        "preview_text": _required_text(payload, "preview_text"),
    }
    body_html = render_safe_body(list(payload.get("sections") or []), payload.get("cta"))
    if not body_html.strip():
        raise BeehiivAssistedPublisherError("approved rich-text body is empty")
    return {
        "schema_version": 1,
        "provider": "beehiiv",
        "action": "upsert_private_draft",
        "issue_id": issue_id,
        "revision": revision,
        "idempotency_key": idempotency_key,
        "content_fingerprint": content_fingerprint,
        "browser_contract": {
            "selector_strategy": "fresh_accessibility_role_and_label",
            "index_lifetime": "one_accessibility_snapshot",
            "refresh_immediately_before_every_write": True,
            "refresh_after_every_navigation_or_dom_change": True,
            "persist_element_indices": False,
        },
        "reconciliation": {
            "existing_draft_id": existing_draft_id,
            "on_retry": "update_same_draft",
            "duplicate_creation_forbidden": True,
        },
        "fields": fields,
        "body": {"format": "html", "content": body_html, "replace_template": True},
        "thumbnail": {
            "path": str(asset),
            "asset_fingerprint": asset_fingerprint,
            "display_on_web": True,
            "upload_adapter": {
                "kind": "verified_public_url_required",
                "selector_contract": {
                    "strategy": "fresh_accessibility_role_and_label",
                    "index_lifetime": "one_accessibility_snapshot",
                    "refresh_after_every_action": True,
                    "persist_element_indices": False,
                    "upload_control_labels": ["Upload", "Choose file", "Add image"],
                },
                "native_file_input_supported": False,
                "url_fallback_allowed": True,
                "verification_required": ["https", "allowlisted_final_host", "http_200",
                                          "image_content_type", "exact_asset_fingerprint"],
            },
        },
        "read_back": {
            "required": True,
            "fields": ["title", "subject", "preview_text", "body_html", "thumbnail_fingerprint",
                       "display_thumbnail_on_web", "status", "external_id", "preview_url"],
        },
        "forbidden_actions": ["schedule", "send", "publish"],
    }


def attach_verified_public_asset(
    manifest: Mapping[str, Any], public_url: str, *, allowed_hosts: set[str],
    timeout_seconds: float = 15,
) -> dict[str, Any]:
    """Verify public bytes, then bind Beehiiv's URL-import adapter."""

    expected = str(manifest.get("thumbnail", {}).get("asset_fingerprint") or "")
    verification = verify_public_asset_url(
        public_url, expected_fingerprint=expected, allowed_hosts=allowed_hosts,
        timeout_seconds=timeout_seconds,
    )
    result = _deep_copy(manifest)
    result["thumbnail"]["upload_adapter"] = {
        "kind": "beehiiv_media_url",
        "url": verification["final_url"],
        "verified": verification,
        "selector_contract": {
            "strategy": "fresh_accessibility_role_and_label",
            "index_lifetime": "one_accessibility_snapshot",
            "refresh_after_every_action": True,
            "persist_element_indices": False,
        },
    }
    return result


def verify_public_asset_url(
    url: str, *, expected_fingerprint: str, allowed_hosts: set[str],
    timeout_seconds: float = 15,
) -> dict[str, Any]:
    """GET an allowlisted image and prove its bytes match the approved asset."""

    if not _SHA256.fullmatch(expected_fingerprint):
        raise BeehiivAssistedPublisherError("expected asset fingerprint is invalid")
    normalized_hosts = {host.lower().rstrip(".") for host in allowed_hosts if host.strip()}
    _validate_public_url(url, normalized_hosts)
    opener = build_opener(_AllowlistedRedirectHandler(normalized_hosts))
    try:
        response = opener.open(
            Request(url, headers={"Accept": "image/*", "User-Agent": "BrandOS-AssetVerifier/1.0"}),
            timeout=timeout_seconds,
        )
    except (HTTPError, OSError) as exc:
        raise BeehiivAssistedPublisherError("public asset could not be fetched safely") from exc
    with response:
        final_url = response.geturl()
        _validate_public_url(final_url, normalized_hosts)
        if response.status != 200:
            raise BeehiivAssistedPublisherError("public asset did not return HTTP 200")
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
        if not content_type.startswith("image/"):
            raise BeehiivAssistedPublisherError("public asset is not an image content type")
        body = response.read(20 * 1024 * 1024 + 1)
    if not body or len(body) > 20 * 1024 * 1024:
        raise BeehiivAssistedPublisherError("public asset is empty or exceeds 20 MB")
    actual = "sha256:" + sha256(body).hexdigest()
    if actual != expected_fingerprint:
        raise BeehiivAssistedPublisherError("public asset fingerprint does not match approval")
    return {
        "final_url": final_url, "http_status": 200, "content_type": content_type,
        "bytes": len(body), "asset_fingerprint": actual,
    }


class BeehiivAssistedPublisher:
    """Execute and verify one private-draft manifest through a tested driver."""

    def run(
        self, manifest: Mapping[str, Any], driver: BeehiivPrivateDraftDriver,
    ) -> BeehiivAssistedDraftReceipt:
        _validate_manifest_boundary(manifest)
        reconciliation = manifest["reconciliation"]
        opened = driver.reconcile_private_draft(
            existing_draft_id=reconciliation.get("existing_draft_id"),
            idempotency_key=str(manifest["idempotency_key"]),
        )
        expected_existing = reconciliation.get("existing_draft_id")
        if expected_existing and opened.get("external_id") != expected_existing:
            raise BeehiivAssistedPublisherError(
                "reconciliation opened a different Beehiiv draft; duplicate creation is forbidden"
            )
        for field in ("title", "subject", "preview_text"):
            driver.set_text_field(field, str(manifest["fields"][field]))
        driver.replace_rich_text(str(manifest["body"]["content"]))
        driver.upload_thumbnail(
            str(manifest["thumbnail"]["path"]),
            str(manifest["thumbnail"]["asset_fingerprint"]),
        )
        driver.set_web_thumbnail_enabled(True)
        return verify_private_draft_readback(manifest, driver.read_back_private_draft())


def verify_private_draft_readback(
    manifest: Mapping[str, Any], readback: Mapping[str, Any],
) -> BeehiivAssistedDraftReceipt:
    """Issue a receipt only when provider read-back equals exact approved material."""

    _validate_manifest_boundary(manifest)
    if readback.get("status") != "draft":
        raise BeehiivAssistedPublisherError("Beehiiv read-back is not a private draft")
    if readback.get("scheduled_at") or readback.get("published_at") or readback.get("sent_at"):
        raise BeehiivAssistedPublisherError("Beehiiv read-back contains a forbidden delivery state")
    for field in ("title", "subject", "preview_text"):
        if readback.get(field) != manifest["fields"][field]:
            raise BeehiivAssistedPublisherError(f"Beehiiv {field} read-back does not match approval")
    if readback.get("body_html") != manifest["body"]["content"]:
        raise BeehiivAssistedPublisherError("Beehiiv rich-text read-back does not match approval")
    if readback.get("thumbnail_fingerprint") != manifest["thumbnail"]["asset_fingerprint"]:
        raise BeehiivAssistedPublisherError("Beehiiv thumbnail read-back does not match approved asset")
    if readback.get("display_thumbnail_on_web") is not True:
        raise BeehiivAssistedPublisherError("Beehiiv web thumbnail is not enabled")
    external_id = _required_text(readback, "external_id")
    existing = manifest["reconciliation"].get("existing_draft_id")
    if existing and external_id != existing:
        raise BeehiivAssistedPublisherError("Beehiiv read-back does not match the reconciled draft ID")
    preview_url = _required_text(readback, "preview_url")
    parts = urlsplit(preview_url)
    segments = [segment for segment in parts.path.split("/") if segment]
    if (
        parts.scheme != "https" or (parts.hostname or "").lower().rstrip(".") != "app.beehiiv.com"
        or parts.port is not None or parts.query or parts.fragment
        or len(segments) < 2 or segments[-2:] != ["posts", external_id]
    ):
        raise BeehiivAssistedPublisherError(
            "Beehiiv preview URL must be canonical HTTPS and match the draft ID"
        )
    return BeehiivAssistedDraftReceipt(
        issue_id=str(manifest["issue_id"]), revision=int(manifest["revision"]),
        external_id=external_id, preview_url=preview_url,
        content_fingerprint=str(manifest["content_fingerprint"]),
        asset_fingerprint=str(manifest["thumbnail"]["asset_fingerprint"]),
        idempotency_key=str(manifest["idempotency_key"]),
    )


def _validate_manifest_boundary(manifest: Mapping[str, Any]) -> None:
    if manifest.get("provider") != "beehiiv" or manifest.get("action") != "upsert_private_draft":
        raise BeehiivAssistedPublisherError("manifest is not a Beehiiv private-draft action")
    if set(manifest.get("forbidden_actions") or []) != {"schedule", "send", "publish"}:
        raise BeehiivAssistedPublisherError("private-draft forbidden-action boundary is incomplete")
    if manifest.get("thumbnail", {}).get("display_on_web") is not True:
        raise BeehiivAssistedPublisherError("web thumbnail enablement must be explicit")


def _required_text(values: Mapping[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BeehiivAssistedPublisherError(f"{key} is required")
    return value.strip()


def _deep_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    import json
    return json.loads(json.dumps(dict(value)))


def _validate_public_url(url: str, allowed_hosts: set[str]) -> None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if (
        parts.scheme != "https" or parts.port is not None or parts.username or parts.password
        or parts.query or parts.fragment or host not in allowed_hosts
    ):
        raise BeehiivAssistedPublisherError("public asset URL is not allowlisted canonical HTTPS")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise BeehiivAssistedPublisherError("public asset host could not be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise BeehiivAssistedPublisherError("public asset host resolved to a non-public address")


class _AllowlistedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str]) -> None:
        self.allowed_hosts = allowed_hosts
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_BEEHIIV_ID = re.compile(
    r"(?:post_[A-Za-z0-9-]{1,200}|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


__all__ = [
    "BeehiivAssistedDraftReceipt", "BeehiivAssistedPublisher",
    "BeehiivAssistedPublisherError", "BeehiivPrivateDraftDriver",
    "build_private_draft_manifest", "verify_private_draft_readback",
    "attach_verified_public_asset", "verify_public_asset_url",
]
