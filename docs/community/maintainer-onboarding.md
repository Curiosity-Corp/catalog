# Review areas and maintainer onboarding

[GOVERNANCE.md](../../GOVERNANCE.md) says who approves what. This page says
which parts of the repository need which kind of review, and how a student or
researcher can grow into a maintainer without being handed root-level
automation on day one.

## Review areas

A review area is a set of paths where a second pair of eyes asks specific
questions. Each area has a **lead** (the maintainer who answers first) and a
**backup** (who answers when the lead has not replied within the 7-day target
in GOVERNANCE.md). Names are kept on the hosting platform, not in this file:
the `CODEOWNERS` entries and the project's member list are the record.

| Area | Paths | Second reviewer asks | Needs two maintainers |
| --- | --- | --- | --- |
| Update control plane | `roles/developer_workstation/tasks/update-*.yml`, `managed-updates.yml`, `latest-releases.yml`, `roles/developer_workstation/files/curiosity-update-summary` | Can this install without a verified digest, skip rollback, or leak a host value? | Yes |
| Privileged system changes | `roles/developer_workstation/tasks/firewall.yml`, `keyboard*.yml`, `locale.yml`, `user-*.yml`, `apt-keyring.yml`, `repos*.yml` | Does it run as root on an unrelated machine, and is it gated by a profile or an explicit variable? | Yes |
| Defaults and public API | `roles/*/defaults/main.yml`, `playbooks/`, `galaxy.yml`, tags, profiles | Is it a breaking change? Is there a [decision record](../decisions/README.md)? | Yes |
| Identity, certificates, secrets | `roles/developer_workstation/tasks/keycloak-sso.yml`, `ziti*.yml`, `bitwarden-desktop.yml`, and `custom_ca_certificates` handling | Does any private value reach the repository or a log? | Yes |
| CI and supply chain | `.gitlab-ci.yml`, `Makefile`, `renovate.json`, `requirements.yml`, `scripts/` | Are inputs pinned and is a release artifact still reproducible? | Yes |
| Security documents | `SECURITY.md`, `docs/threat-model.md`, `docs/vulnerability-response.md` | Does the text still match what the code and the process do? | Yes |
| Documentation, examples, tests only | `docs/`, `examples/`, `tests/` (no code change) | Is it true, tested where it can be, and free of private names? | No: one maintainer |

The first column is the working list. A test (`tests/test_community_docs.py`)
fails if a path named in the second column no longer exists, so the list cannot
silently go stale.

## Security and release approval

Unchanged from [GOVERNANCE.md](../../GOVERNANCE.md#who-approves-what): a
security fix needs two maintainers where available, a release needs an approver
who did not write the release commit, and breaking changes need a decision
record. A student or researcher maintainer takes part in these as a reviewer
before they approve alone.

## Onboarding path for a student or researcher maintainer

The aim is a real maintainer after about six months of steady, small work, with
nobody carrying the root-level code alone. Each step is a **reviewed
merge request or recorded review**, not a quiz.

1. **Contribute** (month 0 to 1): two merged changes from the
   [good first issue backlog](good-first-issues.md), one of them a test.
2. **Triage** (month 1 to 3): answer new issues within the 7-day target, label
   them, and reproduce one bug on a disposable VM, writing down the steps.
3. **Review** (month 2 to 4): review at least five merge requests in the
   documentation, examples and tests area. Reviews are comments on the merge
   request; a maintainer replies to each.
4. **Shadow a release** (month 3 to 5): follow the
   [release checklist](../release-checklist.md) beside the release approver,
   run the disposable VM test for one profile, and write the evidence entry.
5. **Review a privileged change** (month 4 to 6): second-review one change in a
   two-maintainer area, with a maintainer present.
6. **Propose** (month 6): existing maintainers decide by consensus whether to
   grant the Maintainer role, as GOVERNANCE.md says. Record the decision in a
   [decision record](../decisions/README.md) and update the hosting platform's
   member list and `CODEOWNERS`.

Maintainers also rotate: any maintainer can step back at any time by saying so
in an issue, and the role is reviewed at the annual review in GOVERNANCE.md.

## What this page does not do

It does not name anyone. Assigning real people to review areas, adding the
`CODEOWNERS` entries and naming backups are the maintainers' decisions, made on
the hosting platform.
