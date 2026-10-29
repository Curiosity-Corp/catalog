#!/bin/sh
# Runs INSIDE the signing Job's pod (scripts/ci/deb-sign.sh creates it); never on a workstation or the runner.
#
# Two signatures, two trust roots, both held in the org's Azure Key Vault:
#   1. Notation (azure-kv plugin, COSE) over DEB-SHA256SUMS: the package, its provenance statement and its checksums,
#      the same key and format as the wheel's RELEASE-SHA256SUMS (python-sign).
#   2. OpenPGP over the apt repository's Release file (InRelease and Release.gpg), the signature apt itself verifies,
#      with the Key Vault secret STREAMSTACK_APT_KEY_SECRET. The private key lives only in this pod's temporary
#      GNUPGHOME and is discarded with it.
# The results go back through the pod log between SIGN-BEGIN <name> / SIGN-END markers (base64); nothing is printed
# that is not a signature. Inputs are mounted read-only at /work; credentials come from the notation-signing secret.
set -eu

: "${NOTATION_KEY_ID:?}" "${AZURE_CLIENT_ID:?}" "${AZURE_CLIENT_SECRET:?}" "${AZURE_TENANT_ID:?}"
: "${KEY_VAULT_NAME:?}" "${APT_KEY_SECRET:?}" "${APT_KEY_FINGERPRINT:?}"

export XDG_CONFIG_HOME=/tmp
export PATH="/tools:${PATH}"
OUT=/tmp/out
mkdir -p "${XDG_CONFIG_HOME}/notation/plugins/azure-kv" "${OUT}/notation"
cp /tools/notation-plugins/azure-kv/notation-azure-kv "${XDG_CONFIG_HOME}/notation/plugins/azure-kv/"

emit() { # name file
  echo "SIGN-BEGIN $1"
  base64 <"$2"
  echo "SIGN-END"
}

# --- 1. Notation over the package manifest -------------------------------------------------------------------------
notation blob sign \
  --signature-format cose \
  --id "${NOTATION_KEY_ID}" \
  --plugin azure-kv \
  --plugin-config self_signed=true \
  --plugin-config credential_type=environment \
  --signature-directory "${OUT}/notation" \
  /work/DEB-SHA256SUMS
count=0
for sig in "${OUT}"/notation/*.sig; do
  [ -s "${sig}" ] || continue
  count=$((count + 1))
  emit DEB-SHA256SUMS.cose.sig "${sig}"
done
[ "${count}" -eq 1 ] || { echo "expected exactly one Notation signature, found ${count}" >&2; exit 1; }

# --- 2. OpenPGP over the apt Release file --------------------------------------------------------------------------
export GNUPGHOME=/tmp/gnupg
mkdir -m 700 "${GNUPGHOME}"
TOKEN="$(curl -fsS --retry 3 \
  --data-urlencode "client_id=${AZURE_CLIENT_ID}" \
  --data-urlencode "client_secret=${AZURE_CLIENT_SECRET}" \
  --data-urlencode "scope=https://vault.azure.net/.default" \
  --data-urlencode "grant_type=client_credentials" \
  "https://login.microsoftonline.com/${AZURE_TENANT_ID}/oauth2/v2.0/token" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
curl -fsS --retry 3 -H "Authorization: Bearer ${TOKEN}" \
  "https://${KEY_VAULT_NAME}.vault.azure.net/secrets/${APT_KEY_SECRET}?api-version=7.4" |
  python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["value"])' >/tmp/apt-key.asc
unset TOKEN
gpg --batch --quiet --import /tmp/apt-key.asc
rm -f /tmp/apt-key.asc
FOUND="$(gpg --batch --with-colons --list-secret-keys | awk -F: '/^fpr/{print $10; exit}')"
[ "${FOUND}" = "${APT_KEY_FINGERPRINT}" ] || {
  echo "the Key Vault secret is not the repository key (fingerprint ${FOUND}, expected ${APT_KEY_FINGERPRINT})" >&2
  exit 1
}
cp /work/Release "${OUT}/Release"
gpg --batch --yes --pinentry-mode loopback --passphrase '' --local-user "${FOUND}" \
  --clearsign --output "${OUT}/InRelease" "${OUT}/Release"
gpg --batch --yes --pinentry-mode loopback --passphrase '' --local-user "${FOUND}" \
  --armor --detach-sign --output "${OUT}/Release.gpg" "${OUT}/Release"
gpg --batch --export "${FOUND}" >/tmp/apt-public.gpg
gpgv --keyring /tmp/apt-public.gpg "${OUT}/Release.gpg" "${OUT}/Release"
gpgv --keyring /tmp/apt-public.gpg "${OUT}/InRelease"
emit InRelease "${OUT}/InRelease"
emit Release.gpg "${OUT}/Release.gpg"
