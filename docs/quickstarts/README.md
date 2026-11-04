# Researcher quickstarts

Short, public-variables-only guides for the situations researchers ask about
most. Each guide has a matching file in [`examples/`](../../examples) that the
test suite parses, checks against the role's variables and renders with
`ansible-playbook --list-tasks`, so the examples cannot silently drift from
the role.

| You have | Start here | Example files |
| --- | --- | --- |
| An Ubuntu laptop you own | [Researcher laptop](laptop.md) | [`laptop.yml`](../../examples/laptop.yml) |
| A VM for compilers, Python and data tools | [Research VM](research-vm.md) | [`research-vm.yml`](../../examples/research-vm.yml) |
| A lab that boots VMs from images | [cloud-init and libvirt](cloud-init-libvirt.md) | [`cloud-init/`](../../examples/cloud-init) |
| An access appliance or a shared lab machine | [Thin client and shared lab](thin-client-and-shared-lab.md) | [`thin-client.yml`](../../examples/thin-client.yml), [`shared-lab.yml`](../../examples/shared-lab.yml) |
| A university proxy, a slow link or no internet | [Restricted networks](restricted-networks.md) | none (environment settings only) |
| Jupyter notebooks on a remote machine | [Notebooks](notebooks.md) | [`notebooks/`](../../examples/notebooks) |

## Rules that apply to every guide

1. Try the guide on a disposable VM first. The role changes packages, services,
   users, firewall rules and system files, and a syntax check proves only that
   the playbook parses.
2. Use only public variables. Put anything private (certificates, tokens,
   host names of a real lab) in host or group variables, Ansible Vault or a
   secret manager, never in a file you commit.
3. Select tasks with `--tags` for a first pass. Tasks without a tag do not run
   when `--tags` is given, so the pre-flight checks and the host-local
   variables file `/etc/ansible/local-vars.yml` are skipped: pass your
   variables with `-e @file.yml` in that mode.
4. The role has no uninstaller. The supported way to undo a run is to restore
   the VM snapshot or backup you took before it; each guide lists what the role
   leaves behind and how to switch the recurring parts off.

## Terms

- **Profile**: `workstation_profile`, one of `desktop`, `workspace`,
  `thin-client`, `byod-kiosk` ([architecture](../architecture.md)).
- **Tag**: an Ansible tag that selects a task group, for example `python` or
  `packages`.
- **Ring**: `workstation_update_ring`, how long a newly published upstream
  release waits before this machine installs it
  (`canary` 0 h, `early` 6 h, `general` 24 h).
