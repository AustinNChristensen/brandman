"""PyPI metadata stays aligned with the Show HN README and the package version."""
import tomllib
from pathlib import Path

from brandman import __version__
from brandman.main import app

ROOT = Path(__file__).resolve().parents[1]


def test_package_version_matches_the_api_and_readme_metadata():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == __version__ == "0.1.1"
    assert app.version == __version__
    assert project["readme"] == "README.md"
    assert project["license"] == "AGPL-3.0-only"
    assert project["urls"]["Homepage"] == "https://usebrandman.com"
    assert project["urls"]["Repository"] == "https://github.com/AustinNChristensen/brandman"
    readme = (ROOT / "README.md").read_text()
    assert "is not paywalled" in readme
    assert "AGPL-3.0" in readme
    assert "https://usebrandman.com" in readme
    assert "pip install brandman" in readme
    assert "uvx --from brandman brandman-mcp" in readme
    assert "uvx brandman" in readme
    assert "Exact-revision approval" in readme
