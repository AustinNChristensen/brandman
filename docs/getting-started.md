# Getting started

This takes about five minutes and ends with your own brand in BrandMan, the
dashboard open, and an AI agent connected to it over MCP.

## 1. Install

With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install brandman            # or: pipx install brandman
```

For a source checkout, install Node.js 22.12+ and npm, run `uv sync --locked`
in the repository, then `uv run python scripts/build_dashboard.py`. Prefix
commands with `uv run`. Prebuilt wheels include the dashboard and need no Node
at runtime.

## 2. Create your brand

```bash
mkdir my-brand && cd my-brand
brandman init
```

`init` asks for your brand's name, mission, voice and compliance rules, then:

- writes `.env` (mode 600) with a generated sign-in password and a
  credential-encryption key;
- creates `brandman.db` and your brand.

Pass `--name`, `--mission`, `--voice`, `--compliance` and `-y` to run it
without prompts. Add `--with-demo` to also load the two sample brands.

## 3. Open the dashboard

```bash
brandman serve
```

Open <http://127.0.0.1:8000/app> and sign in with `BRANDMAN_PREVIEW_PASSWORD`
from `.env`. You can add more brands under **Settings**.

## 4. Connect an agent

```bash
brandman mcp-config --client claude-code    # prints a `claude mcp add …` command
brandman mcp-config --client claude-desktop # prints JSON for claude_desktop_config.json
```

Then ask your agent something like *"Read the brand context for my-brand and
draft three X posts about our launch."* It reads the brand's mission, voice,
rules, personas and accepted learnings, and saves drafts. You approve the
exact revision in the dashboard; nothing is published automatically. See
[MCP clients](mcp.md) for details.

## 5. Optional: built-in drafting

If you would rather not wire up an agent, install the Anthropic extra and set
an API key:

```bash
uv tool install 'brandman[anthropic]'
export ANTHROPIC_API_KEY=…
brandman draft --brand my-brand --source <source-id>   # a source added in the dashboard (Sources)
```

This turns one source into a draft campaign. It never approves or publishes.

## 6. Keep it running

`brandman-worker` runs one bounded scheduler tick and processes due jobs
(connector reads, measurement windows, plan refreshes). Run it from cron or a
process supervisor, e.g. every five minutes. Or use Docker: see
[Deployment](deployment.md).
