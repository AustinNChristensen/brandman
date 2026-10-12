"""Startup guard for the generated dashboard (web/ -> brandman/static/app).

The built dashboard is not tracked in git, so a deploy that runs the app from a
raw checkout and skips the build would otherwise serve 503 at /app on the first
user request. ``ensure_dashboard`` runs at application startup instead: it builds
the dashboard when this is a source checkout with Node available, and otherwise
refuses to start with an actionable error.
"""
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

_FALSE = {"0", "false", "no", "off"}


class DashboardMissingError(RuntimeError):
    """The dashboard build is absent and could not be produced at startup."""


def _enabled(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() not in _FALSE


def ensure_dashboard(dashboard_dir: Path) -> None:
    """Guarantee ``dashboard_dir/index.html`` exists or raise ``DashboardMissingError``.

    ``BRANDMAN_REQUIRE_DASHBOARD=false`` downgrades the failure to a warning for
    API-only deployments. ``BRANDMAN_AUTO_BUILD_DASHBOARD=false`` disables the
    startup build so a deploy must run ``scripts/build_dashboard.py`` itself.
    """
    entry = dashboard_dir / "index.html"
    if entry.is_file():
        return
    if not _enabled("BRANDMAN_REQUIRE_DASHBOARD", True):
        logger.warning("Dashboard build is missing at %s; /app will return 503", dashboard_dir)
        return

    root = dashboard_dir.parents[2]
    script = root / "scripts" / "build_dashboard.py"
    reason = "no source checkout (web/ and scripts/build_dashboard.py) was found to build from"
    if (root / "web" / "package.json").is_file() and script.is_file():
        if not _enabled("BRANDMAN_AUTO_BUILD_DASHBOARD", True):
            reason = "BRANDMAN_AUTO_BUILD_DASHBOARD is disabled"
        elif shutil.which("npm") is None:
            reason = "Node.js 22.12+ and npm are not installed"
        else:
            logger.warning("Dashboard build is missing; building it now (this can take a minute)")
            try:
                subprocess.run([sys.executable, str(script)], cwd=root, check=True)
            except (subprocess.CalledProcessError, OSError) as error:
                reason = f"the startup build failed ({error})"
            else:
                if entry.is_file():
                    return
                reason = "the startup build finished without producing index.html"

    raise DashboardMissingError(
        f"BrandMan dashboard build is missing at {dashboard_dir} ({reason}). "
        "Run `python scripts/build_dashboard.py` in the deploy/build step, install a "
        "prebuilt wheel or the Docker image, or set BRANDMAN_REQUIRE_DASHBOARD=false "
        "to run API-only."
    )
