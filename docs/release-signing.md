# Release signing and evidence

Downstream labs should be able to check that an artifact came from the intended
source. This page defines how releases are signed, who holds the key, and what to
do before a key exists. CI never holds a signing key: it builds and checks the
evidence, and a person signs it.

## Bootstrap path (no signing key yet)

Until a release key exists, releases are published **unsigned**, and the release
notes say so in one line. The evidence is still complete:

1. The tag points at the exact commit CI built.
2. The release page links the pipeline, `collection.cdx.json` (SBOM) and the
   `evidence/` files from the `provenance` job: `SHA256SUMS`, `toolchain.json`,
   `provenance.intoto.json`.
3. A user verifies with `python scripts/release_evidence.py verify evidence --commit <sha>`
   and `sha256sum --check SHA256SUMS`. That proves the downloaded files match what
   CI recorded; it does not prove who ran CI. State that limit in the release notes.

## Key ownership, storage, rotation, recovery

These are the rules to adopt. Items marked **decide** need the repository owner.

| Topic | Rule |
| --- | --- |
| Owner | One named maintainer holds the release key (**decide**: who), and a second maintainer holds the revocation certificate. |
| Key type | An ed25519 OpenPGP key used only for releases, with a UID such as `Curiosity Workstation Releases <address the owner chooses>`. |
| Storage | On a hardware token (YubiKey or similar) if available; otherwise a passphrase-protected key on one encrypted machine. Never in GitLab, in CI variables, in the repository or in chat. |
| Publication | The public key and its full 40-character fingerprint are published in `SECURITY.md`, on the release page and on a keyserver, in a merge request the second maintainer approves. |
| Rotation | Every 2 years or when a holder leaves. Cross-sign the new key with the old one, publish both for one release, then revoke the old key. |
| Compromise | Revoke immediately with the revocation certificate, publish it, yank affected releases, re-sign the current release with a new key, and say so in `CHANGELOG.md`. |
| Recovery | Loss of the key without compromise: generate a new key, publish it with a note signed by the second maintainer's key, and re-sign the latest release. |

## Signing a release (key holder)

Do this on the key holder's machine after the tag pipeline is green. Download the
pipeline's `evidence/` directory, the artifact and `collection.cdx.json`.

```bash
# Sign the tag (the commit CI built).
git tag -s v<version> -m "curiosity.workstation <version>"
git push origin v<version>

# Verify the unsigned evidence first.
python scripts/release_evidence.py verify evidence --commit "$(git rev-parse v<version>^{commit})"

# Detached, armored signatures over the checksum list and the provenance statement.
gpg --armor --detach-sign evidence/SHA256SUMS
gpg --armor --detach-sign evidence/provenance.intoto.json

# Check them the way a user will.
python scripts/release_evidence.py verify evidence \
  --commit "$(git rev-parse v<version>^{commit})" --fingerprint <40-hex-fingerprint>
```

Publish the artifact, `collection.cdx.json`, `SHA256SUMS(.asc)`,
`provenance.intoto.json(.asc)` and `toolchain.json` on the release page, and
record the checksums and pipeline in the dated evidence log
(`docs/evidence/`). `SHA256SUMS` covers the artifact and the SBOM, so one
signature covers both.

## Verifying a signed release (user)

```bash
gpg --recv-keys <40-hex-fingerprint>          # or import the key from SECURITY.md
python scripts/release_evidence.py verify evidence --fingerprint <40-hex-fingerprint>
sha256sum --check evidence/SHA256SUMS         # next to the downloaded artifact and SBOM
git verify-tag v<version>
```

`--fingerprint` makes the check strict: a valid signature by any other key fails.
A signature next to a file with no fingerprint given is not trusted.
