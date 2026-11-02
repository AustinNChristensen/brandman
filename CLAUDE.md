# CLAUDE.md

@AGENTS.md

`AGENTS.md` is the source of truth for this repo: layout, setup, tests, and
safety rules. Keep repo guidance there, not here.

Claude-specific notes:

- To try the MCP server from Claude Code against a scratch database, use
  `brandman mcp-config --client claude-code` and point `BRANDMAN_DB` at a
  file outside the checkout.
- Ask before running anything that tags a release or publishes a package.
