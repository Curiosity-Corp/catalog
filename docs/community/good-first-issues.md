# Good-first and help-wanted backlog

Small, real tasks for new contributors. Each one has context, acceptance
criteria and test guidance, and none of them needs private infrastructure.

## How the backlog works

- **Labels.** `good first issue` is for a task one person can finish in an
  evening, with a test or a link check. `help wanted` is bigger or needs a
  machine the maintainers lack. Add the area labels (`area-docs`,
  `area-testing`, `area-accessibility`, `researcher-ux`) so people can filter.
- **Shape.** An issue uses the [good-first template](../../.gitlab/issue_templates/Good_first_issue.md):
  context, acceptance criteria, test guidance, and what it depends on.
- **Keeping at least ten open.** When fewer than ten scoped issues are open,
  a maintainer writes more from real gaps they have just seen (for example the
  "not done yet" list in a guide), not from filler.
- **Replies.** Reply to a first-time contributor within the 7-day target in
  [GOVERNANCE.md](../../GOVERNANCE.md). Ask for changes by saying what to
  change and why; a requested change is not a rejection.
- **Credit.** Name each merged contributor, with a link to their change, in the
  release notes in [`CHANGELOG.md`](../../CHANGELOG.md), and keep a link in the
  monthly evidence record. See
  [contributor cohorts](contributor-cohorts.md) for what counts.
- **Monthly stale review.** On the first working day of each month, a
  maintainer lists open issues untouched for 90 days and for each one: closes it
  with a reason, rewrites it so it is workable, or asks the reporter. Record the
  date in the monthly evidence file ([template](../evidence/monthly-template.md)).

## The backlog on 2026-11-04

| Issue | Task | Kind |
| --- | --- | --- |
| [#127](https://git.developerdojo.org/Curiosity-Corp/catalog/-/issues/127) | Constraints-file example for the Python research profile | good first issue, docs |
| [#128](https://git.developerdojo.org/Curiosity-Corp/catalog/-/issues/128) | Troubleshooting page for the commonest failures | good first issue, docs |
| [#129](https://git.developerdojo.org/Curiosity-Corp/catalog/-/issues/129) | Common keyboard layout examples, checked by tests | good first issue, accessibility |
| [#130](https://git.developerdojo.org/Curiosity-Corp/catalog/-/issues/130) | Researcher glossary | good first issue, docs |
| [#131](https://git.developerdojo.org/Curiosity-Corp/catalog/-/issues/131) | Elapsed time in the maintenance summary | good first issue, observability |
| [#132](https://git.developerdojo.org/Curiosity-Corp/catalog/-/issues/132) | Test an interrupted update and the recovery run | help wanted, testing |
| [#133](https://git.developerdojo.org/Curiosity-Corp/catalog/-/issues/133) | Decide whether large downloads should be resumable | help wanted, design |
| [#134](https://git.developerdojo.org/Curiosity-Corp/catalog/-/issues/134) | Record the Python research profile on a second Ubuntu LTS | help wanted, compatibility |
| [#135](https://git.developerdojo.org/Curiosity-Corp/catalog/-/issues/135) | Follow the laptop quickstart on Debian 13 and record what breaks | help wanted, compatibility |
| [#136](https://git.developerdojo.org/Curiosity-Corp/catalog/-/issues/136) | Propose opt-in assistive technology packages | help wanted, accessibility |

Older labelled issues (for example the redacted diagnostic command and schema
validation for public configuration) are also open for contributors. The table
is a snapshot; the issue tracker is the source of truth, filtered by the two
labels.
