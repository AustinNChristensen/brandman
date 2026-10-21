"""BrandMan: a free, open-source agent-first brand brain.

BrandMan Cloud (https://usebrandman.com) is the hosted paid edition. It is not
part of this package. ``brand-os-*`` commands and ``BRAND_OS_*`` settings are
deprecated aliases of ``brandman-*`` and ``BRANDMAN_*``.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import warnings

LEGACY_ENV_PREFIX = "BRAND_OS_"
ENV_PREFIX = "BRANDMAN_"
LEGACY_ALIAS_HELP = (
    "brand-os-* commands and BRAND_OS_* settings are deprecated aliases of "
    "brandman-* and BRANDMAN_*. They still work and warn on use. "
    "BrandMan is free and open source (AGPL-3.0); BrandMan Cloud "
    "(https://usebrandman.com) is the hosted paid edition."
)


def _warn_deprecated(message: str) -> None:
    warnings.warn(message, DeprecationWarning, stacklevel=2)
    print(f"warning: {message}", file=sys.stderr)


def _adopt_legacy_environment() -> None:
    """Accept the pre-rename ``BRAND_OS_*`` variables for existing deployments.

    A ``BRANDMAN_*`` value always wins. The legacy name is copied only when the
    new one is absent, so a mixed configuration resolves deterministically.
    Any ``BRAND_OS_*`` name prints a deprecation warning, including when the
    canonical name is also set.
    """
    legacy = []
    for key, value in list(os.environ.items()):
        if not key.startswith(LEGACY_ENV_PREFIX):
            continue
        legacy.append(key)
        current = ENV_PREFIX + key[len(LEGACY_ENV_PREFIX):]
        if current not in os.environ:
            os.environ[current] = value
    if legacy:
        _warn_deprecated(
            "BRAND_OS_* environment variables are deprecated; rename "
            + ", ".join(sorted(legacy)) + " to BRANDMAN_*."
        )


def warn_if_legacy_command(argv0: str | None = None) -> None:
    """Warn when this process was started as a deprecated ``brand-os-*`` command."""
    invoked = Path(sys.argv[0] if argv0 is None else argv0).name
    if invoked.lower().endswith(".exe"):
        invoked = invoked[:-4]
    if invoked != "brand-os" and not invoked.startswith("brand-os-"):
        return
    canonical = "brandman" + invoked[len("brand-os"):]
    _warn_deprecated(f"{invoked} is deprecated; use {canonical} instead.")


_adopt_legacy_environment()
