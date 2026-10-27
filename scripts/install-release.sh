#!/usr/bin/env bash
# Install a released StreamStack wheel with pipx, after checking it against the signed release manifest.
#
#   scripts/install-release.sh 0.0.8                    download, verify checksums (and the signature when
#                                                       notation and a certificate are available), pipx install,
#                                                       then run `streamstack verify-install`
#   scripts/install-release.sh --require-signature --cert signing.pem 0.0.8
#   scripts/install-release.sh --verify-only 0.0.8      download and verify; install nothing
#
# The release files live in the project's Generic Package Registry (private): a GitLab token is read from
# GITLAB_TOKEN or `glab`, handed to curl on stdin, and never printed. See docs/release.md.
set -euo pipefail

HOST="${STREAMSTACK_GITLAB_HOST:-git.developerdojo.org}"
PROJECT_ID="${STREAMSTACK_PROJECT_ID:-2122}"
PACKAGE="streamstack-release"

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-2}"
}

verify_only=0
require_signature=0
cert="${STREAMSTACK_SIGNING_CERT:-}"
version=""
while [ $# -gt 0 ]; do
  case "$1" in
    --verify-only) verify_only=1 ;;
    --require-signature) require_signature=1 ;;
    --cert) shift; cert="${1:?--cert needs a file}" ;;
    -h|--help) usage 0 ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *) [ -z "$version" ] || usage; version="${1#v}" ;;
  esac
  shift
done
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "give a release version such as 0.0.8" >&2; usage; }

token="${GITLAB_TOKEN:-}"
if [ -z "$token" ] && command -v glab >/dev/null 2>&1; then
  token="$(glab config get token --host "$HOST" 2>/dev/null || true)"
fi
[ -n "$token" ] || { echo "no GitLab token: set GITLAB_TOKEN or run 'glab auth login --hostname $HOST'" >&2; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
base="https://$HOST/api/v4/projects/$PROJECT_ID/packages/generic/$PACKAGE/$version"
wheel="streamstack-$version-py3-none-any.whl"
sdist="streamstack-$version.tar.gz"

fetch() {
  # the token goes to curl on stdin (-K -), so it is in no process listing
  printf 'header = "PRIVATE-TOKEN: %s"\n' "$token" | curl -fsS --retry 3 -K - -o "$work/$1" "$base/$1" \
    || { echo "could not download $1 from $base" >&2; exit 1; }
}

echo "== downloading release $version"
for f in RELEASE-SHA256SUMS RELEASE-SHA256SUMS.cose.sig python-sbom.cdx.json "$wheel" "$sdist"; do
  fetch "$f"
done

echo "== checking the files against the release manifest"
for f in "$wheel" "$sdist" python-sbom.cdx.json; do
  grep -qE "^[0-9a-f]{64} [ *]${f//./\\.}\$" "$work/RELEASE-SHA256SUMS" \
    || { echo "$f is not listed in the signed manifest" >&2; exit 1; }
done
( cd "$work" && sha256sum --check --ignore-missing RELEASE-SHA256SUMS )

signature="unchecked"
if command -v notation >/dev/null 2>&1 && [ -n "$cert" ]; then
  echo "== verifying the signature on the manifest"
  [ -f "$cert" ] || { echo "certificate $cert not found" >&2; exit 1; }
  export XDG_CONFIG_HOME="$work/notation-config"
  mkdir -p "$XDG_CONFIG_HOME/notation/truststore/x509/ca/streamstack"
  cp "$cert" "$XDG_CONFIG_HOME/notation/truststore/x509/ca/streamstack/signing.crt"
  cat > "$XDG_CONFIG_HOME/notation/trustpolicy.blob.json" <<'POLICY'
{
  "version": "1.0",
  "trustPolicies": [{
    "name": "streamstack",
    "signatureVerification": { "level": "strict" },
    "trustStores": ["ca:streamstack"],
    "trustedIdentities": ["*"]
  }]
}
POLICY
  notation blob verify --policy-name streamstack --signature "$work/RELEASE-SHA256SUMS.cose.sig" "$work/RELEASE-SHA256SUMS"
  signature="verified"
elif [ "$require_signature" = 1 ]; then
  echo "--require-signature needs notation >= 2.0.0-alpha.1 on PATH and --cert (or STREAMSTACK_SIGNING_CERT)" >&2
  exit 1
else
  echo "NOTE: the checksums match the manifest, but the manifest's signature was NOT verified" >&2
  echo "      (needs notation >= 2.0.0-alpha.1 and the signing certificate; see docs/release.md)" >&2
fi

if [ "$verify_only" = 1 ]; then
  echo "release $version: checksums ok, signature $signature (nothing installed)"
  exit 0
fi

command -v pipx >/dev/null 2>&1 || { echo "pipx is required (apt install pipx)" >&2; exit 1; }
echo "== installing with pipx"
# --force: pipx treats the old 0.1.0-0.3.1 releases as newer than 0.0.8, so a plain upgrade would refuse.
# --system-site-packages: the tray uses the distribution's python3-gi.
pipx install --force --system-site-packages "$work/$wheel"

bin_dir="$(pipx environment --value PIPX_BIN_DIR)"
echo "== streamstack verify-install"
"$bin_dir/streamstack" verify-install --expect-version "$version"
echo "release $version installed: checksums ok, signature $signature"
