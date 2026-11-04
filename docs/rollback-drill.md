# Rollback drill

The update transaction restores the previous binaries when a health check fails
and holds further installs for a quarantine window. `tests/test_update_rollback_drill.py`
proves this on every merge request by injecting a failure and checking the result
on disk, so the claim does not rest on reading the YAML.

## What the drill does

It runs the role's real `update-foundation.yml`, `update-health.yml`,
`update-rollback.yml`, `update-manifest.yml`, `update-snapshot-retention.yml` and
`update-transaction.yml` with `ansible-playbook` against a temporary directory. Only
`managed-updates.yml`, the part that downloads and installs, is replaced by a stub
that installs a chosen artifact. In order:

| Step | Injected | Checked |
| --- | --- | --- |
| 1. Healthy update | a good release replaces the tool and adds a file | manifest status `success`, digests recorded |
| 2. Failed update | a release whose `--version` exits 1 | the old tool is restored byte for byte; the file the update created is removed; a package-owned path in `workstation_update_non_removable_paths` is left alone; the snapshot is kept; the manifest has status `failed`, the failure message, the catalog commit, the digest before the update and the digest of the failed artifact; `quarantine.json` names the run |
| 3. Retry inside the window | the same update again | nothing is installed (every install task is gated on `workstation_update_install_allowed`), the run is recorded as held |
| 4. Retry after the window | the marker is aged past `workstation_update_quarantine_seconds` | the update is allowed again and succeeds |

Two static checks fail the build if a task is added to `managed-updates.yml` without
the quarantine gate, or if the transaction stops routing failures to rollback and the
manifest.

The drill found one real gap, fixed in the same change: a rolled-back run wrote an
empty `after` list to the manifest, so the digest of the artifact that failed was
never recorded. `update-rollback.yml` now stats the managed paths before restoring
them and records them as the `after` inventory.

## How it stays safe

The task files are copied with `owner`, `group` and `become*` removed, every path is
under a temporary directory, and `dpkg` and `dpkg-query` are stubs on `PATH`. It never
touches `/usr`, `/var` or the host's packages, and it needs no root. Without
`ansible-playbook` the behaviour tests are skipped, and in CI they run. One drill run
takes a few minutes on a busy machine because the foundation task builds a real
inventory and SBOM.

Run it with `python -m pytest -q tests/test_update_rollback_drill.py` (the `rollback_drill` CI job does the same).

## What the drill does not prove

Be careful before relying on rollback outside these limits:

* **Package-manager-owned files** (the entries of `workstation_update_non_removable_paths`,
  for example `/usr/bin/sunshine`) are not removed on rollback, because deleting them would
  leave dpkg's database wrong, and a package upgrade is not downgraded. If an update upgraded
  such a package and its health check failed, reinstall the previous package version by hand.
* **Excluded paths** (`workstation_update_rollback_excluded_paths`: npm, `m365`, the codex
  binary) are not restored, deliberately, so a later failure does not push an older CLI
  over a good registry refresh.
* **Only regular files and symlinks in `workstation_update_managed_paths` are snapshotted.** Package
  state, configuration under `/etc`, systemd units, data and anything an installer wrote
  elsewhere are not part of the snapshot. The Go tree has its own archive snapshot.
* **The health check decides.** A broken update that still passes its configured healthcheck
  is not rolled back. Healthchecks are command, version and service probes (see the
  diagnostics documentation); a tool with no healthcheck row is not gated.
* **The quarantine is time based and local.** It holds the install tasks of the update
  transaction for `workstation_update_quarantine_seconds` (default 3600). It does not stop the
  rest of the role, a manual run, or another host.
* **Real installers are not exercised.** The drill uses a stand-in installer; GitHub
  downloads, checksum verification and package repositories are covered by their own
  tests and by the evidence log, not here.
* **The drill is not a disposable-machine run.** It does not reboot, switch users, or run
  real systemd units. A dated disposable-machine run is still the proof for promoting a
  platform to supported (see the [compatibility matrix](compatibility.md)).
