# Releasing StreamStack

A release is a `vX.Y.Z` tag. CI builds the wheel and sdist, writes a CycloneDX SBOM, signs a checksum manifest
over all of them, builds the Debian package with its provenance statement, signs that and a signed apt repository,
publishes to the project's package registries and creates the GitLab Release. Nothing is built or signed on a
workstation.

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
4. Commit (`chore(release): X.Y.Z`) and merge it to `main` through its merge request, then tag the **release MR's
   head (source) commit**, not the merge commit, and push the tag:

   ```bash
   git tag -a vX.Y.Z <release-MR-head-sha> -m "StreamStack X.Y.Z"
   git push origin vX.Y.Z
   ```

   Other merge requests merge ahead of the release on the train and bring their changelog fragments, so the merge
   commit can hold fragments the tag gate refuses (v0.0.14 was burned that way). The head commit is `main` at
   `prepare` time plus the compiled changelog, so its code and CHANGELOG agree exactly; the fragments that landed in
   between simply roll into the next release. Before pushing, run `python3 scripts/release.py check-tag vX.Y.Z
   --require-main-ancestor` in a checkout of that commit: it must be an ancestor of `origin/main`, with no fragments.
   **Burned versions** (their tags exist, nothing was published; the changes shipped in the next release):
   v0.0.8 and v0.0.9 (secret scan on the tag), v0.0.14 (fragments left in the tag's tree), v0.0.17 (`release-verify`
   had no `git` in its image; the release MR now runs `release-verify-mr` first) and v0.0.18 (three tag-only
   blockers: gitleaks flagged the public `APT_KEY_FINGERPRINT`, `apt-publish` had no `gpgv`, and `python-sbom`
   asked uv for the removed `auto` extra). Look at the tag-only jobs' rules before you tag, not after.
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

- **The release MR** (title `chore(release): X.Y.Z`) runs `release-verify-mr`: the tag job's image and `check-tag vX.Y.Z` against the MR's source head, the commit you will tag, so a tag-only failure (fragments left, a tool missing from the image) shows up before the tag exists. Only `--require-main-ancestor` has to wait for the merge.
- **Merge requests** run `changelog-check` (`scripts/release.py check-fragments`). It fails a merge request that
  edits `CHANGELOG.md`, that changes `src/` without adding a fragment, or that adds a malformed fragment. Release
  commits (`chore(release): ...`) are exempt. An explicit opt-out is the `skip-changelog` merge request label, or a
  `Skip-Changelog: <reason>` trailer on a commit (or a line in the MR description).
- **The release commit** is made by `scripts/release.py prepare`, which deletes the fragments it compiled.
- **The tag pipeline** (`release-verify`) refuses a tag whose tree still holds fragments.

## The tag pipeline

The pipeline is the org's launch-sequence `python-package` template (pinned to a release tag in `.gitlab-ci.yml`)
plus StreamStack's own jobs. On a tag every gate runs again before anything is published.

