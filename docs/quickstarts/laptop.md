# Researcher laptop quickstart

Maintain an Ubuntu laptop with this role without any Curiosity account,
service or network. The steps use only public variables from
[`examples/laptop.yml`](../../examples/laptop.yml).

Target: Ubuntu 24.04 (amd64), the recommended starting point in the
[compatibility matrix](../compatibility.md). Ubuntu 22.04 is a supported
target; other releases are experimental.

> Run these steps on a disposable VM that mirrors the laptop before you run
> them on the laptop. Make a VM snapshot or a backup of the laptop first: the
> role has no uninstaller (see [Uninstall and disable](#uninstall-and-disable)).

## 1. Prepare the controller

The laptop is its own controller. In a normal terminal (not as root):

```bash
sudo apt-get update
sudo apt-get install -y git python3-venv python3-pip
git clone --branch v0.1.0 https://github.com/Curiosity-Corp/catalog.git
cd catalog
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip ansible-core
ansible-galaxy collection install -r requirements.yml
```

Pin a reviewed tag or commit (`v0.1.0` above) rather than a moving branch.

## 2. Set your variables

Copy [`examples/laptop.yml`](../../examples/laptop.yml) next to your checkout,
change `dev_user` and `dev_user_home` to your account, and keep it outside the
repository if it will ever hold anything personal:

```bash
cp examples/laptop.yml ~/laptop-vars.yml
```

What the example switches off and why:

| Variable | Value | Reason |
| --- | --- | --- |
| `cloud_clis_enabled` | `false` | Skips the Azure, AWS and GitLab CLI baseline |
| `bitwarden_browser_extensions_enabled`, `bitwarden_desktop_enabled` | `false` | No forced password-manager install |
| `coder_cli_enabled`, `keycloak_sso_enabled`, `ziti_enabled` | `false` | No organization services (already the default) |
| `sunshine_enabled`, `profile_cleanup_enabled`, `tmp_hygiene_enabled` | `false` | No streaming and no automatic cleanup (already the default) |

`workstation_update_ring: general` makes the laptop wait 24 hours after an
upstream release is published before it installs it
([update rings](../update-rings.md)).

## 3. Preview before changing anything

```bash
TAGS=repos,packages,python,science,data-science,git
ansible-playbook -i localhost, playbooks/site.yml --syntax-check
ansible-playbook -i localhost, -c local playbooks/site.yml \
  -e @$HOME/laptop-vars.yml --tags "$TAGS" --list-tasks
```

`--list-tasks` prints the task groups that would run for those tags and
changes nothing. `--check --diff` goes further but cannot predict package,
shell or service tasks accurately; treat it as a hint, not a guarantee, and use
the VM run as the real preview.

## 4. Apply

```bash
ansible-playbook -i localhost, -c local playbooks/site.yml \
  -e @$HOME/laptop-vars.yml --tags "$TAGS" --become --ask-become-pass
```

What this does, at a high level:

| Tag | Effect |
| --- | --- |
| `repos`, `packages` | Adds vendor APT repositories with verified signing keys and installs the desktop package set |
| `python`, `science`, `data-science` | Installs `uv`, `ruff`, `poetry`, JupyterLab and IPython with `pipx`, and the scientific stack into `/opt/science-venv` |
| `git` | Applies the Git configuration tasks |

The `packages`, `python`, `science` and `data-science` tags also start the
**update transaction**: the role queries
upstream release metadata, evaluates the ring, snapshots the managed binaries,
installs, health-checks, and rolls back and quarantines the run if a health
check fails. The release metadata query is the only step that needs GitHub; see
[restricted networks](restricted-networks.md) when the laptop cannot reach it.

The desktop `packages` task group installs a large set of packages. Read
`roles/developer_workstation/tasks/packages.yml` and drop the `packages` tag
if you want only the Python tooling.

## 5. See what changed and how to roll back

| Where | What |
| --- | --- |
| `/var/lib/curiosity/workstation-updates/last-run.json` | Run manifest: status, versions before and after, ring decision, health results |
| `/var/lib/curiosity/workstation-updates/snapshots/<run>/` | Copy of each managed binary before the run (the newest three runs are kept) |
| `/var/lib/curiosity/workstation-updates/quarantine.json` | Present only after a failed run; blocks the next update for one hour |
| `/var/lib/curiosity/workstation-updates/sbom.cdx.json` | Local software bill of materials |

The role restores the pre-run snapshot automatically when a health check fails.
It restores only the files it manages, which are the binaries listed in
`workstation_update_managed_paths`, not APT packages or your documents: keep
your own backups for those.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `This role must run as root` | Run with `--become`, or `sudo -E env "PATH=$PATH" ansible-playbook ...` |
| `Could not resolve a current upstream release for <tool>` | No network, a proxy, or a GitHub rate limit. The role refuses to install a stale or unverified binary. Retry later, or see [restricted networks](restricted-networks.md). Export `GITHUB_TOKEN` to raise the rate limit and never commit it |
| `GitHub returned no SHA-256 asset digests` | Same family: the run stops rather than install an artifact without a digest |
| The run says the ring is "holding the newest upstream release(s)" | Working as designed for `early` and `general`; nothing was installed for those tools and the run still succeeds |
| A health check failed and a quarantine file exists | The role rolled back. Read `last-run.json` (`failure`, `healthcheck_failures`), fix the cause, and wait out `workstation_update_quarantine_seconds` (3600) or remove `quarantine.json` after review |
| apt reports a lock | Another package manager is running; wait and retry |

When you report a problem, include the Ansible version, the Ubuntu release, the
tags you used and the redacted output. Remove user names, host names, IP
addresses and tokens ([support policy](../../SUPPORT.md)).

## Uninstall and disable

There is no automatic uninstall. To stop the recurring parts of the role:

```bash
sudo systemctl disable --now ansible-pull.timer   # only exists if the ansible-pull tag ran
```

The example's tag list does not include `ansible-pull`, so the nightly timer is
not installed. A full run with no `--tags` on a `desktop` machine does install
it; it pulls `ansible_pull_repo` (public GitHub `main` by default) every night.

To go back to the previous state, restore the VM snapshot or backup. To remove
individual leftovers by hand:

- `/opt/science-venv`, `/etc/profile.d/science-venv.sh`, `/usr/local/bin/jupyter`,
  `/usr/local/bin/ipython`, `/usr/local/share/jupyter/kernels/science`;
- the pipx tools: `pipx uninstall uv ruff poetry pre-commit jupyterlab ipython`;
- `/var/lib/curiosity` and `/var/cache/curiosity/artifacts` (update state and
  download cache);
- packages with `sudo apt-get remove <package>` after reading what depends on it.
