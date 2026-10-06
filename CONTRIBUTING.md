# Contributing to BrandMan

Thanks for helping. Bug reports, fixes, docs and adapters are all welcome.

## Before you start

- For anything bigger than a small fix, open an issue first so we can agree on the approach.
- Security problems: do not open a public issue. Contact the maintainers privately through the repository's security advisory page.

## Development

```bash
uv sync
uv run pytest
```

- Keep changes focused and add or update tests with them.
- Never commit credentials, database files or real customer data. Tests must use scratch databases only.
- Nothing may publish to an external channel without an explicit approval step; keep that invariant.

## Contributor License Agreement

BrandMan is licensed under AGPL-3.0. The project owner also offers the code under
other terms for the managed product, so every contributor must sign a CLA
before a pull request can be merged. We use the
[CLA Assistant](https://github.com/apps/cla-assistant) GitHub app: it comments on
your first pull request with a link to sign, once, with your GitHub account.
Your contribution is still released to everyone under AGPL-3.0.

## Pull requests

- Describe what changed and why, and how you tested it.
- CI must pass.
- Be kind in review. Disagree with the idea, not the person.
