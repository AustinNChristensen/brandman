import os
import subprocess
import sys


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("BRANDMAN_", "BRAND_OS_"))}
    return subprocess.run(
        [sys.executable, "-W", "ignore", "-c",
         "import brandman, os; print(os.environ.get('BRANDMAN_WORKER_MAX_JOBS', ''))"],
        env={**clean, **env}, capture_output=True, text=True, check=True,
    )


def test_legacy_brand_os_variables_are_adopted():
    result = _run({"BRAND_OS_WORKER_MAX_JOBS": "7"})
    assert result.stdout.strip() == "7"
    assert "BRAND_OS_WORKER_MAX_JOBS" in result.stderr
    assert "deprecated" in result.stderr
    assert "BRANDMAN_*" in result.stderr


def test_current_variable_wins_over_legacy_name():
    result = _run({"BRAND_OS_WORKER_MAX_JOBS": "7", "BRANDMAN_WORKER_MAX_JOBS": "9"})
    assert result.stdout.strip() == "9"
    assert "BRAND_OS_WORKER_MAX_JOBS" in result.stderr
    assert "deprecated" in result.stderr


def test_canonical_variable_does_not_warn():
    result = _run({"BRANDMAN_WORKER_MAX_JOBS": "9"})
    assert result.stdout.strip() == "9"
    assert result.stderr == ""
