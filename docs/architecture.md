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
health gate restores the snapshot and records a quarantine window. The control
plane is local so it can work without a central service or a network connection
to Curiosity infrastructure.

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
