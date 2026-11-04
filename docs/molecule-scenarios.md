# Molecule scenarios: design and prerequisites

Molecule converges the role in a throwaway instance and checks the result. It is
**not wired into CI yet**, because the shared runner cannot host it (below). This page
is the design, so the work is a day of setup once a suitable machine exists, not a
new investigation.

## What CI can and cannot do today

The pipeline runs on a non-root Kubernetes runner in a `python:3.13-slim` image. Molecule's
container drivers need a container engine, and a role that manages systemd units, GRUB, udev
and LightDM needs a systemd-booted instance, which means privileged containers or a VM.
Neither is available on that runner and asking for a privileged runner is a change to shared
infrastructure, so it is left to a person (see "Needed to finish").

What exists instead: `ansible-playbook --syntax-check`, ansible-lint, the policy tests that render the
real task files, and [`rollback-drill.md`](rollback-drill.md), which runs the update transaction's task
files against a temporary tree.

## Scenarios

Both run in a systemd-enabled instance (a VM through the delegated driver, or a privileged
`ubuntu:24.04` systemd container) and use only the role's public variables.

| Scenario | Inventory variables | Instance |
| --- | --- | --- |
| `desktop` | `workstation_profile: desktop`, all optional vendors, integrations and credentials left at their defaults (off) | Ubuntu 24.04 amd64 |
| `thin-client` | `workstation_profile: thin-client`, `hardware_profile: generic` | Debian 13 or Ubuntu 24.04, amd64 and arm64 |

Each scenario runs the Molecule sequence `syntax`, `converge`, `idempotence`, `verify`:

1. `syntax`: `ansible-playbook --syntax-check` on `playbooks/site.yml`.
2. `converge`: the playbook against the instance. A failure here is a platform or package problem; record it
   in `docs/platform-differences.md`.
3. `idempotence`: the second converge must report `changed=0` (Molecule does this itself).
4. `verify`: assertions that do not depend on the network, listed below.

Verify assertions, per scenario: the managed update manifest exists, its `schema` is
`curiosity.workstation.update/v1` and `status` is `success`; every command named by a contract healthcheck
is present; the services the profile enables are active; `curiosity-diagnostics preview` (once catalog #41 is merged) runs and its output
contains none of the instance's host name, user name or IP address (the same check the unit tests make on
synthetic data, here on real data); and no file under `/etc/curiosity`, `/var/lib/curiosity` or the
converge log contains a credential-shaped string (`python scripts/scan_secrets.py` run against them).

## No secret leakage

Converge runs with `ANSIBLE_NO_LOG` unset on purpose, so a task that prints a secret is seen; the scan above is
what fails the scenario. Credentials are never passed to Molecule: scenarios use no vault, no tokens and
only default (off) integrations.

## Prerequisites on the machine that runs them

* A Linux machine with Python 3.13, `ansible-core`, `molecule` and `molecule-plugins` pinned in
  `ci/requirements.txt` through `make lock-ci`.
* For container instances: rootless podman cannot run systemd as PID 1 with the units this role touches; use
  `podman run --privileged` or a VM. For VMs: libvirt or a cloud instance reachable over SSH, destroyed after each run.
* About 20 GB of disk and 10 minutes per scenario on an idle machine; the package task downloads several GB.
* Network access to the distribution mirrors and the vendor repositories the profile enables.

## Needed to finish

A person has to provide one of: a runner with a container engine and permission to run privileged systemd
containers for this project, or a small VM the pipeline may start and destroy. Then the scenario files
(`molecule/<scenario>/molecule.yml`, `converge.yml`, `verify.yml`) are written to the design above and a
`molecule` job is added to `.gitlab-ci.yml`. Writing them before an instance exists would leave untested files that
nothing can run.
