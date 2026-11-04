# Supply chain: build, scan, attest, verify

This page describes how the collection is built, what is checked on every merge
request and release, and how anyone can check an artifact themselves. It covers
the collection's own build. What the role installs on a workstation when it runs
is recorded separately in the [dependency register](dependency-lifecycle.md).

## What runs where

| Control | Where | Fails the pipeline when |
| --- | --- | --- |
| Pinned CI toolchain | `ci/requirements.txt`, `ci/requirements.yml`, `image:` in `.gitlab-ci.yml` | a pin is missing or loosened (`tests/test_supply_chain.py`) |
| Reproducible, inspected artifact | `collection` and `artifact_verify` jobs, `scripts/verify_collection_artifact.py` | contents, metadata, `FILES.json`, a clean rebuild or a clean-controller install disagree |
| Dependency-complete SBOM | `sbom` job, `scripts/build_collection_sbom.py`, `scripts/verify_sbom.py` | the SBOM does not match the artifact or lacks a component category |
| Vulnerability audit of the CI toolchain | `dependency_audit` job, `scripts/run_dependency_audit.py` | a pinned package has a known advisory with no unexpired override |
| Secret detection | GitLab `secret_detection` (merge requests, branches) and `secret_scan` (everything, tags included) | a credential-shaped string is committed |
| Provenance and checksums | `provenance` job, `scripts/release_evidence.py` | the files in `evidence/` disagree with each other or with the commit |
| Dependency register | `supply_chain` job, `scripts/check_dependency_register.py` | the role installs or contacts something the register does not describe |

Run the local equivalents with `make supply-chain` (tests, register, audit, secret
scan) and `make verify-collection` (build, rebuild, install into an empty
collections path).

## Toolchain lock and base image

* `ci/requirements.in` lists the direct tools (ansible-core, ansible-lint,
  yamllint, pyyaml, pytest, pip-audit). `ci/requirements.txt` is the resolved
  closure, one exact `name==version` per line, installed with `pip install -r`.
  The pipeline never runs an unpinned `pip install`.
* `ci/requirements.yml` pins the Ansible collections CI installs. The published
  collection keeps its `>=9.0.0` range in `galaxy.yml` and `requirements.yml`,
  because downstream users have their own constraints.
* The CI image is `python:3.13-slim@sha256:...`: the tag stays for readers, the
  digest of the multi-architecture index is what is pulled.
* Updates: Renovate (`renovate.json`) proposes single-line bumps for the lock
  file (grouped monthly), the collection pins and the image digest, each as a
  reviewable diff. Merge one only after its pipeline is green. To re-resolve the
  whole closure run `make lock-ci` (needs podman or docker); it resolves inside
  the CI image so the pins are ones that image can install.
* Changing the Python version means changing the image line, running
  `make lock-ci`, and reviewing the diff together.

## Findings: severity and overrides

`pip-audit` reads the PyPI advisory database. That database does not carry a
severity score, so every finding is treated as blocking. Do not invent a severity.

* A finding with no override fails `dependency_audit`. The fix is to bump the
  package (Renovate usually already has the merge request open).
* If a bump is not possible, add an entry to `ci/vulnerability-exceptions.yml`
  in a merge request: advisory id, package, a reason that says why it is not
  reachable from CI, an `expires` date at most 90 days away, and the GitLab
  username of the maintainer who approved it. The approver is a maintainer other
  than the author. An expired or incomplete override fails the job again, so an
  override cannot be forgotten.
* If the advisory service cannot be reached the job exits 3 and shows as a
  warning ("allowed to fail" on exit code 3 only). Rerun it; do not release on a
  warning you did not look at.
* Secret findings are never overridden in place. Remove the value, rotate the
  credential if it was ever real, and only then, for a reviewed synthetic test
  fixture, add `secret-scan: allow` on that line.

## Secret scanning

GitLab's Secret-Detection template runs on merge requests and branch pipelines
but not on tag pipelines, and a release is a tag. `scripts/scan_secrets.py` is a
small scanner with no dependencies that runs on every pipeline, tags included. It
flags private key blocks, GitHub, GitLab, AWS and Slack token shapes, JWTs and a
password inside a URL, and it prints `path:line: rule` only, never the matched
text. `tests/test_supply_chain.py` proves it with synthetic values assembled at
run time, so the repository holds no secret-shaped fixture.

## SBOM

`collection.cdx.json` is CycloneDX 1.5. Every component carries a
`curiosity:category` property so consumers can tell what kind of thing it is:

| Category | What it is |
| --- | --- |
| `source-file` | each file in the artifact, with its SHA-256 |
| `collection-dependency` | direct collection dependencies, with the resolved version CI used |
| `ci-tool`, `ci-image` | the pinned Python toolchain and the base image |
| `runtime-release-download` | GitHub-release tools the role downloads when it runs |
| `runtime-other-contract` | tools with their own installer (vendor release, go module, npm, pipx) |
| `runtime-package-source` | apt repositories the role adds |
| `runtime-download-source`, `external-installer`, `runtime-package-manager`, `runtime-source-clone` | other hosts the role fetches from, upstream installer scripts, pip/npm installs and git clones |

The runtime components come from a static scan of the role (`scripts/external_sources.py`),
so they cannot drift from the code. The scan reports hosts that appear in tasks,
including tasks skipped on some machines; it over-reports rather than hides a source.
`scripts/verify_sbom.py` checks that the SBOM matches the artifact's digest and file
list and that no required category is empty. The SBOM is published beside the
artifact, and `provenance` lists it as a subject.

## Provenance and release evidence

The `provenance` job writes `evidence/`, which also holds copies of the artifact and the
SBOM so `sha256sum --check SHA256SUMS` works inside the directory:

* `SHA256SUMS`, checkable with `sha256sum --check SHA256SUMS`;
* `toolchain.json`: Python and package versions, lock-file digests, CI image;
* `provenance.intoto.json`: an in-toto Statement v1 with SLSA provenance v1
  fields. Its subjects are the artifact and the SBOM; it records the commit, ref,
  pipeline, job URL, runner, the lock-file digests and the SBOM digest.

The statement is **unsigned in CI** (`"signed": false`). Signing happens on the
key holder's machine, never in CI, so no signing key lives in the pipeline; see
[release signing](release-signing.md). GitLab Runner also writes
`artifacts-metadata.json` for the job (`RUNNER_GENERATE_ARTIFACTS_METADATA`).

## Verify an artifact yourself

```bash
# 1. The files agree with each other (and with the commit you expect).
python scripts/release_evidence.py verify evidence --commit <full-commit-sha>
(cd evidence && sha256sum --check SHA256SUMS)    # the artifact and SBOM are inside evidence/

# 2. The artifact contains only intended files, is internally consistent,
#    rebuilds to the same digest, and installs into an empty collections path.
python scripts/verify_collection_artifact.py curiosity-workstation-<version>.tar.gz --rebuild --install-check

# 3. The SBOM describes this artifact.
python scripts/verify_sbom.py collection.cdx.json --archive curiosity-workstation-<version>.tar.gz
```

The rebuild needs a checkout of the tagged commit. With a detached signature and
the owner's fingerprint, step 1 also checks the signature (see release signing).

## What this does not cover

* It does not prove the runtime downloads the role makes on a workstation are
  safe on the day they run; the register records how each one is verified.
* The SBOM lists what the role will fetch, not exact versions: most tools track
  the latest release when the role runs.
* The provenance is not a SLSA level claim. It records the inputs; an unsigned
  statement protects against accident and drift, not against a compromised runner.
