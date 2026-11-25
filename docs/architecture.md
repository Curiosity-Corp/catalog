# Architecture

The repository has three layers:

```text
playbooks/site.yml
        |
        v
roles/developer_workstation/tasks/main.yml
        |
        +--> profile-specific packages and repositories
        +--> latest-release discovery and verified artifact cache
        +--> update transaction -> health checks -> rollback/quarantine
        +--> optional security, desktop, science, network, and identity tasks
        +--> local manifest with status, versions, provenance, and SBOM path
```

The role is intentionally stateful on the managed host. Before a managed
update it snapshots files listed in `workstation_update_managed_paths`; after
the update it runs application health checks and writes a manifest. A failed
health gate restores the snapshot and records a quarantine window. After a
successful transaction only the newest `workstation_update_snapshot_keep`
snapshots are kept, never the current run's or a quarantined run's. The control
plane is local so it can work without a central service or a network connection
to Curiosity infrastructure.

## Interrupted update

A pull killed half way (power cut, `kill -9`, a reboot during the timer run)
skips both `rescue` and `always` in `update-transaction.yml`. It leaves behind:

- this run's snapshot directory `snapshots/<run_id>` with `before.json` and
  copies of the managed binaries as they were before the run;
- managed tools in whatever state the kill caught them: some updated, some
  not, and possibly one half-written file from an archive being unpacked;
- no new manifest and no new maintenance summary. `last-run.json` and the
  summary still describe the run before, so they say nothing about the
  interruption;
- no quarantine marker, so the next pull is not held back.

Recovery is the next pull of the same tags. It takes a new snapshot, installs
again over whatever is there, runs the health gate and writes a manifest.
`tests/test_update_interrupted_recovery.py` covers this: it runs the role's
real snapshot, CLI-archive install and manifest tasks in a generated playbook
against a temporary directory, SIGKILLs the first run after the snapshot and
the install, truncates one tool as a stand-in for a half-unpacked archive, and
checks that the second run succeeds, writes a manifest and leaves every tool
complete and runnable.

The second run's snapshot is the interrupted state, not the state before the
interrupted run. If the recovery run itself fails its health gate, rollback
restores that interrupted state (including a half-written file). The good
binaries from before the interruption are still in the earlier run's snapshot
directory, which retention keeps until a later successful run prunes it; restore
from it by hand.

What the test does not cover: root ownership and modes (it runs unprivileged
and drops `owner`/`group`), the real health gate (`update-health.yml`, replaced
by running each tool), installers other than `unarchive` of a cached archive
(apt, npm, pipx, the Go tree, direct `get_url` downloads and their partial
files in the artifact cache), a kill in the middle of the snapshot copy, a kill
during rollback, retention pruning and the summary helper.

## Profiles

- `desktop`: full workstation baseline, scientific tooling, updates, and
  optional GUI integrations;
- `workspace`: container-safe tooling and optional Coder credential helpers;
- `thin-client`: small browser/access appliance for a remote workspace;
- `byod-kiosk`: locked-down browser appliance for a shared or borrowed device.

Every profile shares preflight checks, user ownership repair, update policy,
health/provenance reporting, and the nightly `ansible-pull` timer where the
profile supports it. Optional integrations are guarded by variables and
should be configured in host/group vars.

## Trust boundaries

- Upstream release metadata is fetched at run time and selected artifacts are
  verified with the digest published by the upstream release API.
- Package-manager signatures remain the trust mechanism for distribution
  packages.
- Private certificates are read from paths supplied by the operator; they are
  never part of the collection artifact by default.
- Secrets are consumed from protected variables or interactive login flows.
  They are not written to Git or emitted in task output where `no_log` applies.
- Catalog commit verification is available through
  `catalog_integrity_mode: enforce` once an operator has provisioned and
  documented a signing key.

The role has root-level effects. Run it against a disposable VM first and
review the effective variables with `ansible-inventory` or a dry run.
