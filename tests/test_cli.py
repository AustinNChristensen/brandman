import json
import os

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


def test_mcp_config_shapes():
    desktop = json.loads(cli.mcp_config("claude-desktop", database="/data/b.db"))
    server = desktop["mcpServers"]["brandman"]
    assert server["env"]["BRANDMAN_DB"] == "/data/b.db"
    hosted = json.loads(cli.mcp_config("json", database="x", url="https://brand.example/"))
    assert hosted["mcpServers"]["brandman"] == {"type": "http", "url": "https://brand.example/mcp"}
    assert cli.mcp_config("claude-code", database="x", url="https://b.example") == (
        "claude mcp add --transport http brandman https://b.example/mcp"
    )
