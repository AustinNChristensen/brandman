import os
import subprocess
import sys


def _resolved(env: dict[str, str]) -> str:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("BRANDMAN_", "BRAND_OS_"))}
    return subprocess.run(
        [sys.executable, "-W", "ignore", "-c",
         "import brandman, os; print(os.environ.get('BRANDMAN_WORKER_MAX_JOBS', ''))"],
        env={**clean, **env}, capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_legacy_brand_os_variables_are_adopted():
    assert _resolved({"BRAND_OS_WORKER_MAX_JOBS": "7"}) == "7"


def test_current_variable_wins_over_legacy_name():
    assert _resolved({"BRAND_OS_WORKER_MAX_JOBS": "7", "BRANDMAN_WORKER_MAX_JOBS": "9"}) == "9"
