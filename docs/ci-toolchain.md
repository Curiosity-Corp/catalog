# CI toolchain

CI results must not change because a tool released a new version overnight
(catalog#22). Every input that decides a CI result is declared in git, pinned,
and moved only by a reviewed merge request.

## What is pinned, and where

| Input | Declared in | Pin | Moved by |
| --- | --- | --- | --- |
| Job images (`python:3.13`, `-slim`, `-trixie`) | `.gitlab-ci.yml` | tag plus `@sha256:` digest of the multi-arch index, pulled from ECR Public's Docker Library mirror (catalog#97) | Renovate `docker` manager: digest bumps grouped as "container image digests", tag bumps one MR each |
| Rust image for `tools` (`rust:1-bookworm`) | `.gitlab-ci.yml` | tag plus `@sha256:` digest; the crates are pinned by `tools/Cargo.lock` (`cargo --locked`) | Renovate `docker` manager (digest) and `cargo` manager |
| Python tooling (ansible-core, ansible-lint, check-jsonschema, yamllint, PyYAML, pytest, uv) | `ci/requirements.in` | exact `==` for each direct dependency | Renovate `pip-compile` manager: bumps the pin and regenerates the lock in the same MR |
| Their transitive dependencies | `ci/requirements.txt` | full lock with SHA-256 hashes, installed with `pip install --require-hashes` | Renovate lock file maintenance (weekly MR) |
| pip | the job image | the image digest | the image digest bump |
| Ansible collections (community.general) | `ci/requirements.yml` | exact version | Renovate `ansible-galaxy` manager |

`tests/test_public_contract.py::test_ci_toolchain_is_locked` fails when a job
image has no digest, when a `pip install` bypasses the lock, when a direct pin
is missing from the lock, or when a CI collection is not pinned exactly.

The collection's own dependency range for users stays in `galaxy.yml` and
`requirements.yml`; `ci/requirements.yml` pins only what CI tests with.

## Exceptions

- `research-profile` runs `python:${PYTHON_VERSION}-slim` across a matrix of
  interpreters, so its tag is a variable and cannot carry a digest; Renovate
  does not see it. It still installs the hash-checked lock. Raise the matrix by
  hand when a new CPython ships.
- `platform-packages` runs inside each distro it probes (`${IMAGE}`, a matrix
  of `ubuntu:22.04` to `debian:13`), so its image is a variable and carries no
  digest. The probe refreshes apt's lists inside the job and the evidence file
  records the archive answers, not the image, so the image date does not decide
  the result (docs/platform-differences.md). Add a release to the matrix by hand.
- `cloud-init-schema` installs cloud-init's own `requirements.txt` from the
  release tarball. The tarball is checked by SHA-256, but its unpinned
  requirements are upstream's choice; the job runs on merge requests only and
  touches nothing but the example user-data.

## Updating by hand

Renovate is the normal path. To bump something yourself, change it in one MR:

1. Images: resolve the new digest with
   `crane digest public.ecr.aws/docker/library/<image>:<tag>` (or
   `skopeo inspect --format '{{.Digest}}' docker://public.ecr.aws/docker/library/<image>:<tag>`)
   and replace both tag and digest on every line that uses the image.
2. Python tooling: edit the pin in `ci/requirements.in`, then regenerate the
   lock with the command in the header of `ci/requirements.txt`:
   `uv pip compile ci/requirements.in --universal --python-version 3.13 --generate-hashes -o ci/requirements.txt`.
   `--universal` keeps the lock valid for every interpreter CI uses (3.13 to 3.15).
3. Collections: edit the version in `ci/requirements.yml`.

Review the lock diff like code: a new package, a changed source, or a hash
change on an unchanged version is a reason to stop and look.

## Release evidence

The `provenance` job writes `ci-toolchain.txt`: the job image with its digest,
the Python version, the SHA-256 of the lock and collection files, `pip freeze
--all` and the installed collections. Its checksum goes into
`collection.sha256` next to the artifact's. Copy it into the dated evidence log
for the release (`docs/release-checklist.md`).

The same toolchain is listed component by component (`ci-tool`) in the
release SBOM, `collection.cdx.json` (`docs/sbom.md`).
