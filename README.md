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

- **BrandMan (this repository):** free and open source under AGPL-3.0. Run the
  whole thing yourself against a local SQLite database. The PyPI package is
  `brandman`.
- **[BrandMan Cloud](https://usebrandman.com):** the hosted, paid, multi-workspace
  edition. It is this core plus separate closed components (billing,
  multi-tenancy, and similar). Not part of this repository.

BrandOS / brand-os is retired as a product name. Older command and setting
names still run; see below.

## Quickstart

Requirements for a source checkout: Python 3.11+, [uv](https://docs.astral.sh/uv/), and Node.js 22.12+ with npm.

> **Deprecated names.** For existing installs, the `brand-os-*` commands and
> `BRAND_OS_*` environment variables still work and warn on use. The canonical
> names are `brandman-*` and `BRANDMAN_*`. A `BRANDMAN_*` value wins when both
> are set. The session cookie stays `brandos_session` so a browser that is
> already signed in stays signed in.

```bash
git clone https://github.com/AustinNChristensen/brandman.git
cd brandman
uv sync --locked
uv run python scripts/build_dashboard.py

uv run brandman init        # asks for your brand; writes .env and the database
uv run brandman serve       # http://127.0.0.1:8000/app (password is in .env)
uv run brandman mcp-config --client claude-code   # connect an agent
```

Or with Docker: `cp .env.example .env`, set `BRANDMAN_PREVIEW_PASSWORD`, then
`docker compose up -d`.

The source HTML (`web/index.html`) and React code are tracked; generated files in
`brandman/static/app/` are ignored. After changing dashboard source, rerun
`uv run python scripts/build_dashboard.py` before serving it or running Python
HTTP tests. `npm run dev` in `web/` remains available for frontend development.

For a release or deployment, run `uv build` with Node.js available. The wheel
build hook installs the locked frontend dependencies and builds the dashboard,
then includes all generated assets in the wheel. Install that wheel in the
runtime environment; Node.js is only needed by the build machine. Source archives
include the frontend sources and build hook, and rebuild the assets when converted
to a wheel. Editable installs do not run frontend builds automatically.

Builders without Node/npm must install the verified prebuilt wheel, with
`--only-binary=:all:` and an exact version/hash; a git dependency or source
archive still invokes the frontend build hook. A wheel is a Python package,
not a complete hosting upload configuration. See
[release wheel preparation](docs/release-wheel.md) for the runtime proof,
artifact receipts and operator gates before selecting a published version.

Production notes:

- A host serving directly from a checkout must run
  `uv run python scripts/build_dashboard.py` after checkout/dependency installation
  and before starting or restarting the API. A host installing the built wheel
  already has the assets. CI verifies both paths.

- Configure every value through your host's secret manager; `.env.example` is
  the non-secret contract. Never commit credentials or a database file.
- Set `BRANDMAN_ALLOWED_HOSTS` to your public hostname and keep
  `BRANDMAN_REQUIRE_HTTPS=true` behind TLS. `BRAND_OS_ALLOWED_HOSTS` and
  `BRAND_OS_REQUIRE_HTTPS` still work and warn on use.
- The database must be durable. Do not ship a local SQLite file inside a
  container image.
- OAuth state for the hosted MCP endpoint is stored in the same database.
- The built-in password rate limit on `/oauth/authorize` is in memory and per
  process.

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
