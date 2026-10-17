"""Locally prove the standalone wrapper; never contacts FastAPI Cloud.

Run with Python and uv available. Only wrapper files and the smoke probe are
copied into scratch. Installs use a fresh environment, no cache and no builds.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib


WHEEL_SHA256 = "c09d09d3ed6d29a288ffed5d5ffaca44bf7aea51a3f5d6314b67636ba32e1ff2"


def check_lock(wrapper: Path) -> None:
    project = tomllib.loads((wrapper / "pyproject.toml").read_text())
    assert project["project"]["dependencies"] == ["brandman[anthropic]==0.1.0"]
    assert project["tool"]["uv"]["no-build"] is True
    assert project["tool"]["uv"]["package"] is False
    assert project["tool"]["fastapi"]["entrypoint"] == "main:app"
    lock = tomllib.loads((wrapper / "uv.lock").read_text())
    core, = [p for p in lock["package"] if p["name"] == "brandman"]
    assert core["version"] == "0.1.0"
    assert core["source"] == {"registry": "https://pypi.org/simple"}
    wheel, = core["wheels"]
    assert wheel["hash"] == f"sha256:{WHEEL_SHA256}", "Published core wheel receipt changed"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", default=shutil.which("uv"))
    args = parser.parse_args()
    assert args.uv, "uv must be available"
    uv = str(Path(args.uv).resolve())
    root = Path(__file__).resolve().parent.parent
    wrapper = root / "deploy" / "fastapi-cloud"
    check_lock(wrapper)
    python = subprocess.check_output([uv, "python", "find", "3.11"], text=True).strip()
    with tempfile.TemporaryDirectory(prefix="brandman-cloud-proof-") as temp:
        scratch = Path(temp)
        app = scratch / "app"
        app.mkdir()
        for name in ("pyproject.toml", "uv.lock", "main.py"):
            shutil.copy2(wrapper / name, app / name)
        smoke = scratch / "check_dashboard_package.py"
        shutil.copy2(root / "scripts" / smoke.name, smoke)
        empty_bin = scratch / "empty-bin"
        empty_bin.mkdir()
        # Inherit no application configuration, secrets, index overrides or PATH.
        environment = {"PATH": str(empty_bin), "UV_PYTHON_DOWNLOADS": "never"}
        subprocess.run([
            uv, "sync", "--project", str(app), "--python", python,
            "--locked", "--no-cache", "--no-build", "--no-dev",
        ], env=environment, cwd=app, check=True)
        runtime = app / ".venv" / "bin" / "python"
        subprocess.run([
            str(runtime), "-I", str(smoke), "--python-only",
            "--entrypoint-file", str(app / "main.py"),
        ], env=environment, cwd=app, check=True)
    print(f"Cloud wrapper proof passed: brandman 0.1.0 wheel sha256:{WHEEL_SHA256}")


if __name__ == "__main__":
    main()
