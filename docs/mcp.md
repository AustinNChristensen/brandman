# Connecting MCP clients

BrandMan exposes the same governed operations to agents that the dashboard
uses: read brand context, record sources, create and revise drafts and
campaigns, record outcomes, propose learnings. **Approving, scheduling and
publishing stay with a human.**

## Local (stdio)

The agent starts the server itself and talks to your database directly.
`brandman mcp` and `brandman-mcp` are the same server. `brand-os-mcp` is a
deprecated alias and warns on use. `uvx brandman` starts the CLI, so the
server launch is `uvx brandman mcp`.

```bash
brandman mcp-config --client claude-code
# claude mcp add brandman -e BRANDMAN_DB=/…/brandman.db -e BRANDMAN_DATABASE_PROFILE=operating -- uvx brandman mcp
```

```bash
brandman mcp-config --client claude-desktop   # or --client cursor
```

prints the JSON block to merge into the client's MCP configuration
(`claude_desktop_config.json`, `.cursor/mcp.json`).

The stdio command is the installed `brandman-mcp` when that entry point is on
`PATH` outside a uv cache. `uvx brandman mcp-config` prints
`uvx brandman mcp`, which resolves the package again on each launch. A path
under `~/.cache/uv/` is not written into the client config.

## Hosted (Streamable HTTP + OAuth)

A deployed BrandMan serves MCP at `https://your-host/mcp`, protected by
OAuth 2.1 with PKCE and dynamic client registration.

```bash
brandman mcp-config --client claude-code --url https://your-host
# claude mcp add --transport http brandman https://your-host/mcp
```

The client opens a browser sign-in. The consent screen lists what the client
may do. Tokens are short-lived and can be revoked.

## Self-hosted HTTP with a personal token

On a server you run, `brandman init` writes `BRANDMAN_API_TOKEN`. Send it as
`Authorization: Bearer` to the REST API and to `/mcp`. The preview password
must still be configured; the token does not replace it. A bearer value that
is not this token still uses OAuth on `/mcp` and is rejected on the REST API.
Short and example tokens never match. This is one operator's token. BrandMan
Cloud workspace tokens stay in Cloud.

## Useful first tools

| Tool | What it does |
| --- | --- |
| `list_brands` | Brands this database holds |
| `get_brand_context` | Mission, voice, rules, personas and accepted learnings |
| `create_editorial_candidate` | Record an idea worth writing about |
| `create_campaign`, `draft_post` | Save a campaign and canonical drafts. They are not submitted for approval |
| `create_newsletter_issue`, `revise_newsletter_issue` | Draft and revise a newsletter issue |
| `get_active_mission` | Current goals, progress and required pace |
| `report_product_gap` | Tell the operator BrandMan is missing something |

Run `brandman mcp` (or `brandman-mcp`) under an MCP inspector to see the full list.

## MCP Registry

`server.json` is the manifest for the official MCP Registry. Its name is
`io.github.AustinNChristensen/brandman`, and the same string is in this
repository's README as `mcp-name:` so PyPI's long description can prove
ownership. The registry compares that name to the GitHub login **with the
same letter case**. Publish the package to PyPI before registering it; the
registry reads the README from the published version. Registering is a
separate owner step (`mcp-publisher login github`, then `mcp-publisher publish`)
and is not part of the PyPI release workflow. The manifest runs
`uvx brandman mcp`, not `uvx brandman`, because the package command is the CLI.
