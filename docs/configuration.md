# Configuration

All settings are environment variables. `brandman init` writes a `.env`;
`brandman serve` and `brandman mcp-config` read it. In production, use your
host's secret manager. Canonical names use the `BRANDMAN_` prefix. The older
`BRAND_OS_*` names (from the retired BrandOS product name) are still accepted
and print a deprecation warning; a `BRANDMAN_*` value always wins.

| Variable | Default | Purpose |
| --- | --- | --- |
| `BRANDMAN_DB` | (required) | SQLite database path. Keep it on durable storage. |
| `BRANDMAN_DATABASE_PROFILE` | (required) | `operating`, `development`, `test` or `proof`. Checked against the database before any write. |
| `BRANDMAN_PREVIEW_PASSWORD` | (required) | Sign-in password for the dashboard and API. At least 16 characters for non-loopback access. |
| `BRANDMAN_BASIC_USER` | `operator` | HTTP Basic user name for API clients. |
| `BRANDMAN_ACCESS_REQUEST_EMAIL` | none | Contact address for the login page's access-request panel, rendered as a `mailto:` link. When unset or not a valid email, the panel says there is no shared demo sign-in and offers no link. |
| `BRANDMAN_OPERATOR` | `operator` | Principal recorded on approvals and audits; also the only principal allowed owner-only actions. |
| `BRANDMAN_ALLOWED_HOSTS` | loopback only | Comma-separated public host names. |
| `BRANDMAN_REQUIRE_HTTPS` | `false` locally | Reject plain HTTP. Remote hosts always require HTTPS. |
| `BRANDMAN_TRUSTED_LOCAL_NETWORKS` | none | Private CIDRs treated as the local machine (Docker bridge). Only with a loopback-bound published port. |
| `BRANDMAN_CREDENTIAL_MASTER_KEY` | none | Fernet key encrypting stored connector credentials. Needed for native API connectors. |
| `BRANDMAN_SEED_PACK` | `demo` | What a new, empty database starts with: `demo`, `none`, a JSON path, or a registered pack. See [Seed packs](seed-packs.md). |
| `BRANDMAN_CLAIM_UNITS` | from seed pack | Units that mark a volatile numeric claim for fact-checking, e.g. `credits,seats`. |
| `BRANDMAN_WORKER_MODE` | `auto` | `auto`, `assisted_secretless` or `native_api`. |
| `BRANDMAN_WORKER_MAX_JOBS` | `100` | Jobs one worker run may process (1–1000). |
| `BRANDMAN_SCHEDULER_MAX_DECISIONS` | `50` | Due schedules one tick may enqueue (1–500). |
| `BRANDMAN_MCP_STATELESS` | `false` | Bind each hosted MCP request to its own HTTP request. Required when one process serves several databases. |
| `BRANDMAN_DISABLE_PLUGINS` | `false` | Skip installed `brandman.plugins`. |
| `BRANDMAN_API_TOKEN` | none | Personal bearer token for scripts and self-hosted `/mcp`. At least 32 characters. `brandman init` generates one. A short or example value is ignored. Not a BrandMan Cloud workspace token. |
| `BRANDMAN_DRAFT_PROVIDER` | auto | `anthropic`, `openai` or `none`. Unset: Anthropic when `ANTHROPIC_API_KEY` is set, otherwise an OpenAI-compatible endpoint when a key or `BRANDMAN_DRAFT_BASE_URL` is set, otherwise no model (an MCP agent drafts). |
| `BRANDMAN_DRAFT_MODEL` | provider default | Model id. Anthropic default `claude-opus-5-5`. OpenAI-compatible default `gpt-4.1`. |
| `BRANDMAN_DRAFT_BASE_URL` | provider default | OpenAI-compatible base URL (OpenAI, xAI, OpenRouter, Ollama, vLLM). |
| `BRANDMAN_DRAFT_API_KEY` | none | Key for an OpenAI-compatible endpoint. `OPENAI_API_KEY` is used when this is unset. A base URL with no key uses a dummy local key. |
| `ANTHROPIC_API_KEY` | none | Key for optional Anthropic drafting (`brandman[anthropic]`). |
| `OPENAI_API_KEY` | none | Key for optional OpenAI-compatible drafting (`brandman[openai]`). |
