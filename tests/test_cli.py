import json
import os

import pytest

from brandman import cli, store


def test_init_writes_private_env_and_creates_the_brand(tmp_path, monkeypatch):
    for key in ("BRANDMAN_DB", "BRANDMAN_PREVIEW_PASSWORD", "BRANDMAN_SEED_PACK", "BRANDMAN_CREDENTIAL_MASTER_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BRANDMAN_DATABASE_PROFILE", "test")
    previous, environment = store.DATA_PATH, dict(os.environ)
    try:
        assert cli.main([
            "init", "--dir", str(tmp_path), "--name", "Acme Widgets",
            "--mission", "Help people pick widgets.", "-y",
        ]) == 0
        env = (tmp_path / ".env").read_text()
        assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
        assert "BRANDMAN_SEED_PACK=none" in env and "BRANDMAN_PREVIEW_PASSWORD=" in env
        assert "BRANDMAN_API_TOKEN=" in env
        token = next(line.split("=", 1)[1] for line in env.splitlines() if line.startswith("BRANDMAN_API_TOKEN="))
        assert len(token) >= 32
        brand = store.get_brand("acme-widgets")
        assert brand["mission"] == "Help people pick widgets."
        assert store.get_brand("demo-brand") is None
    finally:
        store.DATA_PATH = previous
        os.environ.clear()
        os.environ.update(environment)


def test_env_file_never_overrides_the_real_environment(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("A_TEST_KEY=from-file\nB_TEST_KEY='quoted' # note\n")
    monkeypatch.setenv("A_TEST_KEY", "from-env")
    monkeypatch.delenv("B_TEST_KEY", raising=False)
    cli.load_env_file(path)
    assert os.environ["A_TEST_KEY"] == "from-env" and os.environ["B_TEST_KEY"] == "quoted"
    monkeypatch.delenv("B_TEST_KEY")


def test_init_without_a_terminal_uses_directory_defaults(tmp_path, monkeypatch):
    for key in ("BRANDMAN_DB", "BRANDMAN_PREVIEW_PASSWORD", "BRANDMAN_SEED_PACK", "BRANDMAN_CREDENTIAL_MASTER_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BRANDMAN_DATABASE_PROFILE", "test")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    directory = tmp_path / "cedar-studio"
    previous, environment = store.DATA_PATH, dict(os.environ)
    try:
        assert cli.main(["init", "--dir", str(directory)]) == 0
        brand = store.get_brand("cedar-studio")
        assert brand["name"] == "Cedar Studio"
        assert brand["mission"] == "Help the right people understand and choose this brand."
        assert "generic marketing fluff" in brand["voice"]
    finally:
        store.DATA_PATH = previous
        os.environ.clear()
        os.environ.update(environment)


def test_draft_reports_setup_before_a_missing_source(tmp_path, monkeypatch):
    for key in (
        "BRANDMAN_DB", "BRANDMAN_PREVIEW_PASSWORD", "BRANDMAN_SEED_PACK",
        "BRANDMAN_CREDENTIAL_MASTER_KEY", "BRANDMAN_API_TOKEN", "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY", "BRANDMAN_DRAFT_API_KEY", "BRANDMAN_DRAFT_BASE_URL",
        "BRANDMAN_DRAFT_PROVIDER", "BRANDMAN_DRAFT_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BRANDMAN_DATABASE_PROFILE", "test")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    directory = tmp_path / "cedar-studio"
    previous, environment = store.DATA_PATH, dict(os.environ)
    try:
        assert cli.main(["init", "--dir", str(directory)]) == 0
        with pytest.raises(SystemExit) as raised:
            cli.main(["draft", "--brand", "cedar-studio", "--source", "missing", "--env-file", str(directory / ".env")])
    finally:
        store.DATA_PATH = previous
        os.environ.clear()
        os.environ.update(environment)
    message = str(raised.value)
    assert "source not found" not in message
    assert "BRANDMAN_DRAFT_PROVIDER" in message


def test_mcp_subcommand_starts_the_stdio_server(monkeypatch):
    started = {}

    def run_mcp():
        started["yes"] = True

    monkeypatch.setattr("brandman.mcp_server.main", run_mcp)
    assert cli.main(["mcp"]) == 0
    assert started == {"yes": True}


def test_mcp_config_shapes():
    desktop = json.loads(cli.mcp_config("claude-desktop", database="/data/b.db"))
    server = desktop["mcpServers"]["brandman"]
    assert server["env"]["BRANDMAN_DB"] == "/data/b.db"
    hosted = json.loads(cli.mcp_config("json", database="x", url="https://brand.example/"))
    assert hosted["mcpServers"]["brandman"] == {"type": "http", "url": "https://brand.example/mcp"}
    assert cli.mcp_config("claude-code", database="x", url="https://b.example") == (
        "claude mcp add --transport http brandman https://b.example/mcp"
    )


def test_mcp_config_keeps_a_persistent_entrypoint(monkeypatch, tmp_path):
    executable = tmp_path / ".local" / "bin" / "brandman-mcp"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    monkeypatch.setattr(cli.shutil, "which", lambda name: str(executable) if name == "brandman-mcp" else None)
    server = json.loads(cli.mcp_config("cursor", database="/data/b.db"))["mcpServers"]["brandman"]
    assert server["command"] == str(executable)
    assert "args" not in server


def test_mcp_config_replaces_a_uv_cache_path_with_uvx(monkeypatch, tmp_path):
    cached = tmp_path / ".cache" / "uv" / "archive-v0" / "abc" / "bin" / "brandman-mcp"
    cached.parent.mkdir(parents=True)
    cached.write_text("#!/bin/sh\n")
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: str(cached) if name == "brandman-mcp" else None)
    server = json.loads(cli.mcp_config("cursor", database="/data/b.db"))["mcpServers"]["brandman"]
    assert server["command"] == "uvx"
    assert server["args"] == ["brandman", "mcp"]
    rendered = cli.mcp_config("claude-code", database="/data/b.db")
    assert "uvx brandman mcp" in rendered
    assert "brandman-mcp" not in rendered
    assert "archive-v0" not in rendered
