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
