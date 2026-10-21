"""Keep the PyPI long-description marker and the MCP registry manifest aligned."""
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MCP_NAME = "io.github.AustinNChristensen/brandman"


def test_readme_mcp_name_matches_server_json_and_package_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    manifest = json.loads((ROOT / "server.json").read_text())
    readme = (ROOT / "README.md").read_text()
    description = manifest["description"]

    assert project["readme"] == "README.md"
    assert project["urls"]["Homepage"].startswith("https://github.com/AustinNChristensen/brandman")
    assert project["version"] == manifest["version"] == manifest["packages"][0]["version"]
    assert manifest["name"] == MCP_NAME
    assert f"mcp-name: {MCP_NAME}" in readme
    assert len(description) <= 100
    package = manifest["packages"][0]
    assert package["registryType"] == "pypi"
    assert package["identifier"] == "brandman"
    assert package["runtimeHint"] == "uvx"
    assert [item["value"] for item in package["runtimeArguments"]] == [
        "--from", "brandman", "brandman-mcp",
    ]


def test_readme_launch_positioning_and_registry_workflow():
    readme = (ROOT / "README.md").read_text()
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    issues = (ROOT / "docs/launch/good-first-issues.md").read_text()

    assert "open-source brand context + approval layer" in readme
    assert "bring your own agent, model and keys" in readme.casefold()
    assert "uvx --from brandman brandman init --dir my-brand -y" in readme
    assert "https://usebrandman.com" in readme
    assert "never moved behind a paywall" in readme
    for client in ("Claude Code", "Cursor", "Codex", "Grok Bot"):
        assert client in readme
    assert "mcp-publisher login github-oidc" in workflow
    assert "mcp-publisher publish" in workflow
    assert "needs: package" in workflow
    assert "v1.8.1" in workflow
    assert "login github-oidc" in (ROOT / "docs/mcp.md").read_text()
    headings = [line for line in issues.splitlines() if line.startswith("## ")]
    assert len(headings) == 5
