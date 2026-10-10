"""Build same-origin dashboard assets for a source checkout or release package."""
from pathlib import Path
import shutil
import subprocess


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit(
            "Building the dashboard requires Node.js 22.12+ and npm. "
            "Install them to build from source, or install a prebuilt BrandMan wheel."
        )
    subprocess.run([npm, "ci"], cwd=root / "web", check=True)
    subprocess.run([npm, "run", "build"], cwd=root / "web", check=True)
    if not (root / "brandman" / "static" / "app" / "index.html").is_file():
        raise SystemExit("Dashboard build did not produce brandman/static/app/index.html")


if __name__ == "__main__":
    main()
