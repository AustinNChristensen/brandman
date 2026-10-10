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

## Quickstart

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

> **Legacy aliases.** The product is BrandMan. For compatibility with existing
> installs, the `brand-os-*` commands, `BRAND_OS_*` environment variables and the
> `brandos_session` cookie name still work; the `brandman-*` commands are the
> primary names.

```bash
git clone https://github.com/AustinNChristensen/brandman.git
cd brandman && uv sync

uv run brandman init        # asks for your brand; writes .env and the database
uv run brandman serve       # http://127.0.0.1:8000/app (password is in .env)
uv run brandman mcp-config --client claude-code   # connect an agent
```

Or with Docker: `cp .env.example .env`, set `BRANDMAN_PREVIEW_PASSWORD`, then
`docker compose up -d`.

Walkthrough: [docs/getting-started.md](docs/getting-started.md).

## Documentation

- [Getting started](docs/getting-started.md)
- [Connecting MCP clients](docs/mcp.md): Claude Code, Claude Desktop, Cursor, hosted OAuth
- [Configuration](docs/configuration.md): every `BRANDMAN_*` setting
- [Seed packs](docs/seed-packs.md): start a database with your own brands and house style
- [Deployment](docs/deployment.md): Docker, TLS proxies, backups
- [Extending BrandMan](docs/extending.md): plugins, custom authentication, one database per workspace

## Development

```bash
uv sync && uv run pytest
cd web && npm ci && npm test && npm run build   # rebuilds brandman/static/app
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Contributors sign a CLA.

## License

Copyright (C) 2026 Austin Christensen.

[GNU Affero General Public License v3.0](LICENSE). If you run a modified
version as a network service, you must offer its source to the users of that
service. The BrandMan name and logo are not covered by the license.
