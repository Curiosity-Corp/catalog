# Releasing StreamStack

A release is a `vX.Y.Z` tag. CI builds the wheel and sdist, writes a CycloneDX SBOM, signs a checksum manifest
over all of them, publishes to the project's package registries and creates the GitLab Release. Nothing is built or
signed on a workstation.

## Versioning rule

StreamStack ships **0.0.x patch releases only**. Minor and major versions are set by the owner. Two gates enforce it:

- `scripts/release.py prepare` refuses a release whose major or minor differs from the current `__version__`
  and says why. The owner's approval is recorded by passing `--owner-approved-minor`.
- The tag pipeline's `release-verify` job refuses a tag that is not `0.0.x`, unless the pipeline was started with
  `RELEASE_OWNER_APPROVED_MINOR=true`.

A downgrade is always refused. The old `0.1.0` to `0.3.1` tags stay in Git for history; pip treats them as newer
than `0.0.8`, so installing a `0.0.x` release over one of them needs `--force` (the install script does this).

## Cut a release

1. All work for the release is merged to `main`, each merge request with its changelog fragment in `changelog.d/`
   (see [Changelog fragments](#changelog-fragments)). `CHANGELOG.md` itself is only edited by the release commit.
2. On an up-to-date `main` checkout with a clean tree (use a worktree, not a shared clone):

   ```bash
   scripts/release.py prepare            # 0.0.8 while it has no CHANGELOG section yet, else the next patch
   scripts/release.py prepare 0.0.9      # an explicit patch version
   scripts/release.py prepare --dry-run  # check everything, change nothing
   ```

   It checks the working tree (clean, on `main`, not behind `origin/main`, tag not taken), compiles every fragment in
   `changelog.d/` (plus anything still hand-written under "Unreleased") into `## [X.Y.Z] - date` with a fresh empty
   "Unreleased" above it, deletes the fragments, sets `streamstack.__version__`, and prints the remaining commands.
3. Before tagging, run `gitleaks detect --source . --no-git --redact --config .gitleaks.toml` (the unit tests do it
   when `gitleaks` is installed). Secret scanning only blocks on tags, and tags are immutable (the server refuses to
   delete or move one), so a finding found on the tag pipeline costs a version number: v0.0.8 and v0.0.9 were
   burned this way.
4. Commit (`chore(release): X.Y.Z`), merge that commit to `main` through its merge request, then tag the merged
   commit and push the tag:

   ```bash
   git tag -a vX.Y.Z -m "StreamStack X.Y.Z"
   git push origin vX.Y.Z
   ```
5. Watch the tag pipeline (`glab ci view`). On success the release is on the Releases page and
   `scripts/install-release.sh X.Y.Z` installs it.

The `v*` tag pattern must be a **protected tag** in the project (Settings, Repository). `python-sign` and
`release-evidence` run on the `ci-dispatch` runner, which only serves protected refs; on an unprotected tag those
jobs sit "stuck".

## Changelog fragments

`CHANGELOG.md` is **not** edited by feature merge requests: every merge to `main` used to invalidate the changelog
hunk of every other open merge request, and a wave of parallel MRs rebased two or three times each. Instead each
merge request adds `changelog.d/<issue>.<type>.md` (one file per entry, `type` is `added`, `changed`, `deprecated`,
`removed`, `fixed` or `security`), and the release compiles them. The full how-to is `changelog.d/README.md`:

```bash
echo 'What changed for the user, and why (#140).' > changelog.d/140.security.md
```

Gates:

- **Merge requests** run `changelog-check` (`scripts/release.py check-fragments`). It fails a merge request that
  edits `CHANGELOG.md`, that changes `src/` without adding a fragment, or that adds a malformed fragment. Release
  commits (`chore(release): ...`) are exempt. An explicit opt-out is the `skip-changelog` merge request label, or a
  `Skip-Changelog: <reason>` trailer on a commit (or a line in the MR description).
- **The release commit** is made by `scripts/release.py prepare`, which deletes the fragments it compiled.
- **The tag pipeline** (`release-verify`) refuses a tag whose tree still holds fragments.

## The tag pipeline

The pipeline is the org's launch-sequence `python-package` template (pinned to a release tag in `.gitlab-ci.yml`)
plus StreamStack's own jobs. On a tag every gate runs again before anything is published.

| Stage | Job | What it does |
| --- | --- | --- |
| preflight | `release-verify` | tag == `streamstack.__version__`, `CHANGELOG.md` has a non-empty `## [X.Y.Z]` section, no fragments left in `changelog.d/`, patch-only rule |
| preflight | `changelog-check` | merge requests only: a `src/` change adds a changelog fragment, `CHANGELOG.md` is not edited directly |
| preflight | `lint`, `test` | ruff, and the unit tests on Python 3.13 |
| source-security | `sast`, `e2e` | semgrep and gitleaks (blocking on tags), the Console browser suite |
| supply-chain | `lockfile-verify`, `dependency-scan`, `scorecard` | the wheel has no runtime dependencies (asserted), dependency and repository posture scans |
| build | `python-build`, `package` | wheel and sdist from the tagged commit, `dist/SHA256SUMS` re-verified, `twine check`; `package` installs a separately built wheel and runs `streamstack verify-install` |
| supply-chain-vex | `python-sbom` | CycloneDX 1.6 SBOM of what the wheel installs with the `auto` and `framing` extras |
| artifact-security, policy-gate | `python-vuln-scan`, `python-license`, `pip-audit`, `test-py311` | grype over the SBOM (fails on a fixable high or critical), licence policy, PyPA audit, Python 3.11 tests |
| attest | `python-sign` | signs `RELEASE-SHA256SUMS` (below) |
| artifact-promote | `python-publish` | uploads exactly the built files to the project's PyPI registry; uploads the signed manifest, signature and SBOM to the Generic Package Registry |
| release-create | `release-notes`, `release-assets`, `release` | notes are the CHANGELOG section of the tag; wheel and sdist also go to the Generic Package Registry; the GitLab Release links all of it |
| pipeline-attestation | `pipeline-attestation` | in-toto statement over the built files |

Merges to `main` run the same jobs up to and including `python-sign`, so signing and the SBOM are proven on every
merge; only `python-publish` and the release jobs need a tag.

### Where the files are

For release `X.Y.Z`:

- PyPI registry: `https://$STREAMSTACK_GITLAB_HOST/api/v4/projects/2122/packages/pypi/simple` (wheel and sdist)
- Generic Package Registry, package `streamstack-release`, version `X.Y.Z`:
  `streamstack-X.Y.Z-py3-none-any.whl`, `streamstack-X.Y.Z.tar.gz`, `SHA256SUMS`, `RELEASE-SHA256SUMS`,
  `RELEASE-SHA256SUMS.cose.sig`, `python-sbom.cdx.json`

The project is private: every download needs a GitLab token (`PRIVATE-TOKEN` header).

**The release host is configuration, never code.** The GitLab host that serves the registry is not written into
anything this repository ships (the public catalog contract gate forbids it). Set `STREAMSTACK_GITLAB_HOST` to the bare
host name (`$STREAMSTACK_GITLAB_HOST` in the URLs here); `STREAMSTACK_PROJECT_ID` overrides the default project id
`2122`. In a pipeline of the project `CI_SERVER_HOST` supplies it, and `scripts/install-release.sh` also falls back to
the host of the checkout's `origin` remote. `streamstack chain install-plugin`, `streamstack verify-install` and the
install script all read it the same way (`streamstack.registry`) and say which variable to set when it is missing.

## Signing

Method: **Notation with the azure-kv plugin** (`notation blob sign`, COSE signature), the signing key held in Azure
Key Vault, run as a Kubernetes Job in the `ci` namespace with the org's `notation-signing` secret. The key never
reaches the runner pod and no signing material is in this repository.

Why not keyless Sigstore (cosign)? Keyless needs a Fulcio and Rekor the CI can reach and an OIDC identity to bind to;
the org's CI has neither, and its trust root is the Key Vault key every other launch-sequence job already uses and
verifies (`signature-verify`). StreamStack uses that trust root rather than adding a second one. Cosign stays an
option for the day the org gets a Fulcio.

What is signed is `RELEASE-SHA256SUMS`: the `sha256sum` lines of the wheel and sdist plus the SBOM's. One signature
over that file therefore covers all three; verify the signature, then the checksums, and every byte is verified.

## Verify a release

`scripts/install-release.sh --verify-only X.Y.Z` does steps 1 to 3 below. By hand:

1. Download the five release files (`RELEASE-SHA256SUMS`, `RELEASE-SHA256SUMS.cose.sig`, `python-sbom.cdx.json`,
   the wheel and the sdist) from the Generic Package Registry URL above into one directory.
2. Verify the signature on the manifest. This needs [notation](https://notaryproject.dev) 2.0.0-alpha.1 or newer
   (the first release with `notation blob`) and the signing certificate, the public certificate of the org's
   Key Vault signing key, in a trust store:

   ```bash
   mkdir -p ~/.config/notation/truststore/x509/ca/streamstack
   cp signing-cert.pem ~/.config/notation/truststore/x509/ca/streamstack/
   cat > ~/.config/notation/trustpolicy.blob.json <<'JSON'
   {"version": "1.0", "trustPolicies": [{"name": "streamstack",
     "signatureVerification": {"level": "strict"}, "trustStores": ["ca:streamstack"], "trustedIdentities": ["*"]}]}
   JSON
   notation blob verify --policy-name streamstack --signature RELEASE-SHA256SUMS.cose.sig RELEASE-SHA256SUMS
   ```

   The certificate is public key material, not a secret; the signature embeds it, and the org's own verifier
   fetches it from the same Key Vault certificate the signer uses. Pass it to the install script with
   `--cert signing-cert.pem` (or `STREAMSTACK_SIGNING_CERT`); add `--require-signature` to refuse to install
   unless the signature verified.
3. Check the files against the signed manifest:

   ```bash
   sha256sum --check --ignore-missing RELEASE-SHA256SUMS
   ```
4. After installing, `streamstack verify-install` checks the install itself and prints these instructions again.

Without notation or the certificate, `scripts/install-release.sh` still checks the checksums against the manifest and
says plainly that the signature was not verified.

## SBOM

`python-sbom.cdx.json` is a CycloneDX 1.6 JSON document (reproducible output, validated against the schema in CI) of
the environment the built wheel installs into with the `auto` and `framing` extras: StreamStack itself (type
`application`) and every package pip resolves for it, with their versions and licences where the metadata carries them.
StreamStack's own `dependencies` is empty, so everything else in it comes from the optional extras. grype reads the
same file for the vulnerability gate and the licence gate reads it for the licence policy. It is part of the signed
manifest, so its checksum is covered by the signature.

Inspect it with any CycloneDX tool, or quickly:

```bash
python3 -c 'import json; b = json.load(open("python-sbom.cdx.json")); print(len(b["components"]), "components"); [print(c["name"], c["version"]) for c in b["components"]]'
```

## Install on a workstation

One command, from a checkout (or a copy of the script) with a GitLab token in `GITLAB_TOKEN` or a logged-in `glab`:

```bash
scripts/install-release.sh 0.0.8
```

It downloads the release, checks the files against the manifest (and the manifest's signature when notation and the
certificate are available), runs `pipx install --force --system-site-packages` on the wheel and finishes with
`streamstack verify-install --expect-version 0.0.8`. `--force` is needed because pip ranks the old 0.1.0 to 0.3.1
releases above 0.0.8; `--system-site-packages` lets the tray use the distribution's `python3-gi`. Run
`streamstack setup` afterwards, as before.

Alternatively, straight from the registry: `pipx install --force --system-site-packages streamstack==0.0.8
--index-url https://__token__:$GITLAB_TOKEN@$STREAMSTACK_GITLAB_HOST/api/v4/projects/2122/packages/pypi/simple`
(this skips the signature and manifest checks).

The `developer_workstation` catalog role installs a vendored copy of a release tag with `make install`-style copies
into `/usr/local` (`files/streamstack`, a squashed subtree of the release tag). That path is unchanged: update the
subtree to the new tag and the role installs it. `streamstack verify-install` works on it too (it reports "no pip
metadata" as a warning).

### `streamstack verify-install`

Checks, and exits 1 if one fails:

- the version the code reports against the version pip recorded (a stale copy shadowing the install fails), and
  `--expect-version X.Y.Z` to insist on a particular release;
- the packaged data: the Console frontend, icons, voice presets and personas;
- the four commands (`streamstack`, `streamstack-auto`, `streamstack-console`, `streamstack-tray`) against the
  entry points pip recorded, that each resolves to its function, and that it is on `PATH` (the tray only warns when
  `python3-gi` is missing).

Then it prints how to verify the signature.

## What only a real tag proves

The tag-only jobs (`release-verify`, `python-publish`, `release-notes`, `release-assets`, `release`,
`release-evidence`) cannot run before a tag exists, and publishing is not reversible. Their logic that lives in this
repository (`scripts/release.py`) is unit-tested, and the pipeline configuration is linted and dry-run against an
existing tag ref. The first tag pipeline additionally exercises the Notation job on a protected tag, the registry
upload and the release creation; after it, run `scripts/install-release.sh --verify-only X.Y.Z` and
`streamstack verify-install` to prove the artifacts end to end, and record the signing certificate's SHA-256
fingerprint here.
