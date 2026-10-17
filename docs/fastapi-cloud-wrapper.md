# Standalone FastAPI Cloud wrapper (AUS-73)

This is a **local deployment preparation**, not authorization to deploy. No
FastAPI Cloud application, setting or live data was changed. AUS-34 must
reconcile the actual deployed source/data. AUS-52 owns durable storage and
backup/restore/rollback evidence. Production readiness is not established.

## Supported mechanism and artifact

Checked October 16, 2026 against FastAPI Cloud's official docs:

* [Dependencies](https://fastapicloud.com/docs/builds-and-deployments/install-dependencies/): pyproject.toml and uv.lock are supported; pin Python for reproducible application installs.
* [Existing projects](https://fastapicloud.com/docs/getting-started/existing-project/): source upload, dependency installation and a configured FastAPI entrypoint.
* [Application directory](https://fastapicloud.com/docs/builds-and-deployments/application-directory/): a subdirectory may be selected; workspaces may reuse a root lock.

Use a **standalone copy** of `deploy/fastapi-cloud/`, not the whole core repo,
so the root core project/build hook cannot be selected accidentally. This is
ordinary Python source wrapping an installed wheel. No claim is made that a
bare wheel upload or arbitrary dashboard build command is supported. The
wrapper has no build backend and `tool.uv.no-build=true` prevents dependency
source builds. `main:app` imports `brandman.main:app` from the installed wheel.
No generated dashboard assets are committed.

The wrapper pins Python 3.11 and `brandman[anthropic]==0.1.0`. Its independent
`uv.lock` records the transitive dependency versions/hashes. PyPI's live JSON
metadata was rechecked: `brandman-0.1.0-py3-none-any.whl`, not yanked, SHA256
`c09d09d3ed6d29a288ffed5d5ffaca44bf7aea51a3f5d6314b67636ba32e1ff2`.
Release source receipt: `340c63514f66e193a69a45a79e6745a3cc3957dd` (AUS-71
release proof is Done). The wrapper serves that published release, not every
later source-main change. Do not imply otherwise in the operator handoff.

## Repeatable local proof

From the reviewed checkout with uv and Python available:

```bash
python scripts/check_cloud_wrapper.py
```

It validates the published wheel hash/install policy, copies only the wrapper
and the shared smoke probe into a temporary directory, creates a fresh venv,
and installs the locked dependencies with cache/builds disabled. No application
configuration, secrets, index overrides or caller PATH are inherited. Python
3.11 must already be available to uv. The child has an empty executable PATH;
Node/npm/npx are absent from discovery, and an audit hook rejects frontend
startup attempts. The isolated interpreter imports from its installed wheel,
checks the wrapper exports that application, starts its lifespan on disposable
data, and tests unauthenticated 401, authenticated `/app`, deep-link HTML, every
referenced JS/CSS asset/MIME type and missing asset 404. The Claude extra imports
without an API key or provider call. This is an ASGI lifespan/HTTP probe, not
rendered UI or a FastAPI Cloud deployment test. CI runs the same proof.

The six focused regression cases reject relaxed versions, wheel hash drift,
source-build policy drift and entrypoint drift; an sdist-only fixture proves the build backend never executes. Required existing CI still
runs; no publisher or deployment workflow is added or changed.

## Operator handoff (not executed)

1. Review the exact wrapper commit and rerun its local proof. Export only its
   tracked wrapper files to a separate clean directory. Preserve the lockfile,
   Python requirement and no-build policy; never upload a `.venv`, `.env`, DB,
   core source tree or generated dashboard directory. Archive this exact
   artifact and record its checksum alongside the approved source revision.
2. AUS-34 must inventory the existing source revision, current Python package,
   schema/data, worker configuration and public contact setting without copying
   secret values. Compare behavior with this older 0.1.0 wheel. Preserve current
   data and credentials; do not replace a manually uploaded app blindly.
3. Review environment **names**, not values: old `app.main:app` becomes wrapper
   `main:app` (installed core `brandman.main:app`). Core accepts legacy
   `BRAND_OS_*` aliases but canonical names are `BRANDMAN_*`; verify each mapping
   from `brandman/__init__.py` and `.env.example` before any operator change.
   Keep `BRANDMAN_DB` pointing at the reconciled durable destination and use
   `BRANDMAN_DATABASE_PROFILE=operating`. No destination is selected here.
   Preserve the existing credential master key and password via the host secret
   manager. Verify `BRANDMAN_PREVIEW_PASSWORD`, `BRANDMAN_BASIC_USER`,
   `BRANDMAN_CREDENTIAL_MASTER_KEY`, `BRANDMAN_ALLOWED_HOSTS`,
   `BRANDMAN_REQUIRE_HTTPS`, proxy trust and worker settings. Do not infer that
   a local scratch DB or the wrapper implements the AUS-52 remote backend.
   `BRANDMAN_ACCESS_REQUEST_EMAIL` needs the existing verified address or an
   owner-selected address; none is invented. Optional `ANTHROPIC_API_KEY` and
   `BRANDMAN_DRAFT_MODEL` remain an operator decision; no model call was made.
4. AUS-52/AUS-50 must supply backup capture/isolated restore evidence with schema
   compatibility, data integrity, credential/job isolation and measured RPO/RTO.
   Keep writes quiesced during consistent capture/cutover. A prior application
   artifact **and compatible restored data** must be retained and rehearsed.
   This wrapper proves recoverable installation of the pinned wheel only; it
   does not prove production data recovery or a prior live deployment route.
   Do not rely on an expired deployment or assume dashboard rollback exists.
5. Only after source/data reconciliation, storage/backup/rollback evidence and
   separate owner go/no-go, the operator may link/select the existing app and
   run `uv run fastapi deploy` from the clean wrapper directory. Verify target
   identity before that command. No such command is run by CI or this proof.
   If the host ignores wheel-only policy or requests a core source build, stop
   rather than enabling Node builds or bypassing the lock.
6. After separately authorized deployment, check the exact artifact/version in
   build logs, health/startup logs, login, authenticated desktop/mobile dashboard,
   deep links, JS/CSS MIME types, contact panel and existing data. Check worker
   behavior separately. On failure, stop writes and follow the **rehearsed**
   application+data recovery route; never downgrade a live schema by assumption.

Until those operator steps are satisfied, keep production replacement blocked.
A green local proof and CI are code evidence only.
