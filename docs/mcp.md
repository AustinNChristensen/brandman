# Connecting MCP clients

BrandMan exposes the same governed operations to agents that the dashboard
uses: read brand context, record sources, create and revise drafts and
campaigns, record outcomes, propose learnings. **Approving, scheduling and
publishing stay with a human.**

## Local (stdio)

The agent starts `brandman-mcp` itself and talks to your database directly.

```bash
brandman mcp-config --client claude-code
# claude mcp add brandman -e BRANDMAN_DB=/…/brandman.db -e BRANDMAN_DATABASE_PROFILE=operating -- /…/brandman-mcp
```

```bash
brandman mcp-config --client claude-desktop   # or --client cursor
```

prints the JSON block to merge into the client's MCP configuration
(`claude_desktop_config.json`, `.cursor/mcp.json`).

## Hosted (Streamable HTTP + OAuth)

A deployed BrandMan serves MCP at `https://your-host/mcp`, protected by
OAuth 2.1 with PKCE and dynamic client registration.

```bash
brandman mcp-config --client claude-code --url https://your-host
# claude mcp add --transport http brandman https://your-host/mcp
```

The client opens a browser sign-in. The consent screen lists what the client
may do. Tokens are short-lived and can be revoked.

## Useful first tools

| Tool | What it does |
| --- | --- |
| `list_brands` | Brands this database holds |
| `get_brand_context` | Mission, voice, rules, personas and accepted learnings |
| `create_editorial_candidate` | Record an idea worth writing about |
| `create_campaign`, `draft_post` | Draft a campaign and its posts |
| `create_newsletter_issue`, `revise_newsletter_issue` | Draft and revise a newsletter issue |
| `get_active_mission` | Current goals, progress and required pace |
| `report_product_gap` | Tell the operator BrandMan is missing something |

Run `brandman-mcp` under an MCP inspector to see the full list.
