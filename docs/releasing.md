# Releasing BrandMan

A `v*` tag starts `.github/workflows/release.yml`. It tests the source, builds
and smoke-tests the wheel, sdist and image, then requests approval to publish.
Manual runs are **always build-only**, including a manual run against a tag:
they cannot log in to GHCR, publish to PyPI, or create a GitHub release.

## Before the first real release (Austin)

- [Configure a PyPI trusted publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/) with owner `AustinNChristensen`, repository
  `brandman`, workflow `release.yml`, and environment `pypi`.
- Create/protect the GitHub environment `pypi` with a required reviewer. Disable
  self-review and administrator bypass where supported. Allow release tags and
  verify these protections before tagging: declaring an environment in YAML
  does not create its required-reviewer protection.
- Verify the repository's Actions token can write GHCR packages and create
  GitHub releases. No stored PyPI API token is needed.
- Approve the first real release before anyone pushes a `v*` tag.

The PyPI and GHCR jobs both use `pypi`. PyPI alone receives `id-token: write`;
GHCR alone receives `packages: write`. GitHub release creation has its own
`contents: write` job, after both publishers succeed. A denied/failed PyPI job
also prevents GHCR publishing. Approval settings remain an account operation.

## Release checklist

1. Bump `[project].version` in `pyproject.toml` on a reviewed branch and update
   `uv.lock` with `uv lock`. Review the changelog/release notes and compatibility.
2. Wait for CI, including **release-dry-run / build**, to pass on the exact
   release commit. CI calls the same build-only workflow as Release. It builds
   a wheel from the sdist, installs it and checks its authenticated dashboard;
   it also builds and starts the image without pushing it. The `release-dist`
   Actions artifact contains the wheel and sdist (it is not a registry release).
3. After merge, run **Release → Run workflow** on the intended branch/commit
   using the Actions UI or `gh workflow run release.yml --ref main`. Wait for
   test/build success and confirm the three publishing jobs are skipped.
   Workflow dispatch becomes available after this workflow is on the default
   branch. Manual runs against existing tags also skip all publishers.
4. Confirm the trusted publisher and required environment reviewer are active.
   Obtain Austin's approval and check `uv version --short` on the exact commit.
5. An authorized human creates and pushes `v<VERSION>` on that approved commit.
   The tag must exactly match the package version; a mismatch fails before
   publishing. Agents must not create/push release tags or publish.
6. Review the tagged commit, build evidence and package files before approving
   each `pypi` environment job. Confirm the resulting PyPI version, GHCR digest
   and GitHub release all correspond to the approved commit.

## Irreversible operations and recovery

[PyPI forbids reusing distribution filenames](https://pypi.org/help/#file-name-reuse),
even after deletion; use a new version when rebuilding published distributions. Published wheels,
source archives and container layers may already have been downloaded.
Removing a tag, registry entry or GitHub release cannot recall those copies.
GHCR's `latest` and major/minor tags move on publication; record immutable
image digests for deployments.

Publishing is not atomic across services. If PyPI succeeds and a later job
fails, stop and inspect the receipts. Do not rerun the entire release blindly
or reuse the version: an authorized maintainer must choose targeted recovery
or a new version. No release workflow deploys the application.

## Action maintenance

Release and its reusable build workflow pin external actions to full commit
SHAs, with their source tag/ref in a trailing comment. Verify each replacement
against the upstream repository and review the action change before updating
pins. Rerun the dry-run checks; a comment/tag name is documentation, not a
mutable executable dependency.
