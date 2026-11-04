# Governance

Curiosity Workstation is maintained by Curiosity Research Corporation, a
non-profit, as free software under the Apache-2.0 license. It is an
independent project: nothing here implies endorsement by, or affiliation with,
any hardware, software or cloud vendor whose product the role can configure.

This page turns the contributor policies into working practice. It is written
for a small volunteer team, so it prefers short rules that one person can follow
over process that needs a committee.

## Roles

| Role | Who | What they do |
| --- | --- | --- |
| Contributor | Anyone | Opens issues and merge requests. Follows [CONTRIBUTING.md](CONTRIBUTING.md) and the [code of conduct](CODE_OF_CONDUCT.md) |
| Maintainer | People with the Maintainer or Owner role on the hosting project | Reviews and merges, triages security reports, cuts releases |
| Release approver | A maintainer who did not write the change being released | Signs off the [release checklist](docs/release-checklist.md) |

The current maintainers are the members of the repository with the Maintainer
or Owner role. The hosting platform is the single source of that list, so it
cannot drift out of date in a file. Maintainers do not publish personal contact
details in the repository.

## Maintainer responsibilities

A maintainer commits to:

- answer a new issue or merge request with a first reply within 7 days, even if
  the reply is "not now" (this is a target for volunteers, not a service level);
- keep the default branch releasable: CI green, no private material, no
  unreviewed default changes;
- apply the code of conduct the same way to everyone, including themselves;
- say so in the issue when they step away for more than two weeks, so work is
  reassigned rather than left waiting.

## Review expectations

- Every change to the default branch goes through a merge request with passing
  CI. Nobody merges their own change on the strength of their own review.
- A change that touches defaults, a public variable, a tag, a profile, a
  supported platform, or a security control needs a second maintainer, or the
  author waits for a maintainer who did not write it.
- Documentation, test-only and typo fixes need one maintainer.
- Reviewers check what the merge request template asks: safety impact on an
  unrelated research machine, tests, documentation, changelog, migration notes.
- A reviewer who is unsure says so. "I could not test this on that hardware" is
  a valid review comment and becomes a note in the merge request.

## Backup contact route

If the maintainer you normally deal with does not answer within the target
above:

1. Mention another maintainer in the issue or merge request.
2. For a security report, follow the fallback route in
   [SECURITY.md](SECURITY.md) and the
   [vulnerability response runbook](docs/vulnerability-response.md); it names
   the acknowledgement clock and who steps in when the first maintainer is
   away.
3. For a conduct concern, follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Who approves what

| Decision | Approver | Evidence kept |
| --- | --- | --- |
| Security report triage and fix | Two maintainers where available, one at minimum; the reporter is informed | Private advisory, then the changelog entry and advisory link |
| Disclosure date | The maintainer handling the report, agreed with the reporter | Advisory |
| Breaking change (removed or renamed variable, changed default, dropped platform or profile) | Two maintainers, after a decision record | `docs/decisions/` record, changelog "Changed" or "Removed" entry, migration note |
| New default-on behaviour | Two maintainers, after a decision record | Decision record |
| Release | A release approver who did not author the release commit | [Release checklist](docs/release-checklist.md) completed, evidence log entry |
| Emergency security release | The maintainer handling the report, with a second maintainer informed before publishing | Changelog entry, advisory, evidence log |
| Adding or removing a maintainer | Existing maintainers, by consensus | Hosting platform membership |

## Decision records

Changes to defaults and to the public surface (variables, tags, profiles,
supported platforms, the update manifest schema) are recorded as short decision
records in [`docs/decisions/`](docs/decisions/README.md). A decision record is
written before or with the merge request, not afterwards.

## Annual review

Maintainers review this page, [SECURITY.md](SECURITY.md), the
[threat model](docs/threat-model.md) and the
[telemetry and evidence policy](docs/telemetry-and-evidence.md) once a year, and
record the review here. A test fails if the recorded date is more than 400 days
old, so a missed review shows up in CI.

| Review date | Reviewers | Outcome |
| --- | --- | --- |
| 2026-11-04 | Initial version, written by an automated agent for maintainer review | Not yet reviewed by a human maintainer; a maintainer should add a line when they have |

Next review due: 2027-11-04
