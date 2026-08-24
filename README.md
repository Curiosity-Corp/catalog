# Curiosity Workstation

Curiosity Workstation is a free, open-source Ansible role and collection for
maintaining Linux computers and research VMs. It provides a dependable,
repeatable baseline for students and researchers so they can spend their time
on studies and research instead of system maintenance.

The project is maintained by Curiosity Research Corporation, a non-profit. It
is designed for personal machines, university labs, research groups, and
ephemeral development workspaces. It does not require a Curiosity account or
access to Curiosity infrastructure.

## What it manages

The `developer_workstation` role is organized around a small number of
profiles and independently tagged task groups:

- conservative package and security updates with health checks, provenance,
  snapshots, quarantine, and rollback;
- developer, scientific Python, data-science, CLI, container, Kubernetes, and
  infrastructure tooling;
- desktop, lightweight desktop, thin-client, and kiosk workstation profiles;
- hardware-aware drivers and power-management settings;
- optional firewall, disk-health, firmware, cleanup, and self-healing timers;
- optional Coder, Keycloak, OpenZiti, remote streaming, custom CA, and chat
  integrations.

Integrations that depend on an organization's private services are disabled by
default. The role never ships private hostnames, credentials, CA material, or
secret-store paths. Supply those values through host/group variables,
Ansible Vault, or a secret manager.

## Status

This repository is in the public-hardening phase. The first collection version
is `0.1.0`; the compatibility matrix and release checklist live in
[`docs/compatibility.md`](docs/compatibility.md) and
[`docs/release-checklist.md`](docs/release-checklist.md).

Review the role in a disposable VM before applying it to a real workstation.
It changes packages, services, users, firewall rules, and system files. A
successful syntax check is not a guarantee that every optional hardware or
vendor integration works on a particular machine.

## Quick start

Install Ansible and the collection dependency on a clean controller:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip ansible-core ansible-lint yamllint pyyaml
ansible-galaxy collection install -r requirements.yml
```

Create an inventory for the machine you intend to manage. The checked-in
playbook targets localhost as a convenient example, so a local run can look
like this after reviewing the variables:

```bash
ansible-playbook \
  -i localhost, \
  -c local \
  playbooks/site.yml \
  -e "dev_user=$USER" \
  -e "dev_user_home=$HOME" \
  --become
```

For a first pass, use a disposable VM and select only a tagged task group,
such as `--tags updates` or `--tags packages`. The role's default desktop
profile still installs a substantial workstation baseline; set the profile and
feature variables explicitly for a smaller machine.

## Collection usage

Build the collection artifact locally:

```bash
ansible-galaxy collection build --force
ansible-galaxy collection install curiosity-workstation-0.1.0.tar.gz
```

Then use the role from a playbook:

```yaml
---
- name: Maintain a research workstation
  hosts: research_workstations
  become: true
  roles:
    - role: curiosity.workstation.developer_workstation
```

The collection metadata is in [`galaxy.yml`](galaxy.yml). Publishing to
Ansible Galaxy is a release milestone, not a claim that the registry already
contains this project.

## Configuration

The most important variables are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `workstation_profile` | `desktop` | `desktop`, `workspace`, `thin-client`, or `byod-kiosk` |
| `dev_user` / `dev_user_home` | `curiosity` / `/home/curiosity` | Account that owns user-scoped tools |
| `apt_upgrade_enabled` | `true` | Enable safe APT maintenance |
| `coder_cli_enabled` | `false` | Opt into a configured Coder deployment |
| `cloud_clis_enabled` | `true` | Install the desktop/workspace Azure, AWS, and GitLab CLI baseline |
| `keycloak_sso_enabled` | `false` | Opt into Keycloak login integration |
| `ziti_enabled` | `false` | Opt into OpenZiti and supply local domains/provider |
| `sunshine_enabled` | `false` | Opt into remote desktop streaming |
| `profile_cleanup_enabled` | `false` | Opt into shared-machine profile cleanup |
| `custom_ca_certificates` | `[]` | Controller-local private CA files, kept outside Git |

See [`docs/configuration.md`](docs/configuration.md) for examples and the
full safety model. `roles/developer_workstation/defaults/main.yml` is the
authoritative variable reference.

## Development checks

Run the same checks used by CI:

```bash
make validate
```

The checks cover YAML, Ansible syntax, Ansible Lint, policy regression tests,
public-surface checks, and collection building. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) before opening a change.

## Community and security

- [Contributing guide](CONTRIBUTING.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
- [Security policy](SECURITY.md)
- [Support policy](SUPPORT.md)
- [Change log](CHANGELOG.md)
- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)

Please do not put passwords, tokens, private keys, internal inventories, or
private CA certificates in issues or pull requests.

## License

The automation and documentation are licensed under the
[Apache License 2.0](LICENSE). Optional vendored chat integrations retain their
own notices; see [`THIRD_PARTY.md`](THIRD_PARTY.md).
