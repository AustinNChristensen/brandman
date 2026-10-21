# Deployment

This is self-hosted BrandMan, the free open-source edition (AGPL-3.0).
[BrandMan Cloud](https://usebrandman.com) is the hosted paid edition and is not
deployed from this repository.

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
runtime image. ## Source deployments and the dashboard build

The built dashboard (`brandman/static/app`) is generated, not tracked in git.
The app checks for it at **startup**:

- Source checkout with Node.js 22.12+ and npm available: the app builds the
  dashboard itself on first start (it can take a minute), so a fresh clone needs
  no manual step. Prefer running `uv run python scripts/build_dashboard.py` in the
  deploy/build step so the build is not part of serving traffic.
- Dashboard missing and no way to build it (no Node, no `web/`, a failed build, or
  `BRANDMAN_AUTO_BUILD_DASHBOARD=false`): the process exits non-zero with
  "BrandMan dashboard build is missing". A deploy health check fails; users never
  see a 503 at `/app`.
- API-only deployments can set `BRANDMAN_REQUIRE_DASHBOARD=false`; startup then
  logs a warning and `/app` returns 503.

The wheel and Docker image already contain the dashboard and never need Node at
runtime. For FastAPI Cloud, use the [standalone locked wheel wrapper](fastapi-cloud-wrapper.md);
do not assume a configurable Node build command or bare-wheel app upload. Other
source platforms with Node can run `python scripts/build_dashboard.py` before
startup. CI's `clean-checkout` job proves the source build/startup paths.
