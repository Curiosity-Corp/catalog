# 0007: The dependency register is curated data held to the declarations by catalog-tools

- Status: accepted
- Date: 2026-11-26
- Deciders: catalog maintainers
- Affects: the repository's tooling, CI (`tools` job) and docs
  (`ci/dependency-register.yml`, `docs/dependency-register.md`); no variable,
  tag, profile, platform or schema changes in this record
- Issues: catalog#51; builds on catalog#21 ([decision 0006](0006-release-sbom.md))

## Context

The SBOM (decision 0006) says what the collection pulls in. It does not say
who maintains each upstream, what its licence is, how it is verified, when its
support ends, or what we do when it is archived or relicensed. A
hand-maintained list of that would drift from the roles the first time a role
gains a download.

## Decision

- The lifecycle and ownership facts live in one curated file,
  `ci/dependency-register.yml`, one entry per upstream. Owners are roles from
  GOVERNANCE.md, never people. Each support status and end date cites the
  vendor's own page and the day it was read.
- The set of dependencies is not curated: `catalog-tools register check`
  derives it from `ci/sbom-sources.yml` (already held to the roles),
  `galaxy.yml`, `ci/requirements.{in,yml}`, `apt_vendor_repos` and
  `latest_github_repositories`, and fails when an entry is missing or no
  longer matches what its declaration names. The `tools` job runs it on every
  pipeline.
- `catalog-tools register alerts` turns the recorded facts into alerts
  (abandoned, moved, support ending within `warn_days`, security-critical but
  verified only by HTTPS, review overdue). Release tag and scheduled pipelines
  run it with `--strict`, which fails on an alert without a tracking issue and
  on a review older than `review_interval_days`. MR and main pipelines only
  print, so the passing of time never turns main red.
- It is a `catalog-tools` subcommand (decision 0005), reusing the SBOM's
  readers of the same files.

## Safety impact

Nothing changes on a research machine. The new file is data; no role,
default, variable or tag changes. What could go wrong is a red `tools` job
when a declaration changes without a register update; the fix is a register
entry.

## Alternatives considered

- Live upstream checks in CI (GitHub archive flags, endoflife APIs): they make
  every pipeline depend on outside services and rate limits, and many
  upstreams publish no machine-readable lifecycle. The review reads the
  vendor's own page instead and records it with a date.
- Fields on `ci/sbom-sources.yml` entries: that file covers runtime sources
  only, not the collection and CI dependencies or the GitHub release list, and
  its entries are per-URL where ownership is per-upstream.

## Consequences

- A merge request that adds, removes or repoints a dependency also updates
  the register; `register check` names the entry to change.
- The register is only as current as its last review: release tags enforce
  one every `review_interval_days`, and every recorded fact carries the page
  and the day it was read.
- Alerts become work items (catalog#171 for the first review); a fix that
  changes a role goes in its own merge request.
