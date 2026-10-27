# Changelog fragments

Every merge request that changes user-visible behaviour adds **one small file here** instead of editing
`CHANGELOG.md`. Parallel merge requests then never conflict on the changelog, and `scripts/release.py prepare`
compiles the fragments into the release section.

## How to add one

Create `changelog.d/<issue>.<type>.md`:

```bash
echo 'Console no longer prints its access token to the journal (#140).' > changelog.d/140.security.md
```

- `<issue>` is the GitLab issue number (`140`), or a short slug when there is no issue (`mic-watchdog`).
  Letters, digits, `-` and `_` only. A second entry for the same issue and type gets a distinct name
  (`140-extra.fixed.md`).
- `<type>` is one of `added`, `changed`, `deprecated`, `removed`, `fixed`, `security`
  (the Keep a Changelog headings).
- The content is the entry as Markdown, written the way it should read in the changelog: what changed for the
  user and why, with the `#issue` reference. A leading `- ` is optional; continuation lines are indented for you.

## What CI enforces

The `changelog-check` job on every merge request fails when the merge request

- edits `CHANGELOG.md` directly (release commits, `chore(release): X.Y.Z`, are exempt), or
- changes anything under `src/` and adds no fragment, or
- adds a fragment with a bad name, an unknown type or no content.

A change that genuinely needs no entry (a refactor with no behaviour change) opts out explicitly, with either

- the `skip-changelog` merge request label (re-run the pipeline after adding it), or
- a `Skip-Changelog: <reason>` trailer on any commit of the branch, or a line `Skip-Changelog: <reason>` in the
  merge request description.

## At release time

`scripts/release.py prepare` merges the fragments into the new `## [X.Y.Z] - date` section, grouped under
`### Added`, `### Changed`, `### Deprecated`, `### Removed`, `### Fixed`, `### Security`, sorted by issue
number, and deletes them. The tag gate refuses a release commit that still has fragments. See `docs/release.md`.
