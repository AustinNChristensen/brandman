"""BrandMan: an agent-first brand brain."""
from __future__ import annotations

import os
import warnings

LEGACY_ENV_PREFIX = "BRAND_OS_"
ENV_PREFIX = "BRANDMAN_"


def _adopt_legacy_environment() -> None:
    """Accept the pre-rename ``BRAND_OS_*`` variables for existing deployments.

    A ``BRANDMAN_*`` value always wins. The legacy name is copied only when the
    new one is absent, so a mixed configuration resolves deterministically.
    """
    adopted = []
    for key, value in list(os.environ.items()):
        if not key.startswith(LEGACY_ENV_PREFIX):
            continue
        current = ENV_PREFIX + key[len(LEGACY_ENV_PREFIX):]
        if current not in os.environ:
            os.environ[current] = value
            adopted.append(key)
    if adopted:
        warnings.warn(
            "BRAND_OS_* environment variables are deprecated; rename "
            + ", ".join(sorted(adopted)) + " to BRANDMAN_*.",
            DeprecationWarning, stacklevel=2,
        )


_adopt_legacy_environment()
