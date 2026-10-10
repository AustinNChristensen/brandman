# Deployment

## Docker Compose (single machine)

```bash
cp .env.example .env     # set BRANDMAN_PREVIEW_PASSWORD (16+ characters)
docker compose up -d
```

Opens on <http://localhost:8000/app>. Compose runs the web app and a worker
that ticks every five minutes, sharing the `brandman-data` volume. The port is
published on the host's loopback only.

## Behind a TLS proxy

- Set `BRANDMAN_ALLOWED_HOSTS=brand.example.com` and
  `BRANDMAN_REQUIRE_HTTPS=true`.
- Run uvicorn with `--proxy-headers` and set `FORWARDED_ALLOW_IPS` to the
  proxy's address so the original scheme is visible. Never use `*`.
- Keep the raw app port private.
- Remove `BRANDMAN_TRUSTED_LOCAL_NETWORKS` (it is only for loopback-bound
  local Compose).

## Data

The database is a single SQLite file. Put it on a durable volume, back it up
(for example with [Litestream](https://litestream.io/) or a nightly
`sqlite3 brandman.db ".backup …"`), and never bake it into an image. Run one
web process per database; the worker can run alongside it.

## Images and packages

Tagged releases publish `ghcr.io/austinnchristensen/brandman:<version>` and
the `brandman` package on PyPI.

Build source packages with `uv build` on a machine with Node.js 22.12+ and npm.
The wheel includes the dashboard; its runtime requires only Python. Docker builds
the same dashboard and package in a build stage and leaves Node out of the
runtime image. For a direct source deployment, run
`uv run python scripts/build_dashboard.py` before restarting the service.
