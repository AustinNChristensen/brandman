#!/usr/bin/env bash
# Mount only the release artifact and smoke probe: no checkout or frontend source.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
mapfile -t wheels < <(find "$root/dist" -maxdepth 1 -name '*.whl' -type f)
test "${#wheels[@]}" -eq 1
wheel=$(basename "${wheels[0]}")
docker run --rm \
  --mount "type=bind,source=$root/dist,target=/dist,readonly" \
  --mount "type=bind,source=$root/scripts/check_dashboard_package.py,target=/proof/check_dashboard_package.py,readonly" \
  --workdir /tmp \
  -e PYTHONPATH= -e PYTHONNOUSERSITE=1 \
  python:3.11-slim \
  sh -ec 'python -m venv /runtime
    /runtime/bin/pip install --no-cache-dir --only-binary=:all: "/dist/$1[anthropic]"
    /runtime/bin/pip check
    /runtime/bin/python -I /proof/check_dashboard_package.py --python-only' sh "$wheel"
# Kept outside dist: publishers must continue to receive only wheel/sdist files.
{
  printf 'source_revision=%s\n' "${GITHUB_SHA:-$(git -C "$root" rev-parse HEAD)}"
  printf 'python_runtime=python:3.11-slim\n'
  (cd "$root" && sha256sum dist/*.whl dist/*.tar.gz)
} | tee "$root/release-receipts.txt"
