# Getting started

This takes about five minutes and ends with your own brand in BrandMan, the
dashboard open, and an AI agent connected to it over MCP. BrandMan is free and
open source (AGPL-3.0). [BrandMan Cloud](https://usebrandman.com) is the hosted
paid edition; these steps are for the self-hosted package.

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

On a terminal it prompts. Without a terminal, and also with `-y`, any field
you omit gets a default: the directory name as the brand name, and
"Help the right people understand and choose this brand." as the mission.
Replace that mission. Pass `--name`, `--mission`, `--voice` and `--compliance`
to set them explicitly. Add `--with-demo` to also load the two sample brands.

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

`brandman mcp` starts the stdio server. `brandman-mcp` remains the same
server. `mcp-config` prints a command the agent can keep. A normal install
uses the `brandman-mcp` entry point on your `PATH`. Running the command
through `uvx` prints `uvx brandman mcp` instead of a path inside the uv
cache, which would break the next time the cache is cleared. `uvx brandman`
without `mcp` starts the CLI, not the server.

Then ask your agent something like *"Read the brand context for my-brand and
draft three X posts about our launch."* `draft_post` saves canonical campaign
drafts. It does not submit them for approval. In the dashboard, open
**Content → Campaign posts → Create review draft**, then **Submit for review**.
You can skip the agent: **Content → New X draft → Save & submit for review**
needs no API key. Approve that exact revision under **Approvals**. **Execution**
shows a pending handoff for X, or for Beehiiv after the same approval on a
newsletter issue. Nothing is published automatically. See
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
Without the extra, or without `ANTHROPIC_API_KEY`, the command says so even
if the source id is wrong. Create the source first (the dashboard's Sources
tab, or `POST /api/brands/{slug}/sources`) and pass its id.

## 6. Keep it running

`brandman-worker` runs one bounded scheduler tick and processes due jobs
(connector reads, measurement windows, plan refreshes). Run it from cron or a
process supervisor, e.g. every five minutes. `brand-os-worker` still runs and
prints a deprecation warning. Or use Docker: see [Deployment](deployment.md).
