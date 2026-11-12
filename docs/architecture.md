# Architecture

_Last verified: 2026-11-11 against `main`._

The repository is one Ansible collection (`curiosity.workstation`, version
`0.1.0` in [`galaxy.yml`](../galaxy.yml)) with one playbook and two roles:

```text
playbooks/site.yml                       (localhost, become, force_handlers)
        |
        +--> roles/developer_workstation   tasks/main.yml includes ~125 task files
        |       +--> preflight, users, user-scoped systemd and linger
        |       +--> workstation profile packages and repositories
        |       +--> hardware profile features (drivers, display, input, power)
        |       +--> latest-release discovery and verified artifact cache
        |       +--> update transaction -> health checks -> rollback/quarantine
        |       +--> optional security, desktop, science, network, identity,
        |            StreamStack, PIA tunnel, Ziti, fleetd, tmp hygiene
        |       +--> local manifest with status, versions, provenance, SBOM path
        |
        +--> roles/workstation_storage     opt-in encrypted btrfs /home
        |
        +--> post_tasks: converge stamp /var/lib/curiosity/converged.json
                         (only after every role converged; on with fleetd)
```

The role is intentionally stateful on the managed host. Before a managed
update it snapshots files listed in `workstation_update_managed_paths`; after
the update it runs application health checks and writes a manifest. A failed
health gate restores the snapshot and records a quarantine window. After a
successful transaction only the newest `workstation_update_snapshot_keep`
snapshots are kept, never the current run's or a quarantined run's. The control
plane is local so it can work without a central service or a network connection
to Curiosity infrastructure.

## How a machine stays converged

A machine is enrolled once by running `playbooks/site.yml` against it. Where
the profile supports it, the role then installs a nightly `ansible-pull` timer
(`tasks/ansible-pull-timer.yml`) that checks out `ansible_pull_repo` at
`ansible_pull_branch` into `ansible_pull_dir` (default
`/opt/curiosity-catalog`) and re-runs the same playbook. Site values live on
the host in `/etc/ansible/local-vars.yml` or in protected inventory, never in
this repository. With `catalog_integrity_mode: enforce` the pull verifies the
commit signature before applying it.

Device inventory and drift reporting are optional and separate: with
`fleetd_enabled` the role installs a site-built, Notation-signed Fleet agent
package (`tasks/fleetd.yml`) and the playbook writes the converge stamp after
each complete run. Fleet never installs software; the catalog stays the
machine layer.

## Profiles

Two independent selectors shape a run:

- `workstation_profile` decides what kind of machine this is:
  - `desktop`: full workstation baseline, scientific tooling, updates, and
    optional GUI integrations;
  - `workspace`: container-safe tooling and optional Coder credential helpers;
  - `thin-client`: small browser/access appliance for a remote workspace;
  - `byod-kiosk`: locked-down browser appliance for a shared or borrowed
    device.
- `hardware_profile` (default `generic`; also `arm-thin-client`,
  `x86-byod-laptop`, `z590`, `deskmeet-b760`, `t460`) turns hardware-specific
  features on. Each entry in `hardware_profiles` sets keys such as
  `display_scale`, `cpu_max_perf_pct`, `mouse_debounce_ms`,
  `crash_diagnostics` or `pia_tunnel_apps`, and the role derives its
  `*_enabled` variables from them. Every derived default is `false` or empty
  for `generic`.

Every profile shares preflight checks, user ownership repair, update policy,
health/provenance reporting, and the nightly `ansible-pull` timer where the
profile supports it. Optional integrations are guarded by variables and
should be configured in host/group vars. `defaults/main.yml` of each role is
the authoritative variable reference;
[`configuration.md`](configuration.md) explains them.

## Storage role

`workstation_storage` does nothing unless `workstation_storage_enabled` is
true. When it is, it manages a LUKS2 + btrfs data volume for `/home` and
optional hot write paths, a monthly scrub and a weekly fstrim, and ships the
`hh-storage-*` scripts for a one-time cutover. It takes no scheduled snapshots
and makes no backups. See [`storage.md`](storage.md).

## Vendored StreamStack

StreamStack (the call and stream studio) is its own project with its own
tests, CI, releases and decision records. This repository vendors a release
tag as a squashed `git subtree` at
`roles/developer_workstation/files/streamstack` (v0.0.32 as of this check) and
only wires it into the host (`tasks/streamstack*.yml`). Changes to StreamStack
code, CI or its container images go to the StreamStack project and arrive here
through `git subtree pull`, never as an edit to the vendored tree. Renovate
ignores that path for the same reason. See [`streamstack.md`](streamstack.md).

## Trust boundaries

- Upstream release metadata is fetched at run time and selected artifacts are
  verified with the digest published by the upstream release API.
- Package-manager signatures remain the trust mechanism for distribution
  packages.
- Site-built packages (fleetd) are installed only after a detached Notation
  signature on their checksum list verifies against an operator-supplied
  certificate.
- Private certificates are read from paths supplied by the operator; they are
  never part of the collection artifact by default. The only certificate in
  the role is PIA's public WireGuard API CA.
- Secrets are consumed from protected variables, root-only files delivered out
  of band, or interactive login flows. They are not written to Git or emitted
  in task output where `no_log` applies.
- Catalog commit verification is available through
  `catalog_integrity_mode: enforce` once an operator has provisioned and
  documented a signing key.
- `tests/test_public_contract.py` fails the build if a private hostname,
  secret-store name or private certificate enters the public surface.

The role has root-level effects. Run it against a disposable VM first and
review the effective variables with `ansible-inventory` or a dry run.

## Delivery

CI is GitLab (`.gitlab-ci.yml`). Feature branches get merge request pipelines
only; `main` gets a branch pipeline. Stages:

| Stage | Jobs |
| --- | --- |
| validate | `yaml` (yamllint), `syntax` (playbook syntax check), `lint` (ansible-lint) |
| test | `policy`: `tests/test_update_policy.py`, then `pytest` over all of `tests/` |
| security | `secret_detection` (GitLab template), `secret_hygiene` (public-surface contract) |
| package | `collection` (build and normalize the tarball), `sbom` (CycloneDX), `provenance` (SHA-256 of both) |

`make validate` runs the same checks locally for contributors. Decisions of
record are indexed in [`adr/README.md`](adr/README.md).
