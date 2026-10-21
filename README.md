# BrandMan

<!-- mcp-name: io.github.AustinNChristensen/brandman -->

The open-source brand context + approval layer for your AI agents. Bring your own agent, model and keys.

Claude, Cursor, Codex, Grok Bot, and any other MCP client read one voice, one set of rules, and one approval trail. Agents draft. A person approves the exact revision. Nothing is posted until that approval, and the handoff is tied to the revision you approved.

**[BrandMan Cloud](https://usebrandman.com)** is the hosted edition. Features released in BrandMan are never moved behind a paywall. Cloud adds managed services, team, and scale.

> Early software. Expect rough edges. Channels are adapters (X, Beehiiv, a website). The brand context does not live inside a channel.

## Try it in 60 seconds

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/). No model key, and no Node.js.

```bash
uvx --from brandman brandman init --dir my-brand -y
cd my-brand
uvx --from brandman brandman serve
```

Open <http://127.0.0.1:8000/app>. The sign-in password is `BRANDMAN_PREVIEW_PASSWORD` in `.env`. Omit `-y` if you want `init` to ask for the brand name and mission. Without a terminal, omitted fields use defaults (the directory name, and a mission you should replace).

In another terminal, from `my-brand`:

```bash
uvx --from brandman brandman mcp-config --client cursor
```

That prints a command the agent can keep: `uvx --from brandman brandman-mcp`. It does not write a path inside the uv cache.

Ask the agent to read the brand context and draft an X post. It saves a draft. In the dashboard, open **Content → New X draft → Save & submit for review**, then **Approvals**, and approve that exact revision. **Execution** shows a pending handoff. Nothing is posted.

`brandman init` also writes `BRANDMAN_API_TOKEN`. Scripts and a self-hosted `/mcp` client send it as `Authorization: Bearer`. The dashboard password is still required. This is not a BrandMan Cloud workspace token.

## How agents connect

Every client talks to the same stdio server, `brandman-mcp`, against your SQLite file. Approving, scheduling, and publishing stay with a person. Details: [Connecting MCP clients](docs/mcp.md).

### Claude Code

```bash
uvx --from brandman brandman mcp-config --client claude-code
```

Run the printed `claude mcp add …` command. It passes `BRANDMAN_DB` and starts `brandman-mcp`.

### Cursor and Grok Bot

```bash
uvx --from brandman brandman mcp-config --client cursor
```

Merge the JSON into `.cursor/mcp.json` (or Cursor Settings → MCP). Grok Bot, when it runs in Cursor, uses that same MCP server. There is no separate Grok key inside BrandMan.

### Codex

Add this to `~/.codex/config.toml`. Use the absolute path `init` wrote for `BRANDMAN_DB`.

```toml
[mcp_servers.brandman]
command = "uvx"
args = ["--from", "brandman", "brandman-mcp"]

[mcp_servers.brandman.env]
BRANDMAN_DB = "/absolute/path/my-brand/brandman.db"
BRANDMAN_DATABASE_PROFILE = "operating"
```

### Claude Desktop

```bash
uvx --from brandman brandman mcp-config --client claude-desktop
```

Paste the JSON into `claude_desktop_config.json`.

A server you deploy also speaks MCP at `https://your-host/mcp` (OAuth, or the personal bearer token above). `brandman mcp-config --client claude-code --url https://your-host` prints that setup.

## Optional built-in drafting

With no provider configured, your agent or the dashboard writes the drafts. To have BrandMan call a model you already pay for, install an extra. The core package does not include a model SDK.

Install `brandman[openai]`, `brandman[anthropic]`, or `brandman[drafting]` for both. Set `BRANDMAN_DRAFT_PROVIDER` to `anthropic`, `openai`, or `none`. `openai` means any OpenAI-compatible endpoint (OpenAI, xAI, OpenRouter, Ollama, vLLM). See [Getting started](docs/getting-started.md).

## Other ways to run it

Docker, from a checkout of this repository:

```bash
cp .env.example .env
# Set BRANDMAN_PREVIEW_PASSWORD to a real secret. Leave ALLOWED_HOSTS and
# REQUIRE_HTTPS commented out for this loopback Compose file.
docker compose up -d    # pulls ghcr.io/austinnchristensen/brandman:latest
```

`latest` is the last published release. Add `--build` to build this checkout instead.

From a source checkout: Python 3.11+, uv, and Node.js 22.12+. `uv sync --locked`, then `uv run python scripts/build_dashboard.py`, then `uv run brandman init` and `uv run brandman serve`. Generated files in `brandman/static/app/` are not committed.

Older `brand-os-*` commands and `BRAND_OS_*` settings still run and warn. The canonical names are `brandman-*` and `BRANDMAN_*`.

Production, configuration, and the release wheel: [docs/deployment.md](docs/deployment.md), [docs/configuration.md](docs/configuration.md), [docs/release-wheel.md](docs/release-wheel.md).

## Documentation

- [Getting started](docs/getting-started.md)
- [Connecting MCP clients](docs/mcp.md)
- [Configuration](docs/configuration.md)
- [Seed packs](docs/seed-packs.md)
- [Deployment](docs/deployment.md)
- [Extending BrandMan](docs/extending.md)
- [Good first issues](docs/launch/good-first-issues.md)

## Development

```bash
uv sync && uv run pytest
cd web && npm ci && npm test && npm run build
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Contributors sign a CLA. Five scoped starter issues are in [docs/launch/good-first-issues.md](docs/launch/good-first-issues.md).

## License

Copyright (C) 2026 Austin Christensen.

[GNU Affero General Public License v3.0](LICENSE). If you run a modified version as a network service, you must offer its source to the users of that service. The BrandMan name and logo are not covered by the license.
