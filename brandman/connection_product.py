"""Plain-language, secret-free connection onboarding contracts.

This module is deliberately data-only.  It tells an operator what each lane can
do before a credential is entered; it never accepts or returns secret material.
"""

from __future__ import annotations

from typing import Any


CONNECTION_LANES: dict[str, dict[str, Any]] = {
    "beehiiv_read": {
        "provider": "beehiiv",
        "title": "Beehiiv insights",
        "purpose": "Read newsletter posts and aggregate performance.",
        "scopes": ["posts.read"],
        "capabilities": ["posts.read", "metrics.read"],
        "credential_fields": ["api_key"],
        "can_read": True,
        "can_write": False,
        "write_boundary": "Cannot create, schedule, publish, or send newsletters.",
    },
    "beehiiv_write": {
        "provider": "beehiiv",
        "title": "Beehiiv draft creation",
        "purpose": "Create an unpublished draft after exact human approval.",
        "scopes": ["posts.write"],
        "capabilities": ["drafts.write"],
        "credential_fields": ["api_key"],
        "can_read": False,
        "can_write": True,
        "write_boundary": "Draft only. Cannot schedule, publish, or send.",
    },
    "x_read": {
        "provider": "x",
        "title": "X insights",
        "purpose": "Read owned posts and performance.",
        "scopes": ["tweet.read", "users.read", "offline.access"],
        "capabilities": ["metrics.read", "engagement.read"],
        "credential_fields": ["access_token", "refresh_token", "client_id", "expires_at"],
        "can_read": True,
        "can_write": False,
        "write_boundary": "Cannot create or publish posts.",
    },
    "x_write": {
        "provider": "x",
        "title": "X approved-post delivery",
        "purpose": "Publish only an exact, separately approved post.",
        "scopes": ["tweet.read", "tweet.write", "users.read", "offline.access"],
        "capabilities": ["posts.write"],
        "credential_fields": ["access_token", "refresh_token", "client_id", "expires_at"],
        "can_read": False,
        "can_write": True,
        "write_boundary": "Exact approved revision only; no autonomous publishing.",
    },
}


# Stable billing classifications, not prices. The customer supplies every price.
RATE_CARD_OPERATIONS: dict[str, dict[str, str]] = {
    "beehiiv_read": {
        "provider": "beehiiv", "method": "GET",
        "endpoint_pattern": "https://api.beehiiv.com/v2/publications/*/posts",
        "billable_category": "post.read", "unit_name": "resource",
    },
    "beehiiv_draft": {
        "provider": "beehiiv", "method": "POST",
        "endpoint_pattern": "https://api.beehiiv.com/v2/publications/*/posts",
        "billable_category": "post.create", "unit_name": "request",
    },
    "x_owned_read": {
        "provider": "x", "method": "GET",
        "endpoint_pattern": "https://api.x.com/2/users/*/tweets",
        "billable_category": "post.read_owned", "unit_name": "resource",
    },
    "x_general_read": {
        "provider": "x", "method": "GET",
        "endpoint_pattern": "https://api.x.com/2/tweets*",
        "billable_category": "post.read_general", "unit_name": "resource",
    },
    "x_plain_post": {
        "provider": "x", "method": "POST",
        "endpoint_pattern": "https://api.x.com/2/tweets",
        "billable_category": "post.create_plain", "unit_name": "request",
    },
    "x_link_post": {
        "provider": "x", "method": "POST",
        "endpoint_pattern": "https://api.x.com/2/tweets",
        "billable_category": "post.create_with_url", "unit_name": "request",
    },
}


def onboarding_manifest(*, encryption_configured: bool) -> dict[str, Any]:
    """Return a stable non-secret mode and lane comparison for first-run UI."""

    lanes = []
    for key, definition in CONNECTION_LANES.items():
        lanes.append({
            "lane": key,
            **{name: value for name, value in definition.items() if name != "credential_fields"},
            "credential_inputs": [
                {
                    "key": field,
                    "label": {
                        "api_key": "API token",
                        "access_token": "OAuth access token",
                        "refresh_token": "OAuth refresh token",
                        "client_id": "OAuth client ID",
                        "expires_at": "Access token expiry",
                    }[field],
                    "secret": field != "expires_at",
                }
                for field in definition["credential_fields"]
            ],
        })
    return {
        "encryption": {
            "ready": encryption_configured,
            "required_for": "Every standalone API or OAuth connection",
            "operator_action": None if encryption_configured else (
                "Ask the BrandOS administrator to configure credential encryption before entering a token."
            ),
        },
        "modes": [
            {
                "mode": "assisted",
                "title": "Browser-assisted",
                "available_now": True,
                "credentials_stored": False,
                "best_for": "Starting now without a provider developer account or supported write API.",
                "tradeoff": "A signed-in browser helper and human confirmation are required for each public action.",
            },
            {
                "mode": "standalone",
                "title": "Standalone API",
                "available_now": encryption_configured,
                "credentials_stored": True,
                "best_for": "Durable scheduled reads and approved actions without an open browser.",
                "tradeoff": "Requires provider API access, credential encryption, and separate least-privilege lanes.",
            },
        ],
        "providers": {
            "beehiiv": {
                "authentication": "API token",
                "availability": "Depends on the customer's Beehiiv plan and API permissions.",
                "lanes": [lane for lane in lanes if lane["provider"] == "beehiiv"],
            },
            "x": {
                "authentication": "OAuth 2.0 with refresh-token rotation",
                "availability": "Requires the customer's X developer project. Guided OAuth authorization is a future setup step.",
                "lanes": [lane for lane in lanes if lane["provider"] == "x"],
            },
        },
        "pricing": {
            "source": "customer_supplied_rate_card",
            "vendor_prices_bundled": False,
            "explanation": (
                "BrandOS records payload-free usage. An administrator enters a versioned rate card "
                "from the customer's provider agreement; BrandOS does not assume vendor pricing."
            ),
        },
    }


__all__ = ["CONNECTION_LANES", "RATE_CARD_OPERATIONS", "onboarding_manifest"]
