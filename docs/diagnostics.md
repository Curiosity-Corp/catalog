# Diagnostics and support bundles

When a maintenance run fails, a researcher should be able to say what happened
without sending their whole system. `curiosity-diagnostics` builds a short,
redacted report on the machine, shows it to the person, and writes it to a private
file only if they agree. It never sends anything: the program contains no network
code, and `tests/test_diagnostics.py` fails if that changes. The person decides
whether to attach the file to an issue.

The role installs the command to `/usr/local/bin/curiosity-diagnostics` on the
desktop, thin-client and kiosk profiles (`diagnostics_cli_enabled`, default `true`;
set it to `false` to remove it). The script itself is
`roles/developer_workstation/files/curiosity_diagnostics.py` and runs as an ordinary
user with only the Python standard library.

## Commands

```bash
curiosity-diagnostics fields      # the policy below, as JSON
curiosity-diagnostics preview     # print the bundle; writes nothing
curiosity-diagnostics write       # print it, ask "Write this to a private file? [y/N]", then save
curiosity-diagnostics delete --all          # remove the bundles this tool wrote
curiosity-diagnostics delete <file>         # remove one
```

Options before the command: `--tags packages,update` (the tags of the run you are
reporting; the role does not record them), `--profile`, `--service UNIT` (extra
systemd units to check), `--redact NAME` (extra names to hash, such as a lab or
project name), `--manifest PATH` and `--output-dir DIR`.

`write` refuses to run without a terminal to answer on unless you pass `--yes`, so a
script cannot save a bundle by accident.

## Where the bundle is stored and how to delete it

`~/.local/state/curiosity/diagnostics/diagnostics-<UTC time>.json`, in a directory with
mode `0700`, the file with mode `0600`. Nothing else keeps a copy. Delete with
`curiosity-diagnostics delete <file>` or `--all`, or remove the file yourself. `delete`
only removes files named `diagnostics-*.json` inside that directory.

## What is collected, hashed, redacted and never collected

| Group | Fields |
| --- | --- |
| Collected | tool and schema version, collection date; OS id, version and codename, architecture, kernel major.minor, Python version; `ansible --version` first line; role profile, update ring and the tags you pass; the last update run's status, run id, times, quarantine state, catalog commit and whether it was verified, failure message, failed and warned health check names; per managed binary the path (home shortened to `~`), whether it exists and its sha256 before and after the run; upstream release tag names; a health re-check made now (below) |
| Hashed to a tag | user names (the current user, accounts under `/home`, names given with `--redact`), the host name and fully qualified names ending in a common top-level domain, IPv4 and IPv6 addresses, MAC addresses, e-mail addresses. The tag (`host-1a2b3c4d`) is a salted hash; the salt is discarded, so it cannot be reversed or matched with another bundle, but one machine keeps one tag inside a bundle |
| Replaced outright | tokens (GitHub, GitLab, AWS, Slack, JWT, bearer), passwords inside URLs, values of keys named like secret, token, password, credential or api key, certificate and private key blocks |
| Never collected | file contents other than the update manifest and `/etc/os-release`; home directory listings, shell history, browser, password-manager, SSH and GPG data; research data and project files; environment variables; process lists; network topology; package inventories and application contract settings from the manifest |

Public hosts such as `github.com` and `pypi.org` are left readable so a download
failure is still understandable; loopback addresses are left alone.

## The health re-check

The same command re-checks the machine, bounded to five seconds per command and
read-only, and lists problems in `summary`:

| Check | Source | Reported as |
| --- | --- | --- |
| Command availability and version | the `healthcheck` of each application contract in the last manifest. A command whose arguments are only `--version`, `-V`, `-v` or `version` is run; anything else is only checked for presence and the executable bit | `missing`, `not-executable`, `failed`, `ok`, `present` |
| Service state | `systemctl is-active` for the contract's `services` and any `--service` | `active`, `failed`, `inactive`, `unknown` |
| Disk space | `/`, `/var/lib/curiosity`, `/usr/local` | `low` below 10 percent or 2 GiB free |
| Stale or changed artifacts | sha256 of each managed binary against the digest the last run recorded; age of the last manifest | `missing`, `changed-since-last-run`, `stale` after 14 days |
| Last update | manifest `status` and `healthcheck_failures` | `last update: failed`, `health check failed in last update: <name>` |

This is a re-check on request. The pass/fail gate inside an update run is the role's
`update-health.yml`, which rolls the update back (see [the rollback drill](rollback-drill.md));
the diagnostics command reports what is true now, including after a manual change.

## Limits

* Redaction is pattern based. A name the tool does not know (a colleague's name in a
  failure message, an internal project code) is not found; read the preview and use
  `--redact` for names you know are sensitive.
* The report is only as complete as the last update manifest. A machine that never
  finished a run reports that and the live checks only.
* A report is not a log archive. Add logs yourself, after reading them, if a maintainer asks.
