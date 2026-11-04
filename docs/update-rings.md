# Update ring policy for researchers

The update ring balances two needs: installing current security fixes, and not
changing a machine under a long-running experiment or a teaching session. A
ring is a per-machine waiting period applied to newly published upstream
releases. This page documents what the role does today, so you can choose a
ring and plan around the parts that surprise people.

## Choose a ring

Set `workstation_update_ring` in host or group variables or in the machine's
`/etc/ansible/local-vars.yml`. **The default is `canary`**, because the
catalog's own lab follows upstream immediately. Researchers should pick a ring
explicitly.

| Ring | Minimum age of an upstream release | Random start delay of the nightly pull | Use it for |
| --- | --- | --- | --- |
| `canary` | 0 h (installs immediately) | up to 15 min | One machine you are willing to break first |
| `early` | 6 h | up to 6 h | Personal research machines and VMs |
| `general` | 24 h | up to 24 h | Shared lab machines, teaching rooms, long-running experiments |

The values come from `workstation_update_ring_min_age_seconds` (`0`, `21600`,
`86400`) and `workstation_update_ring_randomized_delay_seconds` (`900`,
`21600`, `86400`). The delay applies to the `ansible-pull` timer, which runs at
02:00 local time on `desktop`, `thin-client` and `byod-kiosk` machines when the
`ansible-pull` task group has been applied.

## How the ring decides (read this before relying on it)

1. Release discovery asks GitHub for the newest stable release of **every**
   binary source in `latest_github_repositories` and records each release's
   publication time.
2. On `early` and `general`, any release younger than the ring's minimum age is
   a *blocker*. A release whose publication time cannot be read also blocks.
3. If there is at least one blocker, or a failed run's quarantine is still
   active, `workstation_update_install_allowed` is `false` and the whole
   managed-update transaction is skipped for that run, **including the APT
   upgrade** (`system-updates.yml` is part of the transaction). The run still
   succeeds, writes the manifest and logs "holding the newest upstream
   release(s)" with the names of the blockers.
4. The next run (the next night, with a timer) decides again. Nothing is pinned
   anywhere; waiting is the only effect.

Consequences to plan for:

- The gate is all-or-nothing per run, and it considers every source, not only
  the tools you use. With many upstream sources, a `general` machine can be held
  on a day when any one of them published within 24 hours. Expect updates to
  arrive in batches on quiet days rather than at a fixed delay behind each
  release.
- `canary` never waits for age, but a quarantine still blocks it.
- A held run is not an outage. The previous versions stay installed and
  healthy. If you need an urgent security fix on a held machine, run the
  update by hand on `canary` ring for that run: `-e workstation_update_ring=canary`
  (extra variables beat host variables).

Where to read the decision: `last-run.json` has `update_ring`, `ring_blockers`,
`install_allowed` and `quarantine_active`; each run also writes
`observations/<run>-ring.json` (`schema: curiosity.workstation.update-ring/v1`)
under `/var/lib/curiosity/workstation-updates/`.

## Opting out of automatic updates

| Goal | Setting |
| --- | --- |
| No APT upgrades by the role | `apt_upgrade_enabled: false` |
| No nightly pull | Do not apply the `ansible-pull` tag, or `sudo systemctl disable --now ansible-pull.timer` |
| A machine that must never change on its own | Run the role by hand only, from a reviewed checkout, with `--tags` for what you want |
| Hold everything for a window | Run the role only outside the window; there is no maintenance-window variable |

## Reboot-sensitive changes and operator notifications

- The role never reboots a machine. Kernel and service updates stay pending
  until you reboot.
- A pending reboot is shown in the login MOTD and in
  `/var/lib/curiosity/ansible-pull-status.json` (`desktop`, `thin-client`,
  `byod-kiosk` profiles). Read the file in a monitoring script to alert on it.
- After library upgrades, `needrestart` restarts affected services
  automatically (`pro_mimic_needrestart_auto: true`). For a machine that runs a
  long-lived service you cannot interrupt, set `pro_mimic_needrestart_auto:
  false`, or keep it and list the service in `pro_mimic_needrestart_blacklist`
  (for example `['^experiment-runner[.]service$']`).
- A failed health check after an update restores the managed binaries from the
  run's snapshot, writes a quarantine record and blocks the next update for
  `workstation_update_quarantine_seconds` (3600).
- There is no built-in e-mail or chat notification; the manifest and status
  files are the interface.

## Host-local override example

[`examples/update-ring-early.yml`](../examples/update-ring-early.yml) shows the
whole override and names no host, registry or service. Put it in
`/etc/ansible/local-vars.yml` on the machine; the role loads that file at the
start of a full run (a run with `--tags` skips it, so pass `-e @file.yml` there).

## Review with a research workload

This policy has been checked against the role's code and defaults (see
`tests/test_quickstarts.py`), but it has not yet been reviewed with a real
research workload. Before relying on `general` for a production experiment,
run one full cycle on a disposable copy: note which night the update was held,
which blockers were named, and whether a restart of a service interrupted the
job. Record the result under `docs/evidence/`.
