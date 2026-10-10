# Brand OS MVP technical scope

## Outcome

A trusted, portable brand context and content action layer that a brand team can use from a dashboard or any MCP-compatible agent.

## V1 flows

1. Select a brand → retrieve its mission, voice, compliance policy and personas.
2. Add an external/manual content source → create a campaign → draft channel posts.
3. Explicitly approve an exact revision → create a governed delivery handoff.
   Beehiiv is draft-only; X may be posted by a configured browser/MCP operator,
   then reconciled from a provider-specific receipt. Nothing auto-sends.
4. An agent performs the same flows through MCP rather than a browser.
5. Record channel outcomes → propose an evidence-backed learning → explicitly
   accept it into canonical brand context.
6. Report missing Brand OS capabilities discovered during live brand operations.

## Data model

`brands → personas`, `brands → sources`, `brands → campaigns`, and
`campaigns → posts`. Post lifecycle is strictly `draft → approved → scheduled`.
Brands also own performance records, proposed/accepted learnings, and product
feedback. Learning lifecycle is strictly `proposed → accepted`; unreviewed
proposals never alter agent context.
First-party website ingestion additionally stores normalized authenticated-session,
tool-use, conversion, and deployment-change observations. Session identifiers are
one-way hashed. Exact funnel attribution is accepted only when runtime brand,
campaign, active asset membership, tracked link, CTA, and source match; explicitly
unattributed observations retain null campaign fields. A complete provider page is
validated before any connector event, projection, or cursor change. Website
provider event IDs are immutable account-wide identities across polling streams;
same-material replays are idempotent and conflicting rebindings fail atomically.
Platform credentials, when optional API mode is used, live only in the encrypted
credential store and are never returned through content, REST, MCP, or logs.

The preview uses an environment-only HTTP Basic password and fails closed if it
is absent. Its HTTP boundary defaults to loopback-only, rejects unlisted Host
headers, requires HTTPS for non-loopback requests, rejects cross-site browser
mutations using Origin/Referer and Fetch Metadata, disables response caching, and
sets CSP/frame/referrer/MIME/cross-origin browser protections on every response,
including authentication failures. Remote configuration requires an explicit
hostname allowlist, HTTPS enforcement, and a non-placeholder password of at least
16 characters. Production will replace this single-user boundary with workspace
auth/RBAC.

## Architecture

FastAPI and SQLite (replaceable with Postgres) own canonical state and the policy
gate. A small dashboard consumes REST. `brandman/mcp_server.py` exposes governed agent
tools. `brandman/workflows.py` is LangChain provider-neutral and requires structured
output. n8n initiates connector events but is never the system of record.
FastAPI and MCP imports are database-inert. Their runtime composition boundary
requires an explicit database path/profile, validates the persisted profile
before schema writes, and only then constructs database-backed services.

## Cut list / next increment

- prove one real Beehiiv draft and one real approved X browser handoff
- configure the production website-analytics endpoint
- deploy/supervise the bounded worker and complete a retry/lease/measurement soak
- close the source → newsletter → distribution-package composition gap
- feed explicitly accepted experiment learnings into planning; experiment-owned
  measurement/review windows are now durable and restart-safe
- signed webhook ingestion, assets/storage, RBAC, and production Postgres migration

## Acceptance checks

- two seeded brand workspaces load via REST and MCP
- a schedule attempt for a draft returns a conflict
- an approved post can be scheduled
- no unapproved revision can be handed off or published; global and provider
  delivery disables prevent new assisted-execution claims
