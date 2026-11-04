# Upstream collaboration and maintainer path

Reducing maintenance debt means sending generic fixes to the projects this
collection depends on, instead of keeping private patches. This page is the
inventory and the rules. It records no outreach, because none has been done:
contacting an upstream project is a decision for a maintainer.

## Inventory of third-party code in the collection

| Item | Where | License | Local change? | Upstream status |
| --- | --- | --- | --- | --- |
| `wee_most.py` | `roles/developer_workstation/files/wee_most.py` | GPLv3 | Described in `THIRD_PARTY.md` as "based on" upstream `wee-most`; the difference from upstream has **not been measured** | Not reviewed |
| `teams.py` | `roles/developer_workstation/files/teams.py` | GPLv3 | Not compared with an upstream | Not reviewed |
| `wee-slack` | installed from its upstream repository at run time by `tasks/weechat.yml` | upstream | None: installed unmodified | Not applicable |
| StreamStack | `roles/developer_workstation/files/streamstack/` | Own README and records | Developed alongside the role | Own project, not an upstream dependency |

`THIRD_PARTY.md` is the license record; this table adds what to do next. Add a
row whenever a vendored or patched file enters the repository, and a
[decision record](../decisions/README.md) explaining why a local patch remains
when an upstream fix is not possible.

## For each patched item

A maintainer fills in, in the item's own issue:

1. The exact difference from the upstream version (a diff against a named
   upstream commit or release).
2. Whether the difference is generic. Only generic fixes go upstream; anything
   specific to one deployment stays local and is named as such.
3. The upstream project's contribution norms: where issues go, the review
   process, the license of contributions, and the release cadence.
4. The link to the upstream issue or pull request, once a person has opened it,
   and whether it was merged.
5. A compatibility note: how fast upstream releases, and what breaks here when
   it does.

Preserve contributor credit: a change made by someone else keeps their
authorship, and links to merged upstream work are kept as plain links.

## The maintainer path

Becoming a maintainer or core contributor of an upstream project is earned by
sustained, useful work and granted by that project. It is not a number this
project can claim.

- Choose **one** upstream project that the collection really depends on.
- Read its contributing guide and talk to its maintainers about what they need
  and what responsibility means there, in their channels, as a person.
- Work through the project's own process: issue triage, tests, documentation,
  review of others' changes, and fixes. Do not open pull requests to make a
  record.
- Record a role or a merged pull request in this repository only after the
  upstream project has granted it, and link the upstream page.

## What this page does not do

It does not contact anyone and it does not decide which project to work with.
Automation here only prepares the inventory.