Every stage of the launch-sequence `python-package` pipeline, the job that fills it and what that job proves. A
stage that is green on a tag pipeline is the definition of done for a release (#102); `tests/test_release_stages.py`
fails when this table and `.gitlab-ci.yml` drift apart.

| Stage | Job | What it does |
| --- | --- | --- |
| .pre | `offboarded-identity-guard` | refuses commits by offboarded identities |
| preflight | `release-verify` (tags), `lint`, `test`, `pipeline-canary`, `changelog-check` (merge requests) | tag == `streamstack.__version__`, `CHANGELOG.md` has a non-empty `## [X.Y.Z]` section, patch-only rule, no fragments left in `changelog.d/`; ruff; the unit tests on Python 3.13; the launch-sequence sentinel, an always-green job that shows the template loaded; an MR adds a changelog fragment when it changes `src/` |
| source-security | `sast`, `e2e`, `e2e-lighting` x3 | semgrep and gitleaks (blocking on tags), the Console browser suite, the lighting soak at night, noon and the evening transition |
| source-evidence | `source-evidence` | a structured evidence report of the source assurance controls, hashed and Notation-signed (Kubernetes Job) |
| supply-chain | `lockfile-verify`, `dependency-scan`, `scorecard` | the wheel has no runtime dependencies (asserted), dependency and repository posture scans |
| build | `python-build`, `package`, `deb-build`, `dfn-ladspa-image` | wheel and sdist from the tagged commit with `dist/SHA256SUMS` re-verified and `twine check`; `package` installs a separately built wheel and runs `streamstack verify-install`; `deb-build` builds the reproducible `.deb`, its provenance statement and `DEB-SHA256SUMS`, runs the extracted launcher and the apt tests (signatures, tampering, update, rollback); `dfn-ladspa-image` builds the DeepFilterNet plugin (main, when its recipe changes; docs/audio-chain.md) |
| supply-chain-vex | `python-sbom`, `apt-index` | CycloneDX 1.6 SBOM of what the wheel installs with the `auto`, `framing` and `whip` extras; the apt repository index of every published package plus this build |
| supply-chain-evidence | `supply-chain-evidence` | the same for the supply-chain controls (lockfile, base images, dependency scan, scorecard, VEX) |
| artifact-security | `python-vuln-scan`, `pip-audit`, `test-py311` | grype over the SBOM (fails on a fixable high or critical), the PyPA advisory audit, the unit tests on the oldest supported Python (3.11) |
| policy-gate | `python-license` | licence policy over the SBOM |
| attest | `python-sign`, `deb-sign` | Notation signature over `RELEASE-SHA256SUMS`; Notation signature over `DEB-SHA256SUMS` and the OpenPGP signature over the apt `Release` file (below) |
| artifact-promote | `python-publish`, `apt-publish` (tags), `dfn-ladspa-package`, `dfn-ladspa-verify` | the built files to the project's PyPI registry, the signed manifest, signature and SBOM to the Generic Package Registry; the verified apt repository to `streamstack-apt/stable` and the package files to `streamstack-release/X.Y.Z`; the plugin to `streamstack-ladspa/<version>`, fetched back and run (main, when its recipe changes) |
| compliance | `defectdojo` | opt-in at the organisation level (below) |
| release-create | `release-notes`, `release-assets`, `release` | notes are the CHANGELOG section of the tag; wheel and sdist also go to the Generic Package Registry; the GitLab Release links all of it |
| release-evidence | `release-evidence` | the same for the release lifecycle (release, notes, archive, bundle signatures) |
| pipeline-attestation | `pipeline-attestation` | in-toto statement over the built files |

`compliance` holds one job, the DefectDojo import. The template makes it opt-in: it exists only where the organisation
has set `DEFECTDOJO_URL` and `DEFECTDOJO_API_TOKEN` as group variables, which Curiosity-Corp has not done. Without them the
stage has no job, which GitLab shows as nothing rather than as a skipped or red job, and the v0.0.12 tag pipeline is
green in every other stage. Enabling it is an organisation decision, not a StreamStack change; once the variables exist
the job appears and the table needs no edit.

The gate that keeps main green: merge trains are on, so every merge request is tested as it would merge (a
merged-results pipeline, `refs/merge-requests/N/merge`) before it lands, and `hyperharness ship` waits for main to be
green. The cheap gates run on those pipelines; the heavy ones (build, SBOM, scans, signing) run on main and tags.

Merges to `main` run the same jobs up to and including `python-sign`, so signing and the SBOM are proven on every
merge; only `python-publish` and the release jobs need a tag.

### Where the files are

For release `X.Y.Z`:

- PyPI registry: `https://$STREAMSTACK_GITLAB_HOST/api/v4/projects/2122/packages/pypi/simple` (wheel and sdist)
- Generic Package Registry, package `streamstack-release`, version `X.Y.Z`:
  `streamstack-X.Y.Z-py3-none-any.whl`, `streamstack-X.Y.Z.tar.gz`, `SHA256SUMS`, `RELEASE-SHA256SUMS`,
  `RELEASE-SHA256SUMS.cose.sig`, `python-sbom.cdx.json`, and the package files `streamstack_X.Y.Z_all.deb`,
  `DEB-SHA256SUMS`, `DEB-SHA256SUMS.cose.sig`, `deb-provenance.intoto.json`
- Generic Package Registry, package `streamstack-apt`, version `stable`: the apt repository (below)

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

The Debian package is signed by a second route, because apt does not read Notation: the repository's `Release` file
carries an OpenPGP signature (`InRelease`, `Release.gpg`) made with the key `streamstack-apt-signing-key` in the same
Key Vault (`omlab-secrets`). `deb-sign` fetches it inside the signing Job's own pod, signs, checks the result with
`gpgv` and discards the key with the pod; it never reaches the runner or a log. The public half is committed as
`packaging/apt-public.asc` (fingerprint `F3AA 4E82 E815 D695 089D  B4D2 456B DF83 CAB0 C1E8`), the Notation
certificate as `packaging/signing-cert.pem`; both are public key material. The fingerprint is committed as
`packaging/apt-public.fingerprint`, which `deb-sign` reads, and a test keeps it equal to the committed key. It is not a
CI variable because a `KEY: <hex>` line in `.gitlab-ci.yml` trips gitleaks on the tag pipeline. To rotate the repository key, generate a new one, store it under the same secret name, commit the new
public key and its fingerprint in `packaging/apt-public.fingerprint` in one merge request.

`DEB-SHA256SUMS` lists the package and `deb-provenance.intoto.json`, an in-toto v1 statement (SLSA provenance v1) built
by `scripts/deb_provenance.py` from the CI job's own variables: the source repository and commit, the pipeline and job
that built it, the build script and the package's sha256 as the subject. Notation signs `DEB-SHA256SUMS`, so one
signature covers the package and where it came from.

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

## The Debian package and the apt repository

`scripts/build_deb.py` builds `streamstack_X.Y.Z_all.deb` from `make install DESTDIR=... PREFIX=/usr`, the same files
the self-contained `make install` puts down, with a `Depends` list of what the distribution provides
(python3, python3-gi, ffmpeg, v4l-utils and the audio tools), the udev and module configuration, and a `postinst` that
reloads udev. The build is reproducible: the same commit gives the same bytes (`SOURCE_DATE_EPOCH` is the commit time,
files are root-owned, mode-normalised and xz-compressed), and a test proves it.

The repository is a flat apt repository at one stable URL, served from the project's Generic Package Registry, so a
workstation's apt line never changes while the files behind it do. Every released package stays in it: `apt install
streamstack=0.0.11` is the rollback. The files are `Packages`, `Packages.gz`, `Release`, `InRelease`, `Release.gpg`
and every `.deb`. On a tag, `apt-publish` first checks the repository exactly as apt will (both signatures, `Release`
against `Packages`, `Packages` against every file) and only then uploads, `.deb` files first and the signed index last.
A published version is never overwritten.

Set up a machine (the project is private, so apt needs a GitLab token, kept in a root-only file):

```bash
H="$STREAMSTACK_GITLAB_HOST"            # your GitLab host
sudo install -d -m 0755 /etc/apt/keyrings
sudo gpg --dearmor -o /etc/apt/keyrings/streamstack.gpg packaging/apt-public.asc
echo "machine $H login streamstack password <a read_api or deploy token>" | sudo tee /etc/apt/auth.conf.d/streamstack.conf
sudo chmod 600 /etc/apt/auth.conf.d/streamstack.conf
echo "deb [signed-by=/etc/apt/keyrings/streamstack.gpg] https://$H/api/v4/projects/2122/packages/generic/streamstack-apt/stable ./" |
  sudo tee /etc/apt/sources.list.d/streamstack.list
sudo apt update && sudo apt install streamstack
```

Update with `sudo apt update && sudo apt install --only-upgrade streamstack`. Roll back to the previous release with
`sudo apt install --allow-downgrades streamstack=<older version>`; `apt-cache policy streamstack` lists what the
repository holds. `tests/test_apt_repo.py` runs all of this against a signed local repository with the real `apt-get`:
trust and install, update of an older install, rollback of the newest, refusal of an unsigned repository, of a changed
index, a changed package and a signature by another key.

Verify the package files without apt (`H` and the token as above; `PRIVATE-TOKEN` is the header GitLab expects):

```bash
B="https://$H/api/v4/projects/2122/packages/generic/streamstack-release/X.Y.Z"
for f in streamstack_X.Y.Z_all.deb DEB-SHA256SUMS DEB-SHA256SUMS.cose.sig deb-provenance.intoto.json; do
  curl -fsSL -H "PRIVATE-TOKEN: $TOKEN" -O "$B/$f"
done
# the trust store and policy from "Verify a release", with packaging/signing-cert.pem
notation blob verify --policy-name streamstack --signature DEB-SHA256SUMS.cose.sig DEB-SHA256SUMS
sha256sum --check DEB-SHA256SUMS
```

`DEB-SHA256SUMS` covers the package and the provenance statement; the statement's subject digest is the package's.
