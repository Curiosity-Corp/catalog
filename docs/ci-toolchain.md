# CI toolchain

CI results must not change because a tool released a new version overnight
(catalog#22). Every input that decides a CI result is declared in git, pinned,
and moved only by a reviewed merge request.

## What is pinned, and where

| Input | Declared in | Pin | Moved by |
| --- | --- | --- | --- |
| Job images (`python:3.15`, `-slim`; `python:3.13-trixie`, `-slim-trixie`) | `.gitlab-ci.yml` | tag plus `@sha256:` digest of the multi-arch index, pulled from ECR Public's Docker Library mirror (catalog#97) | Renovate `docker` manager: digest bumps grouped as "container image digests", tag bumps one MR each; `-trixie` tags are held at 3.13 (below) |
| Rust image for `tools` (`rust:1-bookworm`) | `.gitlab-ci.yml` | tag plus `@sha256:` digest; the crates are pinned by `tools/Cargo.lock` (`cargo --locked`) | Renovate `docker` manager (digest) and `cargo` manager |
| Python tooling (ansible-core, ansible-lint, check-jsonschema, yamllint, PyYAML, pytest, uv, reuse with charset-normalizer) | `ci/requirements.in` | exact `==` for each direct dependency | Renovate `pip-compile` manager: bumps the pin and regenerates the lock in the same MR |
| Their transitive dependencies | `ci/requirements.txt` | full lock with SHA-256 hashes, installed with `pip install --require-hashes` | Renovate lock file maintenance (weekly MR) |
| pip | the job image | the image digest | the image digest bump |
| Ansible collections (community.general) | `ci/requirements.yml` | exact version | Renovate `ansible-galaxy` manager |

The `-trixie` images stand in for a Debian 13 laptop (`debian13-quickstart`,
`debian13-vm-run`, `quickstart-check-preview`), so they run Debian 13's system
Python, 3.13, whatever the other jobs use: trixie's `python3-apt` ships
`apt_pkg` built for CPython 3.13 only, and under 3.15 `import apt, apt_pkg`
fails (catalog#184). A `renovate.json` rule (`matchCurrentValue: /-trixie$/`,
`allowedVersions: /^3\.13(\.\d+)?-/`) keeps those tags on 3.13 while their
digests still move; move them only when Debian's own `python3` does.
`tests/test_public_contract.py::test_debian13_laptop_jobs_use_debian13_python`
fails when a job on a `-trixie` image uses another minor or the rule is gone.

`tests/test_public_contract.py::test_ci_toolchain_is_locked` fails when a job
image has no digest, when a `pip install` bypasses the lock, when a direct pin
is missing from the lock, or when a CI collection is not pinned exactly.

The collection's own dependency range for users stays in `galaxy.yml` and
`requirements.yml`; `ci/requirements.yml` pins only what CI tests with.

Only one job reaches galaxy.ansible.com (catalog#174, catalog#182). The
first-stage `galaxy-collections` job runs `scripts/fetch-collections.sh`,
which fetches everything CI needs once per pipeline and passes it on as
artifacts:

| Directory | What | Used by |
| --- | --- | --- |
| `.ci-collections` | `ci/requirements.yml`, the exact pin | every job that extends `.python_setup` |
| `.ci-collections-floor` | community.general at the `galaxy.yml` floor, alone on its path | `collection-floor` |
| `.ci-collections-user` | `requirements.yml` downloaded as a user resolves it today (tarballs and a `requirements.yml`) | `quickstart-check-preview`, `debian13-quickstart`, `debian13-vm-run`, which run the guide's install with `--offline` |

Galaxy resets connections in bursts (Errno 104). Each fetch gets five tries,
each bounded to 30 s per Galaxy request and 150 s in all, 10, 20, 40 and 80 s
apart; a timeout says galaxy.ansible.com did not answer. If all five fail, the
job fails alone and GitLab retries it (`retry: max: 2`) before any other job
has started. The GitLab cache keeps the two pinned directories, keyed on
`ci/requirements.yml` and `galaxy.yml`; a cached copy that passes
`ansible-galaxy collection verify --offline` is used without asking Galaxy.
The user download is never cached, so it follows new releases.

Every other job uses the artifact after `verify --offline` finds the pinned
versions with every file matching its manifest, and fails, never fetches,
when it does not: a job with `needs:` must list `galaxy-collections`. Two
guards keep it that way. `tests/test_galaxy_fetch_isolation.py` fails when any
other job, or a script it runs, names galaxy.ansible.com or runs
`ansible-galaxy collection install` or `download` without `--offline`, or
runs ansible-lint without `--offline` (it installs `requirements.yml` itself
at start otherwise); and the
pipeline sets `ANSIBLE_GALAXY_SERVER` to a closed local port for every job but
the fetch job, so a live fetch the scan cannot see fails at once.
`tests/test_fetch_collections_retry.py` replays the reset against the fetch
script and proves it retries through it, stops after its bounded tries and
skips the network on a verified cache.

Because every other job tests the exact pin, the `collection-floor` job
(`scripts/check-collection-floor.sh`) tests the other end of the range: on the
floor the fetch job installed (alone on the collection path, checked against
its manifest) it fails when a community.general module, filter, test or
lookup the roles name is missing or deprecated there, or when
`requirements.yml` states a different floor. Keep the floor on the oldest
major that still receives backports (community.general backports to its three
latest `stable-x` branches, ansible-collections/community.general#582); raise
it in `galaxy.yml` and `requirements.yml` together. Run both locally from the
repository root with `scripts/fetch-collections.sh` and then
`scripts/check-collection-floor.sh .ci-collections-floor`.

If Galaxy outages still cost pipelines after this, the next step is a
cluster-served mirror or pull-through proxy, decided with the infrastructure
unit; that is not built here.

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
