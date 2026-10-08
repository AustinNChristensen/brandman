"""Who is acting: the operator identity and privileged-human checks.

Self-hosted BrandMan has one human operator, named by ``BRANDMAN_OPERATOR``
(default ``operator``): the principal recorded on approvals and audits. The
HTTP Basic user name is separate (``BRANDMAN_BASIC_USER``, default ``operator``).

Hosts that authenticate many users (see ``brandman.extensions``) mark the
current request's principal as privileged instead; privileged-only actions
such as resolving product feedback or accepting an experiment winner then
accept that principal too.
"""
from __future__ import annotations

from contextvars import ContextVar
import os

DEFAULT_OPERATOR = "operator"

_privileged_principal: ContextVar[str | None] = ContextVar(
    "brandman_privileged_principal", default=None,
)


def operator_principal() -> str:
    return os.environ.get("BRANDMAN_OPERATOR", "").strip() or DEFAULT_OPERATOR


def set_privileged_principal(principal: str | None):
    """Mark ``principal`` as privileged for the current context; returns a reset token."""
    return _privileged_principal.set(principal)


def reset_privileged_principal(token) -> None:
    _privileged_principal.reset(token)


def is_privileged(actor: str) -> bool:
    return bool(actor) and (actor == operator_principal() or actor == _privileged_principal.get())
