# Runbook: public contributor and release session

A scheduled session where outside people work on real issues around a real
release or compatibility milestone. The point is useful work and an honest
record, not attendance numbers.

## Choose the milestone (two weeks before)

- Pick a real trigger: a release candidate to test, a new Ubuntu LTS to try the
  quickstarts on, or a security-hardening list. Write down why it matters.
- Choose 8 to 12 issues from the [backlog](good-first-issues.md) that fit one
  sitting, including at least two that need no code. Mark them in the issue
  tracker for the session.
- Name two maintainers: one answers questions, one reviews. Name a backup for
  each (see [review areas](maintainer-onboarding.md)).

## Prepare a clean environment

Each participant should be able to start without help:

```bash
git clone https://github.com/Curiosity-Corp/catalog.git && cd catalog
python3 -m venv .venv && . .venv/bin/activate
python -m pip install --upgrade pip ansible-core ansible-lint yamllint pyyaml pytest
ansible-galaxy collection install -r requirements.yml
make quickstarts            # runs in a few minutes and proves the setup works
```

Run exactly these commands yourself on a fresh machine the day before, and fix
the page if anything fails. Offer disposable VMs for anyone who cannot install
locally, and say clearly that nobody should test on a machine with data they
care about.

## Announce honestly

Publish the date, the milestone, the issue list and the two ways to take part
(live or asynchronously within a week). Do not offer prizes, do not ask people
to "star" or sign up as a count, and do not ask anyone to split a change into
several merge requests. Say that review comments are normal and that a
requested change is not a rejection.

## During the session

- Open a notes file from
  [`docs/evidence/contributor-session-notes-template.md`](../evidence/contributor-session-notes-template.md).
- Reply to every merge request within the session, even if only to say when it
  will be reviewed.
- Pair a newcomer with a reviewer for their first change.
- Security reports are never handled in public: use the process in
  [SECURITY.md](../../SECURITY.md).

## After the session (within one week)

1. Fill in the notes: what was attempted, what merged, what is still open, what
   confused people.
2. Credit every contributor with a link to their change in the next
   [`CHANGELOG.md`](../../CHANGELOG.md) release notes.
3. Turn confusion into issues or documentation fixes.
4. Link the notes from the monthly evidence record and count contributors only
   by the rules in [contributor cohorts](contributor-cohorts.md).

## What this runbook does not do

It does not pick the date, publish the announcement or staff the session. A
maintainer does those, and the notes are written only after a session happened.
