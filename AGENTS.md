# AGENTS.md: BrandMan

This is the guide for coding agents (Claude Code, Codex, Cursor, and others)
working in this repository. `CLAUDE.md` imports this file. If something here
disagrees with the code, the code is right: fix this file in the same PR.

## What this repo is

BrandMan is the AGPL-3.0 open core of an agent-first brand brain: brand
context, sources, campaigns, newsletters, exact-revision approvals,
connectors (X, Beehiiv), feedback and missions, a REST API, a React
dashboard, and an MCP server. It is published on PyPI as
[`brandman`](https://pypi.org/project/brandman/) and as a container image.
One SQLite database per install. [BrandMan Cloud](https://usebrandman.com) is
a separate hosted product built on this package through its public extension
points; its code is not here.

## Layout

| Path | What is there |
| --- | --- |
| `brandman/main.py` | FastAPI app (`brandman.main:app`), auth gate, app composition |
| `brandman/routes/` | REST routes by area: brands, content, dispatch, newsletters, connections, feedback, performance, execution, orchestration, engagement |
| `brandman/store.py` | SQLite schema and data access; `using_database()` binds a context to one file |
| `brandman/mcp_server.py`, `brandman/hosted_mcp.py` | MCP tools over stdio (`brandman-mcp`) and the hosted `/mcp` OAuth endpoint |
| `brandman/approval_snapshots.py`, `dispatch.py`, `sqlite_dispatch.py`, `content_dispatch.py` | Exact-revision approval and the dispatch lifecycle (draft, awaiting approval, approved, queued, published) |
| `brandman/x_*.py`, `brandman/beehiiv_*.py`, `connectors.py` | Channel adapters and connector health |
| `brandman/cli.py` | `brandman` CLI (`init`, `serve`, `mcp-config`, `draft`, ...) |
| `brandman/worker_cli.py`, `scheduler.py`, `jobs.py` | `brandman-worker`: one scheduler tick plus due jobs |
| `brandman/extensions.py`, `seed_packs.py`, `brandman/seeds/` | Plugin, authenticator and seed-pack extension points |
| `brandman/drafting.py` | Optional built-in drafting (`brandman[anthropic]`) |
| `brandman/static/app/` | Generated dashboard build. Never commit it |
| `web/` | Dashboard source (React, Vite, TypeScript, Vitest, Playwright) |
| `scripts/` | `build_dashboard.py`, `seed_dev.py`, `run_dashboard_e2e.py`, wheel and package checks |
| `tests/` | Pytest suite. `conftest.py` forces scratch databases |
| `docs/` | User docs: getting started, MCP, configuration, seed packs, deployment, extending, releasing |
| `deploy/`, `Dockerfile`, `compose.yml` | Container and wrapper deployment examples |

## Setup

Requires [uv](https://docs.astral.sh/uv/), Python 3.11+, and Node.js 22.12+
with npm (for the dashboard).

```bash
uv sync --locked
uv run python scripts/build_dashboard.py
uv run brandman init      # writes .env and brandman.db in the current directory
uv run brandman serve     # http://127.0.0.1:8000/app
```

Run `brandman init` in a scratch directory, not the repo root, so no `.env`
or database lands in the checkout.

## Tests and lint

```bash
uv run pytest -q                               # Python suite, about 2 minutes
uv run pytest -q tests/test_dispatch.py -k x   # one test
cd web && npm ci && npm run lint && npm test && npm run build
cd web && npm run test:e2e                     # optional: Playwright
```

- The Python suite does not need a built dashboard (`conftest.py` sets
  `BRANDMAN_REQUIRE_DASHBOARD=false`) and always uses scratch databases.
- `npm run lint` is oxlint. No Python linter or formatter is configured; match
  the surrounding style (type hints, small functions, docstrings).
- `tests/test_project_metadata.py` checks README phrases and the package
  version. If you edit the README, keep those phrases or update the test on
  purpose.
- Run checks locally before opening a PR. Do not rely on CI alone; it may be
  unavailable.

## Releases

Releases are cut by the maintainer from a `v*` tag (see
`docs/releasing.md`). Do not bump the version, push tags, or publish to PyPI
or GHCR unless the issue asks for it.

## Safety rules

- **Never commit secrets** (`.env`, API keys, tokens, Fernet keys), database
  files, or real customer data. Do not print secret values in logs, tests or
  PR text.
- **Approval is the gate.** A channel adapter publishes only a revision with
  a recorded approval for that exact revision. Editing an approved item clears
  the approval, bumps the revision, and returns it to draft. Keep that
  invariant. Who approves is the operator's choice (a person in the
  dashboard, or their own automation through the REST API), and a brand's
  `approval_policy` is a setting. The MCP server has no approve or publish
  tool; do not add one without an issue that asks for it.
- Tests must never call a real channel API or post anything.
- Keep the extension points (`set_authenticator`, `using_database`, plugins,
  seed packs) backward compatible; BrandMan Cloud and other integrators
  depend on them. Keep the `brand-os-*` and `BRAND_OS_*` aliases working.
- Database changes must be additive and safe on an existing database.

## Workflow

1. Every task starts from a GitHub issue. If there is none, open one first.
2. Branch from `main`: `<type>/<issue>-<short-slug>`, for example
   `fix/40-x-retry` or `docs/35-agents-guide`.
3. Keep PRs small: one issue, one concern.
4. The PR body says `Closes #<issue>`, what changed, why, and how you tested
   it. Use the PR template.
5. Squash-merge after review.

- **Commit without signing.** Agents have no GPG or SSH signing key. If a
  commit asks for a signing key or passphrase, or fails with a signing error,
  commit unsigned with `git commit --no-gpg-sign` (or
  `git -c commit.gpgsign=false commit`) and keep going. Do not create, import
  or ask for a key, and do not change global git config.

See `CONTRIBUTING.md` for commit style and the CLA.
