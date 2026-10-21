# Good first issues

These are starter tasks for the open-source repository. They are not GitHub
issues yet. Open one only after reading the scope below, and keep the pull
request inside that scope.

Each task is one pull request, with a test. Do not publish a release, and do
not add a model SDK to the core install.

## 1. `brandman mcp-config --client codex`

**Why.** The README tells Codex users to paste a TOML block by hand. The CLI
already prints Claude Code, Claude Desktop, and Cursor config, and it already
refuses to embed a uv cache path. Codex should get the same treatment.

**Scope.** `brandman/cli.py` (`--client` choices and `mcp_config`), `tests/test_cli.py`, and the Codex section of `docs/mcp.md` and `README.md`.

**Out of scope.** Hosted OAuth, a new MCP tool, or writing the user's `config.toml` for them. Print the block. Do not touch `~/.codex/`.

**Done when.**

- `brandman mcp-config --client codex` prints a `[mcp_servers.brandman]` table whose `command` is the installed `brandman-mcp`, or `uvx` with args `--from`, `brandman`, `brandman-mcp` when that entry point is inside a uv cache.
- The printed `BRANDMAN_DB` is the database path passed in, and `BRANDMAN_DATABASE_PROFILE` is `operating`.
- A test covers both the persistent entry point and the uv-cache fallback, and asserts the output does not contain `.cache/uv`.
- `docs/mcp.md` shows the command instead of only a hand-written example.

## 2. Put the 60-second install on the public homepage

**Why.** `README.md` starts with `uvx --from brandman brandman init`. The page served at `/` (`brandman/static/index.html`) still only links to `/app`. Someone who opens the running server never sees the install command.

**Scope.** `brandman/static/index.html` and `tests/test_http_security.py` (`test_homepage_console_figures_are_labeled_illustrative` or a sibling test).

**Out of scope.** The React dashboard, BrandMan Cloud, and pricing. Keep the existing "Demo data" label on the sample console figures.

**Done when.**

- The homepage shows `uvx --from brandman brandman init` as copy-pasteable text, plus a link to this repository.
- The sample console still says it is demo data and not live data.
- A test loads `GET /` and asserts the command is present, and that the preview password is not.

## 3. Mark sample brands in the API

**Why.** The dashboard treats `demo-brand`, `demo-personal`, `demo-fictional-studio`, and names starting with `[DEMO]` as sample data (`web/src/lib/demoData.ts`). A renamed sample brand, or a real brand that happens to use one of those slugs, gets the wrong label. The seed pack should say which brands are samples.

**Scope.** `brandman/seeds/demo.json`, the brand payload returned by brand list/detail (see `brandman/main.py` and `brandman/seed_packs.py`), `web/src/lib/demoData.ts`, and the dashboard banner in `web/src/components/Shell.tsx`.

**Out of scope.** Deleting the demo seed, inventing metrics, or labeling a brand the operator created themselves.

**Done when.**

- A seeded sample brand includes a boolean the client can trust, such as `sample: true`. Real brands created through the API omit it or send `false`.
- `isSampleBrand` uses that flag. The slug list remains only as a fallback for databases seeded before the flag existed.
- Tests cover a sample brand, a normal brand, and the Shell banner: visible on the sample brand, absent when only a normal brand is selected.
- The banner text still includes "Demo data".

## 4. Don't pre-fill the agent id

**Why.** `web/src/pages/Agents.tsx` starts the agent id at `demobrand-browser`. That looks like an agent BrandMan already registered. The field should be empty until the operator types an id.

**Scope.** `web/src/pages/Agents.tsx` and `web/tests/Agents.test.tsx`.

**Out of scope.** Changing how agents are stored, heartbeats, or execution controls.

**Done when.**

- The agent id input renders empty, with a placeholder that tells the operator to choose an id.
- Submitting with an empty id does not call configure (the page already toasts "Enter an agent ID first.").
- A test renders the page and asserts the input value is empty, then that a typed id is what `configure` receives.

## 5. Say which channels exist

**Why.** BrandMan moves drafts for X, Beehiiv, a website, and RSS. The README says that in passing. A short page should say what is implemented and what is not, so a newcomer does not start a LinkedIn connector by accident.

**Scope.** A new `docs/channels.md`, a link from `README.md`, and a test that the page names every `ConnectorKind` in `brandman/connectors.py` and says LinkedIn is not a connector.

**Out of scope.** Implementing a new connector, Buffer/Typefully handoff, or BrandMan Cloud's managed OAuth apps.

**Done when.**

- The page lists `beehiiv`, `x`, `website`, and `rss` in the same terms the enum uses, and states that scheduling other networks is not in this repository.
- It says nothing is posted automatically: approval still creates a handoff.
- The README links to the page.
- A test fails if `ConnectorKind` gains a value the page does not mention.
