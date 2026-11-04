#!/usr/bin/env bash
# Re-resolve the CI toolchain lock (ci/requirements.txt) from ci/requirements.in
# inside the same container image the pipeline uses, so the pins are the ones
# that image's Python can install. Needs podman or docker and network access.
#
#   scripts/lock_ci_toolchain.sh
#
# Review the diff, run `make supply-chain`, and open a merge request. Renovate
# proposes single-line bumps between re-resolves; this is for a full refresh.
set -euo pipefail

cd "$(dirname "$0")/.."
engine="${CONTAINER_ENGINE:-$(command -v podman || command -v docker || true)}"
if [ -z "$engine" ]; then
  echo "lock_ci_toolchain: install podman or docker (or set CONTAINER_ENGINE)" >&2
  exit 2
fi
image="$(sed -n 's/^image: *//p' .gitlab-ci.yml | head -n 1)"
[ -n "$image" ] || { echo "lock_ci_toolchain: no image: line in .gitlab-ci.yml" >&2; exit 2; }

header="$(sed -n '1,/^[A-Za-z]/{/^#/p}' ci/requirements.txt)"
out="$(mktemp)"
trap 'rm -f "$out"' EXIT

"$engine" run --rm -v "$PWD/ci:/ci:ro,Z" "$image" bash -c '
  set -e
  python -m venv /tmp/lock
  /tmp/lock/bin/python -m pip install --quiet --disable-pip-version-check -r /ci/requirements.in
  /tmp/lock/bin/python -m pip freeze --all --exclude-editable | grep -v "^pip==" | sort -f
' > "$out"

{ printf '%s\n' "$header"; cat "$out"; } > ci/requirements.txt
echo "wrote ci/requirements.txt ($(grep -c '==' ci/requirements.txt) pins); review with: git diff ci/requirements.txt"
