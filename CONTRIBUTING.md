# Contributing to BrandMan

Thanks for helping. Bug reports, fixes, docs and adapters are all welcome.
Coding agents should also read [AGENTS.md](AGENTS.md).

## Start from an issue

- Every change starts from a GitHub issue. For a small fix, open the issue and
  the pull request together. For anything bigger, open the issue first so we
  can agree on the approach before you write code.
- Use the bug report or feature request template.
- Security problems: do not open a public issue. See [SECURITY.md](SECURITY.md).

## Development setup

Requires [uv](https://docs.astral.sh/uv/), Python 3.11+, and Node.js 22.12+
with npm.

```bash
git clone https://github.com/AustinNChristensen/brandman.git
cd brandman
uv sync --locked
uv run python scripts/build_dashboard.py
```

To try it, run `uv run brandman init` and `uv run brandman serve` in a
scratch directory. `scripts/seed_dev.py` fills a development database with
sample content.

## Tests

```bash
uv run pytest -q
cd web && npm ci && npm run lint && npm test && npm run build
```

- Add or update tests with every behavior change.
- Tests use scratch databases only (`tests/conftest.py` enforces this) and
  must never call a real channel API.
- Run the checks locally before you open a pull request and say what you ran
  in the PR.

## Rules

- Commit frontend sources under `web/`, never generated `brandman/static/app/`
  files.
- Never commit credentials, `.env` files, database files or real customer data.
- Nothing may publish to an external channel without a recorded approval of
  that exact revision; keep that invariant.
- Keep the extension points and the `brand-os-*` / `BRAND_OS_*` aliases
  backward compatible.

## Branches and pull requests

- Branch from `main`: `<type>/<issue>-<short-slug>`, where type is `fix`,
  `feat`, `docs`, `refactor`, `test` or `chore`. Example: `fix/40-x-retry`.
- One issue per pull request. Keep it small; split refactors from behavior
  changes.
- The PR description says `Closes #<issue>`, what changed and why, and how
  you tested it. The PR template has the checklist.
- CI must pass when it runs. Pull requests are squash-merged.
- Be kind in review. Disagree with the idea, not the person.

## Commit style

- Subject in plain, imperative English, about 72 characters or less, saying
  what the change does. Optional issue prefix, for example
  `AUS-26: fail at startup when the dashboard build is missing`.
- The body, if any, explains why.
- Because of squash merges, the PR title becomes the commit on `main`. Make
  it read well.

## Contributor License Agreement

BrandMan is licensed under AGPL-3.0. The project owner also offers the code under
other terms for the managed product, so every contributor must sign a CLA
before a pull request can be merged. We use the
[CLA Assistant](https://github.com/apps/cla-assistant) GitHub app: it comments on
your first pull request with a link to sign, once, with your GitHub account.
Your contribution is still released to everyone under AGPL-3.0.
