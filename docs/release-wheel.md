# Release wheel preparation (AUS-71)

The current intended package version is **0.1.0**, pending owner review. The
reviewed starting revision is `04c39c9af83bf30107ff7ac612646882ddc7901b`, which
includes the dashboard startup/access-contact changes and optional Anthropic
drafting support. The first release must also include the reviewed AUS-71 proof;
select its final reviewed commit, not the old starting revision. No tag or
publication is authorized by this document. AUS-35 owns account readiness,
name availability, trusted publishing and the release go/no-go.

Build with Node 22 on the build runner using the existing `uv build` path. It
builds a wheel from the sdist and includes dashboard assets without committing
generated files. PR/main `package` CI and the shared `release-build.yml` both
run `bash scripts/check_python_only_wheel.sh` against the wheel just built.
The shared build is also the release dry-run; manual dispatch remains build-only.
All publisher permissions, tag/version checks and pinned release actions remain
unchanged.

The proof mounts only `dist` and the standalone smoke script into a fresh
`python:3.11-slim` container, creates an empty venv, installs that exact local
wheel with its `[anthropic]` extra and `--no-cache-dir --only-binary=:all:`, and
runs pip check. No checkout, frontend source or source/build cache is mounted.
The smoke script verifies Node/npm/npx are absent, imports BrandMan from the
installed venv, rejects frontend subprocess attempts and exercises default
startup with disposable data. Unauthenticated `/app` returns 401; authenticated
`/app` and `/app/content?brand=demo-brand` return dashboard HTML. Every referenced
JS/CSS asset is nonempty, 200, and has the expected MIME type; a missing asset
returns 404. Anthropic imports without a key or provider call.

After successful proof, `release-receipts.txt` records the exact GitHub source
revision and wheel/sdist filenames and SHA256 hashes. CI uploads receipts
separately from `release-dist`, so publication still receives only distributions.
Use the **release-runtime-receipts** and **release-dist** artifacts from the same
successful exact-commit release dry-run for the operator handoff. A fresh build
can have different hashes; do not substitute local receipts for CI artifacts.

Before publication, the operator must verify AUS-35 readiness and approve the
final source revision and version, including these dashboard/drafting fixes.
After an authorized publication actually exists, downstream AUS-72/AUS-73 must
verify PyPI version, source provenance and wheel hash against these receipts.
Use an exact `brandman[anthropic]==<verified-version>` wheel requirement and
hash-aware lock; prevent sdist fallback. No version is asserted to be published.
The FastAPI entrypoint is `brandman.main:app`; platform upload/wrapper support,
secrets, live data, durable storage, backups and rollback remain separate
operator/deployment tickets. Do not upload a bare wheel assuming the platform
will discover an application, or replace a live deployment from this proof.
