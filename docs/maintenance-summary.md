# Maintenance summary

After each update run, a researcher can read in plain language what changed,
what was checked, what was left out and whether anything needs attention.

```bash
curiosity-update-summary              # the last run, as text
curiosity-update-summary --format json
```

## Where it comes from

Every update run writes a root-only manifest,
`/var/lib/curiosity/workstation-updates/last-run.json` (schema
`curiosity.workstation.update/v1`). The state directory is `0750 root`, so the
person who uses the machine cannot read it. At the end of the run the role also:

1. installs the helper `/usr/local/bin/curiosity-update-summary`, and
2. runs it with `--write`, which saves `last-run-summary.txt` and
   `last-run-summary.json` in `/var/lib/curiosity/update-summary/`
   (`workstation_update_summary_dir`), readable by everyone on the machine.

If the helper fails, the update does not: both steps are best effort. If you
cannot read the manifest, the helper prints the saved summary instead.

## Example

```text
Workstation update 20260704T020000Z: success
The update finished and every health check passed.

Finished:    2026-07-04T02:09:41Z
Profile:     desktop, update ring general
Catalog:     0123456789ab (signature verified)

Tools changed (3):
  updated  /usr/local/bin/lazygit
  removed  /usr/local/bin/old
  added    ~/.local/bin/uv
Tools unchanged: 1
Upstream releases checked: 2

Health checks failed: none
Health warnings (disabled apps): sunshine
Optional integrations skipped: coder, sunshine
Rollback: not needed

Needs attention: nothing
```

The values are synthetic.

| Section | Meaning |
| --- | --- |
| Status and headline | `success`, `failed`, `rolled_back` or a held run, in one sentence |
| Tools changed | Files added, updated or removed, with their paths, and how many stayed the same |
| Upstream releases checked | The release each moving application resolved to |
| Health checks | Failures, and warnings for applications that are switched off |
| Optional integrations skipped | Applications whose feature flag is off on this host (for example `coder` or `sunshine`), so nothing was installed or checked for them |
| Rollback | Whether the run rolled back, which task failed, and why |
| Needs attention | A short list of things a person should look at, or "nothing" |

## Format and stability for automation

`--format json` prints an object with the schema
`curiosity.workstation.update-summary/v1`. Within `v1`, fields are only added,
never renamed or removed; a breaking change is a new schema version, and the
helper refuses a manifest whose own schema it does not know (exit status 2).
The text format is for people and may change wording.

Exit status: `0` when a summary was produced, `2` when there is no usable
manifest.

## What it leaves out

The summary is built from an allow-list of manifest fields. It never copies the
host name, package inventories, checksums, signer fingerprints or application
contracts, and it redacts credential-looking strings and home directory names
from the one free-text field (the failure message, cut to 300 characters). That
makes it safe to show to the person using the machine and to attach to a bug
report. For the wider rules about what may leave a machine, see
[telemetry and evidence](telemetry-and-evidence.md).

## Checked by

`tests/test_update_summary.py` builds synthetic manifests and checks the content,
the schema, the redaction, the exit codes, that every field the helper reads is
still written by the role, and that the "skipped integrations" rule matches the
health check's.
