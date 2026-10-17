from pathlib import Path
import io
import os
import runpy
import shutil
import subprocess
import tarfile

import pytest


ROOT = Path(__file__).resolve().parent.parent
CHECK = runpy.run_path(str(ROOT / "scripts" / "check_cloud_wrapper.py"))


def test_cloud_wrapper_uses_exact_published_wheel_and_no_source_builds():
    CHECK["check_lock"](ROOT / "deploy" / "fastapi-cloud")


@pytest.mark.parametrize("change", ["hash", "build", "version", "entrypoint"])
def test_wrapper_check_rejects_receipt_or_install_policy_drift(tmp_path, change):
    source = ROOT / "deploy" / "fastapi-cloud"
    project = (source / "pyproject.toml").read_text()
    lock = (source / "uv.lock").read_text()
    if change == "hash":
        lock = lock.replace(CHECK["WHEEL_SHA256"], "0" * 64)
    elif change == "build":
        project = project.replace("no-build = true", "no-build = false")
    elif change == "version":
        project = project.replace("brandman[anthropic]==0.1.0", "brandman[anthropic]>=0.1.0")
    else:
        project = project.replace('entrypoint = "main:app"', 'entrypoint = "old.main:app"')
    (tmp_path / "pyproject.toml").write_text(project)
    (tmp_path / "uv.lock").write_text(lock)
    with pytest.raises(AssertionError):
        CHECK["check_lock"](tmp_path)


def test_wrapper_policy_rejects_sdist_before_build_backend_runs(tmp_path):
    uv = shutil.which("uv")
    assert uv, "the supported test runner requires uv on PATH"
    sentinel = tmp_path / "build-ran"
    package = tmp_path / "brandman-0.1.0.tar.gz"
    files = {
        "pyproject.toml": '[build-system]\nrequires=[]\nbuild-backend="backend"\nbackend-path=["."]\n'
                          '[project]\nname="brandman"\nversion="0.1.0"\n',
        "backend.py": f'from pathlib import Path\nPath({str(sentinel)!r}).touch()\nraise RuntimeError("build ran")\n',
    }
    with tarfile.open(package, "w:gz") as archive:
        for name, source in files.items():
            data = source.encode()
            info = tarfile.TarInfo(f"brandman-0.1.0/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    result = subprocess.run([
        uv, "pip", "install", "--target", str(tmp_path / "installed"),
        "--no-cache", "--no-deps", "--no-index", str(package),
    ], cwd=ROOT / "deploy" / "fastapi-cloud",
        env={k: v for k, v in os.environ.items() if not k.startswith("UV_NO_BUILD")},
        capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "build" in result.stderr.lower(), result.stderr
    assert not sentinel.exists(), result.stderr
