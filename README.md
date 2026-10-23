# BrandMan

BrandMan is an agent-first brand brain. It keeps a brand's mission, voice,
compliance rules, audience personas, sources, campaigns, and results in one
place, and lets AI agents (through MCP) and people (through a dashboard) work
from that same record.

**This repository is the AGPL-3.0 open core.** It is free software and is not
paywalled. You can run, modify, and self-host it with no BrandMan Cloud
account. If you run a modified version as a network service, the AGPL requires
you to offer that source to the people who use the service. The BrandMan name
and logo are not covered by the license.

**[BrandMan Cloud](https://usebrandman.com)** is a separate paid product. It is
this core plus closed components (a hosted service, billing, multi-tenancy, and
similar). Cloud has more features. The open core is the smaller, functional
system: one SQLite database, a password-gated dashboard, and an MCP server you
run yourself. Cloud is not in this repository, and you do not need it to use
the core.

> Status: early (0.1.x). The core runs and is tested. Expect rough edges and
> breaking changes.

## What the open core does

- **Brand context.** Mission, voice, guidelines, personas, and compliance rules,
  readable by agents and people.
- **Content workflow.** Sources, campaigns, channel drafts, and newsletter
  issues.
- **Exact-revision approval.** A person approves one revision. The MCP server
  can draft and submit that revision. It has no tool that approves or publishes.
  Editing an approved item clears the approval, bumps the revision, and sends it
  back to draft. Nothing is posted to a channel automatically.
- **Feedback.** Record what a channel did, propose a learning, and accept it
  into the brand context only with an explicit decision.
- **Missions.** Time-boxed goals with a baseline, a target, and a trajectory.
- **MCP.** The same flows for Claude, Cursor, and Codex, over stdio
  (`brandman-mcp`) or a hosted OAuth endpoint (`/mcp`).
- **Dashboard and REST API.** Operator UI at `/app`, and the API behind it.
- **Channels at the edge.** X and Beehiiv are replaceable adapters. The brand
  record does not live inside a channel.

## Install

Python 3.11 or newer. Package installers download the latest
[`brandman` release on PyPI](https://pypi.org/project/brandman/). This
repository can be ahead of that release. The wheel includes the dashboard, so
a PyPI install does not need Node.js.

```bash
pip install brandman
# or: uv tool install brandman
# or: pipx install brandman
```

In an empty directory:

```bash
brandman init     # writes .env (mode 600) and brandman.db, then asks for the brand
brandman serve    # http://127.0.0.1:8000/app
```

Sign in with `BRANDMAN_PREVIEW_PASSWORD` from `.env`. Pass `--name`,
`--mission`, `--voice`, `--compliance`, and `-y` to skip the prompts.

`brandman init` sets the brand's approval policy to `human_approval_required`.

### Connect Claude, Cursor, or Codex

`brandman mcp-config` prints a config for this install. `--client` accepts
`claude-desktop` (the default), `claude-code`, `cursor`, `codex`, and `json`.

When `brandman-mcp` is on `PATH`, the printed command is that executable.
When it is not, the command is `uvx --from brandman brandman-mcp`. That `uvx`
form is a real entry point of the package (`brandman-mcp`), not a separate
installer. `uvx brandman` starts the `brandman` CLI instead, which is the wrong
process for an MCP client.

Claude Desktop and Cursor use the same JSON (`claude_desktop_config.json`,
`.cursor/mcp.json`). This is the block printed when `brandman-mcp` is not on
`PATH`:

```json
{
  "mcpServers": {
    "brandman": {
      "command": "uvx",
      "args": ["--from", "brandman", "brandman-mcp"],
      "env": {
        "BRANDMAN_DB": "/absolute/path/brandman.db",
        "BRANDMAN_DATABASE_PROFILE": "operating"
      }
    }
  }
}
```

Claude Code:

```bash
claude mcp add brandman \
  -e BRANDMAN_DB=/absolute/path/brandman.db \
  -e BRANDMAN_DATABASE_PROFILE=operating \
  -- uvx --from brandman brandman-mcp
```

Codex writes `~/.codex/config.toml`:

```bash
codex mcp add brandman \
  --env BRANDMAN_DB=/absolute/path/brandman.db \
  --env BRANDMAN_DATABASE_PROFILE=operating \
  -- uvx --from brandman brandman-mcp
```

`brandman mcp-config --client codex` prints that command.
`brandman mcp-config --help` lists the clients the installed release knows.
The `codex mcp add` line above talks to the same `brandman-mcp` server either
way.

A deployed BrandMan serves MCP at `https://your-host/mcp` (OAuth 2.1, PKCE,
dynamic client registration):

```bash
brandman mcp-config --client claude-code --url https://your-host
# claude mcp add --transport http brandman https://your-host/mcp

brandman mcp-config --client codex --url https://your-host
# codex mcp add brandman --url https://your-host/mcp
```

Useful first tools: `list_brands`, `get_brand_context`,
`create_editorial_candidate`, `create_campaign`, `draft_post`,
`create_newsletter_issue`, `revise_newsletter_issue`, `get_active_mission`,
`report_product_gap`. The full list is the tools on `brandman-mcp`.

### Run this repository

Requires [uv](https://docs.astral.sh/uv/) and Node.js 22.12+ with npm. The
dashboard source is tracked; generated files under `brandman/static/app/` are
not.

```bash
git clone https://github.com/AustinNChristensen/brandman.git
cd brandman
uv sync --locked
uv run python scripts/build_dashboard.py
uv run brandman init
uv run brandman serve
uv run brandman mcp-config --client cursor
```

`uv build` (with Node on the build machine) produces a wheel that already
contains the dashboard. Editable installs do not build the frontend on their
own.

### Docker

```bash
cp .env.example .env    # set BRANDMAN_PREVIEW_PASSWORD (16+ characters)
docker compose up -d    # http://127.0.0.1:8000/app
```

The published port is loopback-only. See
[deployment](https://github.com/AustinNChristensen/brandman/blob/main/docs/deployment.md).

### Optional built-in drafting

MCP agents do not need this. To draft from a source without an external agent:

```bash
pip install 'brandman[anthropic]'
export ANTHROPIC_API_KEY=…
brandman draft --brand my-brand --source <source-id>
```

That creates a draft campaign. It does not approve or publish it.

`brandman-worker` runs one scheduler tick and due jobs. Run it from cron, or
use the Compose file, which ticks every five minutes.

The `brand-os-*` commands, `BRAND_OS_*` variables, and the `brandos_session`
cookie still work. `brandman-*` and `BRANDMAN_*` are the primary names.

## Documentation

- [Getting started](https://github.com/AustinNChristensen/brandman/blob/main/docs/getting-started.md)
- [MCP clients](https://github.com/AustinNChristensen/brandman/blob/main/docs/mcp.md)
- [Configuration](https://github.com/AustinNChristensen/brandman/blob/main/docs/configuration.md)
- [Seed packs](https://github.com/AustinNChristensen/brandman/blob/main/docs/seed-packs.md)
- [Deployment](https://github.com/AustinNChristensen/brandman/blob/main/docs/deployment.md)
- [Extending BrandMan](https://github.com/AustinNChristensen/brandman/blob/main/docs/extending.md)

For a host that serves a checkout, build the dashboard before starting the
API. A wheel install already has those assets. Keep the database on durable
storage. Set `BRANDMAN_ALLOWED_HOSTS` and `BRANDMAN_REQUIRE_HTTPS=true` behind
TLS. Do not commit `.env` or a database file.

## Development

```bash
uv sync --locked
uv run python scripts/build_dashboard.py
uv run pytest
```

Frontend: `cd web && npm ci && npm test && npm run build`.

## Contributing

See [CONTRIBUTING.md](https://github.com/AustinNChristensen/brandman/blob/main/CONTRIBUTING.md).
Contributors sign a CLA. Contributions are released under AGPL-3.0.

## License

Copyright (C) 2026 Austin Christensen.

[GNU Affero General Public License v3.0](https://github.com/AustinNChristensen/brandman/blob/main/LICENSE).
The open core is not paywalled. If you run a modified version as a network
service, you must offer its source to the users of that service.
