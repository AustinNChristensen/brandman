# BrandMan

BrandMan is an agent-first brand brain. It keeps a brand's mission, voice,
compliance rules, audience personas, content sources, campaigns and results in
one governed place, and lets AI agents (through MCP) and people (through a
dashboard) work from that same source of truth.

Channels are replaceable adapters. X, a newsletter platform or your own site
sit at the edge: BrandMan drafts for them, records what happened, and learns
from the results. The brand context itself never lives inside a channel.

> Status: early. The core runs and is tested, but expect rough edges and
> breaking changes. Nothing is published to a channel automatically; posting
> is handed off for explicit approval.

## What is in the core

- **Brand context:** mission, voice, guidelines, personas and policy per brand,
  retrievable by agents and humans.
- **Content workflow:** sources, campaigns, channel drafts, exact-revision
  approval, and a governed delivery handoff.
- **Feedback loop:** record channel outcomes, propose evidence-backed learnings,
  and accept them into the brand context only on explicit approval.
- **KPI missions:** time-boxed goals with baselines, targets and trajectory,
  fed by connector observations.
- **MCP server:** the same flows exposed to any MCP-compatible agent, over stdio
  or a hosted OAuth-protected HTTP endpoint.
- **Dashboard and REST API:** an operator UI and the API behind it.

## Two ways to run it

- **Self-hosted (this repository):** run the whole thing yourself against a local
  SQLite database. This repository is licensed AGPL-3.0.
- **Managed:** a hosted offering built on this core plus separate closed
  components (billing, multi-tenancy and similar). Not part of this repository.

On first start the app seeds a sample brand (`demo-brand`) and a second one (`demo-personal`) so there is something to click through. Replace them with your own.

## Quickstart (self-hosting)

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

> **Legacy aliases.** The product is BrandMan. For compatibility with existing
> installs, the `brand-os-*` commands, `BRAND_OS_*` environment variables and the
> `brandos_session` cookie name still work; the `brandman-*` commands are the
> primary names.

```bash
git clone https://github.com/AustinNChristensen/brandman.git
cd brandman
uv sync

# Copy .env.example for the full list of settings. A minimal local run:
export BRAND_OS_DB="$PWD/brand_os.db"
export BRAND_OS_DATABASE_PROFILE=development
export BRAND_OS_PREVIEW_PASSWORD="choose-a-long-random-password"
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. The home page is public; the dashboard, API and
login are behind the preview password. Run the test suite with `uv run pytest`.

Production notes:

- Configure every value through your host's secret manager; `.env.example` is
  the non-secret contract. Never commit credentials or a database file.
- Set `BRAND_OS_ALLOWED_HOSTS` to your public hostname and keep
  `BRAND_OS_REQUIRE_HTTPS=true` behind TLS.
- The database must be durable. Do not ship a local SQLite file inside a
  container image.
- OAuth state for the hosted MCP endpoint is stored in the same database.
- The built-in password rate limit on `/oauth/authorize` is in memory and per
  process.

## MCP setup

Local (stdio), for example with Claude Code:

```json
{
  "mcpServers": {
    "brandman": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/brandman", "brandman-mcp"],
      "env": { "BRAND_OS_DB": "/path/to/brand_os.db" }
    }
  }
}
```

Hosted endpoint: your deployment serves Streamable HTTP MCP at `/mcp`, protected
by OAuth 2.1 with PKCE and dynamic client registration. Point an MCP client at
`https://your-host/mcp` and complete the browser sign-in. The consent screen
says what the client may do: read brand data and create or edit drafts,
campaigns, records and connector jobs. Approving and publishing stay with a
human.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Contributors sign a CLA.

## License

Copyright (C) 2026 Austin Christensen.

[GNU Affero General Public License v3.0](LICENSE). If you run a modified
version as a network service, you must offer its source to the users of that
service. The BrandMan name and logo are not covered by the license.
