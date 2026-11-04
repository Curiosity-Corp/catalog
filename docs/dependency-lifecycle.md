# Dependency lifecycle and ownership register

The role installs a long list of external tools and adds several package
repositories. [`dependency-register.yml`](dependency-register.yml) records, for each
one, who maintains it, where it comes from, its license, how updates arrive, how
the download is verified, what to do if it goes away, how critical it is, and when a
person last looked at it.

## What is in the register

* `tools`: one row per application contract in `roles/developer_workstation/defaults/main.yml`
  (GitHub-release tools and tools with their own installer).
* `sources`: one row per external host the role downloads from or adds as a package
  repository, found by scanning the role's tasks (`scripts/external_sources.py`).
* `installer_types`: defaults a tool row inherits (for example every GitHub-archive
  tool uses the GitHub asset digest and the same fallback), so rows stay short.

Fields: `owner`, `owner_type` (`individual`, `community`, `company`, `foundation`),
`source`, `license`, `update_channel`, `verification`, `fallback`, `criticality`
(`standard` or `security-critical`), `status` (`active`, `watch`, `at-risk`,
`abandoned`), `last_reviewed`, and an optional `note`.

## What the check enforces

`python scripts/check_dependency_register.py` runs in the `supply_chain` job on every
merge request:

* a new tool or a new host in the role without a register row fails, and so does a
  row for something the role no longer uses;
* every row must have every field, and a tool's installer and verification method
  must match the role's contract;
* an `abandoned` tool that is still installed fails; `watch` and `at-risk` rows are
  printed as alerts with their note, so they stay visible without blocking work;
* a `security-critical` row with no verification method fails;
* an apt repository must be verified by signature (`apt-signature`) and nothing may
  be piped into a shell.

At release time (tag pipelines) and on a schedule it also runs with
`--max-age-days 45 --online`: a row not reviewed in 45 days fails, and GitHub is
asked whether each upstream repository has been archived. An archived upstream whose
row is still `active` fails; one already recorded as `at-risk` is shown as an alert.
If GitHub cannot be reached the check says so and does not pass silently.

A monthly pipeline schedule is optional because every release runs the same check;
creating one is a project setting (CI/CD, Schedules) for a maintainer.

## Reviewing the register

At each release, and monthly if a schedule exists:

1. Run `python scripts/check_dependency_register.py --max-age-days 45 --online`.
2. For each alert, decide: keep (update `note`), replace, or drop from the role.
3. For rows you checked, set `last_reviewed` to today's date in a merge request.

What counts as a reason to change a row:

| Event | Action |
| --- | --- |
| Upstream repository archived | Set `status: at-risk` with a note, then replace or drop the tool in the same or the next release. Use the row's `fallback`. |
| License change to a source-available or restrictive license | Set `status: watch` with a note naming the open alternative (for example OpenTofu for Terraform) and decide whether to switch. |
| A vendor stops publishing checksums or signatures | Set `status: watch`; if no verification method remains, the check fails and the tool needs a pinned, verified download or removal. |
| Security advisory with no fix | Use the `fallback` (pin the previous release, use the distro package, or skip the optional tool). |

Prefer distro or vendor-repository packages with signature verification where the
version is good enough; the role's apt repositories use `signed-by` keyrings and the
register requires that.

## Current alerts

The check prints these today. They are decisions for the maintainers, not failures:

* `filebrowser` and `talhelper` have archived upstream repositories (read-only, no
  further fixes). Decide whether to drop them from the role defaults or replace them.
* `terraform` and `vault` use the source-available BUSL license; OpenTofu is already
  installed by the role, and OpenBao is the open fork of Vault.
* Several git clones, the role's own `pip`/`npm` installs, one downloaded WeeChat script
  and the Private Internet Access installer are not pinned by checksum. Pin them when
  they next change.
