"""Extension points for hosts that build on BrandMan.

The self-hosted app needs none of this. A host such as a managed, multi-user
deployment uses it to:

* replace the single-operator password gate with its own authentication
  (``set_authenticator``), including choosing which database a request uses;
* add routes, MCP tools or startup work from an installed package
  (``brandman.plugins`` entry points, see ``load_plugins``).

The HTTP boundary checks (allowed hosts, HTTPS, cross-site mutation refusal and
response hardening) always run before an authenticator and cannot be replaced.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
import inspect
import logging
import os
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Authentication:
    """A request the host has authenticated (or deliberately made public).

    ``principal`` is recorded as the human actor on approvals; ``None`` marks a
    public request (for example a sign-in page) that must not reach any
    principal-attributed action. ``privileged`` lets the principal perform
    owner-only actions (see ``brandman.principals``). ``database`` binds the
    request to one isolated database.
    """

    principal: str | None
    privileged: bool = False
    database: Path | None = None


AuthenticatorResult = Authentication | Response
Authenticator = Callable[[Request], AuthenticatorResult | Awaitable[AuthenticatorResult]]

_authenticator: Authenticator | None = None


def set_authenticator(authenticator: Authenticator | None) -> None:
    """Install (or with ``None`` remove) the host's request authenticator."""
    global _authenticator
    _authenticator = authenticator


def get_authenticator() -> Authenticator | None:
    return _authenticator


async def authenticate(request: Request) -> AuthenticatorResult | None:
    if _authenticator is None:
        return None
    result = _authenticator(request)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, (Authentication, Response)):
        raise TypeError("an authenticator must return Authentication or a Response")
    return result


_loaded_plugins: dict[str, Any] = {}


def load_plugins(target: str, subject: Any) -> list[str]:
    """Call ``register_<target>(subject)`` on every installed BrandMan plugin.

    A plugin is any object exposed under the ``brandman.plugins`` entry-point
    group; it may define ``register_app(app)`` and/or ``register_mcp(mcp)``.
    ``BRANDMAN_DISABLE_PLUGINS=1`` skips plugin loading entirely.
    """
    if os.environ.get("BRANDMAN_DISABLE_PLUGINS", "").strip() in {"1", "true", "yes"}:
        return []
    registered = []
    for entry in entry_points(group="brandman.plugins"):
        plugin = _loaded_plugins.get(entry.name)
        if plugin is None:
            plugin = _loaded_plugins[entry.name] = entry.load()
        hook = getattr(plugin, f"register_{target}", None)
        if hook is None:
            continue
        hook(subject)
        registered.append(entry.name)
        logger.info("registered BrandMan plugin %s (%s)", entry.name, target)
    return registered


__all__ = [
    "Authentication", "Authenticator", "authenticate", "get_authenticator",
    "load_plugins", "set_authenticator",
]
