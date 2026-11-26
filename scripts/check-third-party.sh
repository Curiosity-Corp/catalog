#!/bin/sh
# Licence scan of the BUILT collection artifact (catalog#48, THIRD_PARTY.md).
#
#   scripts/check-third-party.sh curiosity-workstation-X.Y.Z.tar.gz
#
# Unpacks the tarball and runs the REUSE tool (pinned in ci/requirements.in) on
# what a lab would actually redistribute, with the REUSE.toml shipped inside it:
#
# 1. `reuse lint` fails on a file no annotation covers (an image, font,
#    archive, installer or key nobody listed), on a licence the inventory uses
#    without its text in LICENSES/, and on a licence text nothing uses.
# 2. A file that carries its own copyright or licence notice must be listed in
#    REUSE.toml by path. reuse reports such a notice with source_type
#    "file-header" (or "dot-license" for a NAME.license file); listed files use
#    precedence "override", so any such report is unlisted third-party material.
set -eu

# Pick the pure-Python encoding detector (ci/requirements.in) over python-magic,
# so the result does not depend on whether the host has libmagic.
export REUSE_ENCODING_MODULE=charset_normalizer

if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
  echo "usage: $0 COLLECTION_TARBALL" >&2
  exit 2
fi
command -v reuse >/dev/null 2>&1 || {
  echo "reuse not found: install the CI toolchain (pip install --require-hashes -r ci/requirements.txt)" >&2
  exit 2
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT INT TERM
tar -xzf "$1" -C "$work"

for f in REUSE.toml THIRD_PARTY.md NOTICE LICENSE; do
  if [ ! -f "$work/$f" ]; then
    echo "third-party: $f is missing from $1; it must ship with the collection" >&2
    exit 1
  fi
done

echo "== reuse lint (every file covered, every licence text present and used)"
reuse --root "$work" lint

echo "== files that carry their own notice but are not listed in REUSE.toml"
reuse --root "$work" lint --json > "$work/.reuse-lint.json"
unlisted=$(awk '
  /^ *"path": / { path = $0; sub(/^ *"path": "/, "", path); sub(/",?$/, "", path) }
  /"source_type": "(file-header|dot-license)"/ { print path }
' "$work/.reuse-lint.json" | sort -u)
if [ -n "$unlisted" ]; then
  echo "$unlisted" | sed 's/^/  /' >&2
  echo "third-party: these files carry a copyright or licence notice of their own." >&2
  echo "List each in REUSE.toml with its source, version and licence (THIRD_PARTY.md)." >&2
  exit 1
fi
echo "none"
