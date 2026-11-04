# Counting external contributors

The project reports how many unique external human contributors made
substantive changes in a rolling 12-month window, and never more than the
evidence supports. A script prepares a worksheet; **a person decides**.

## The monthly review

On the first working day of each month, one maintainer:

1. Runs the worksheet on the default branch:

   ```bash
   python3 scripts/contributor_cohort.py --internal-file ~/maintainers.txt
   ```

   The internal file lists the emails or `@domains` of maintainers and internal
   accounts, one per line. Keep it outside the repository, because it names
   people. Without it, every author is a candidate, including maintainers.
2. Reads the changes of each candidate (the script prints commit counts by kind
   and how many commits were tiny; it never decides) and moves a name to the
   evidence record only when the person is human and made at least one
   substantive change.
3. Records the date, the window, the names (or a private reference) and links to
   each change in the [monthly evidence file](../evidence/monthly-template.md),
   and the result of the review, including a month with a lower number than the
   last.
4. Credits contributors in the release notes and replies to any open requested
   changes (see the [backlog rules](good-first-issues.md)).

## What counts

A contribution counts when it is a code, test, documentation or maintenance
change that a maintainer reviewed and merged, or a reviewed change that the
project asked for. A person counts once in the window however many changes they
made.

## What never counts

- Bots and AI agents. The script lists them separately and excludes them.
- Maintainers and people paid by the project, who are internal to this count.
- Token changes: whitespace, reflowed text, a file touched only to appear in the
  history.
- One change split into several merge requests, and people recruited to produce
  a number. If a maintainer suspects either, the person is not counted until the
  maintainer is satisfied.
- A person's identity guessed from an email address. If a name is unclear, ask.

## Before using the number outside the project

Recompute it that day from the default branch, read the changes again, and keep
the worksheet. Never report a threshold the worksheet and a reviewer did not
support. The same rule applies to downloads and dependents, which come only from
a registry measurement
([downloads and dependents template](../evidence/downloads-dependents-template.md)).

## Checked by

`tests/test_contributor_cohort.py` builds a throwaway git repository and checks
that bots, AI agents, internal accounts and commits outside the window are left
out, that the output says it is a list to review, and that the script reads
nothing but git and the optional file.
