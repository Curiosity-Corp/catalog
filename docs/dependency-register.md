# Dependency register

Labs adopting the collection need to know which upstream projects it depends
on, who maintains each one, and what happens when one is archived, changes its
licence or reaches end of life. [`ci/dependency-register.yml`](../ci/dependency-register.yml)
answers that for every external dependency (catalog#51,
[decision 0007](decisions/0007-dependency-register.md)).

## What it records

One entry per upstream, covering one or more declarations:

| Field | What it says |
| --- | --- |
| `upstream`, `source` | Who maintains it (an organisation or project, never a person) and its own page |
| `declarations` | The declarations it covers, each with what the declaration names (URL, repository, range) |
| `licence`, `licence_source` | SPDX expression or `proprietary`, and where it was read |
| `owner` | Our owner role (`owner_roles`: role-maintainer, ci-maintainer, release-approver; [GOVERNANCE.md](../GOVERNANCE.md)) |
| `update_channel` | How new versions reach a host or CI |
| `verification` | `package-manager-signature`, `vendor-repository-signature`, `vendor-signature`, `pinned-checksum` or `tls-only` |
| `security_critical` | Runs as root, handles credentials, or controls network trust |
| `support` | `status`, the vendor page it was read from (`evidence`), the day it was read (`checked`), and published end dates (`ends`) |
| `replacement` | The fallback when the upstream is archived, relicensed or ends support |
| `alert_tracked_in` | The issue that tracks the entry's open alerts |

Signatures from the distribution or a package manager (APT, Snap, Flatpak,
browser stores) are preferred over vendor checksums, and a vendor checksum or
signature over HTTPS alone. A `security_critical` entry verified only by
`tls-only` raises an alert until the role checks something stronger.

## Where the declarations come from

The register is curated, but the list of dependencies is not.
`catalog-tools register check` reads it from what the checkout already
declares, and fails when the register and the declarations disagree:

| Declaration id | Read from | What it names |
| --- | --- | --- |
| `source:<name>` | [`ci/sbom-sources.yml`](../ci/sbom-sources.yml) (held to the roles by the SBOM, [docs/sbom.md](sbom.md)) | `url`, the `url_var` default, `reference_url`, or the package modules |
| `collection:<name>` | `galaxy.yml` `dependencies` | the declared range |
| `ci-collection:<name>` | `ci/requirements.yml` | its Galaxy page |
| `ci-python:<name>` | `ci/requirements.in` (direct pins; the transitive packages of `ci/requirements.txt` are owned through the direct pin that brings them in) | its PyPI page |
| `vendor-repo:<key>` | `apt_vendor_repos` in the role defaults | the repository URIs |
| `github-release:<key>` | `latest_github_repositories` in the role defaults | the GitHub repository |

`check` fails when a declaration is missing from the register, an entry names
a declaration that no longer exists, a declaration now names something else
(for example a new Node.js line or a moved repository), or an entry is
malformed (unknown owner role, a missing citation, an invalid date). The
`tools` CI job runs it on every pipeline; `make register-check` runs it
locally.

## Alerts

`catalog-tools register alerts` (`make register-alerts`) lists:

- `abandoned`: the upstream is archived, unmaintained or past end of life;
- `moved`: the upstream moved and the declaration still names the old home;
- `support-ending`: a published end date is past or within `warn_days`;
- `unverified-security-critical`: see above;
- `review-overdue`: `reviewed` is older than `review_interval_days`.

Every pipeline prints them. With `--strict`, which release tag pipelines (and
any scheduled pipeline a maintainer adds) use, the job fails on an alert whose
entry has no `alert_tracked_in` issue, and on an overdue review, which only a
review clears.

Alerts from the 2026-11-26 review are tracked in catalog#171.

## Reviewing it

Monthly, and at every release (the [release checklist](release-checklist.md)
has the step):

1. Run `make register-alerts`.
2. For each entry, open `support.evidence` and record what the vendor's page
   says that day: status, end dates, a licence change. Set `support.checked`.
3. For a new alert, open or extend a tracking issue, set `alert_tracked_in`,
   and follow the entry's `replacement` plan in a merge request.
4. Set `reviewed` to the day of the review.

When a role gains a dependency, declare it where it belongs (usually
`ci/sbom-sources.yml`, which the SBOM already requires) and add a register
entry in the same merge request; `register check` names what is missing.
