# Previewing changes before you run the role

The role runs as root and changes packages, services, users, firewall rules
and system files. This guide shows how to find out what it would do first, and
is honest about how much a preview can tell you.

**Short version.** Try the role in a throwaway virtual machine (VM). Check your
variables with the redacted variable preview, use `--check --diff` to read a
preview, then run it for real inside the VM and compare the machine before and
after. Only after that, repeat the steps on a machine you care about. Ansible's
check mode is useful for *file changes* and weak for *packages, commands and
services*; the sections below explain why.

## Contents

- [VM first](#vm-first)
- [What stops a run before it changes anything](#what-stops-a-run-before-it-changes-anything)
- [Opt-in settings change nothing by default](#opt-in-settings-change-nothing-by-default)
- [What check mode can and cannot tell you](#what-check-mode-can-and-cannot-tell-you)
- [Tag-scoped preview commands](#tag-scoped-preview-commands)
- [Printing variables without leaking secrets](#printing-variables-without-leaking-secrets)
- [Before and after summary](#before-and-after-summary)
- [How these statements are checked](#how-these-statements-are-checked)

## VM first

Recommended order, safest first:

1. **Disposable VM** (a cloud instance, or a local VM from your hypervisor of
   choice) running a [supported platform](compatibility.md), such as Ubuntu
   24.04. Take a snapshot before you start so you can roll the whole machine
   back, not just what the role knows about. The
   [research VM quickstart](quickstarts/research-vm.md) builds one.
2. In the VM: [print your variables](#printing-variables-without-leaking-secrets),
   run the [preview commands](#tag-scoped-preview-commands), then the real run,
   then the [before and after](#before-and-after-summary) comparison.
3. **A second VM that matches your real machine** (same Ubuntu release, same
   variables) when the real machine has special hardware or private services.
   A VM cannot exercise a GPU, a capture card, a TV or a vendor VPN, so those
   task groups are untested until they run on the real hardware. Leave them
   out (`--skip-tags`) or enable them one tag at a time.
4. **Your real machine**, one tag at a time, starting with the tags whose
   preview was clean.

A plain container is fine for reading file diffs, but it has no `systemd`, so
service tasks and handlers fail there. Do not use a container as proof that a
run will succeed.

Never run a first pass with `--become` against a laptop or workstation you
cannot easily rebuild, and never run it as root from a shell you need to keep.

## What stops a run before it changes anything

Two groups of checks run at the start of every run, whatever `--tags` you
give, and before any package, file or service task:

1. **Configuration validation** (tag `validate`, catalog#31). It checks the
   types and allowed values of every public variable, the shape of URLs,
   checksums and paths, and settings that need each other, such as an enabled
   integration without its URL. It reads variables only, needs neither root
   nor facts, and reports every problem in one message that starts with
   "The public configuration is invalid and nothing on this host was changed".
   Secrets in that message appear as `$REDACTED$` or `(hidden: no_log value)`.
   See [configuration.md#validation](configuration.md#validation) for every
   rule.
2. **Pre-flight checks** (tag `preflight`): running as root, the managed user
   list, that every managed user exists with a home directory, and a valid
   `workstation_profile`. They need root. A preview that cannot become root
   skips them by name with `--skip-tags preflight`; validation still runs.

The host-local variables file `/etc/ansible/local-vars.yml` loads before both,
so they check the values the run will really use.

A preview with `--check` runs both, so a mistyped setting fails the preview the
same way it would fail the real run, with nothing changed.

## Opt-in settings change nothing by default

Integrations that reach outside the machine, remove data or install optional
software are off until you set them. With the defaults, a run leaves them
alone:

| Setting | Default | What it would do when set |
| --- | --- | --- |
| `catalog_tools_enabled` | `false` | Install `catalog-tools` for the [diagnostics summary](#before-and-after-summary) |
| `latest_release_skip_disabled_sources` | `false` | Skip release lookups for applications you switched off ([low-bandwidth guide](low-bandwidth-and-offline.md)) |
| `accessibility_packages` | `[]` | Install reviewed assistive technology ([accessibility](accessibility-and-localization.md)) |
| `user_dotfiles_repo` | `""` | Apply a dotfiles repository; `user_dotfiles_apply: review` gates it on the repository's own review command ([user dotfiles](configuration.md#user-dotfiles)) |
| `workspace_git_auth_helper_enabled` | `false` | Install the workspace Git credential helper ([remote workspaces](workspace-providers.md)) |
| `agent_cpu_policy_enabled` | `false` | Bound the combined CPU of coding agents and IDE processes ([agent CPU policy](agent-cpu-policy.md)) |
| `profile_cleanup_enabled`, `tmp_hygiene_enabled` | `false` | Delete old profile and `/tmp` data on shared machines |
| `zram_swap_enabled`, `browser_memory_cap_enabled` | `false` | Add compressed swap in RAM, or cap browsers' memory ([memory resilience](memory-resilience.md)) |
| `ziti_enabled`, `keycloak_sso_enabled`, `entra_id_enabled`, `coder_cli_enabled`, `fleetd_enabled` | `false` | Join private networks, identity providers or fleet services |
| `workstation_timezone` | `""` | Set the time zone |

[reference.md](reference.md) lists every variable with its default. To see
the value a host will really use, print it with the
[variable preview](#printing-variables-without-leaking-secrets), for example
`-e preview_match='_enabled$'`. In a `--check` preview, the tasks behind a
switched-off setting show as `skipping`.

## What check mode can and cannot tell you

`ansible-playbook --check` asks every task "would you change anything?" without
changing anything. `--diff` adds a before/after text diff for tasks that can
produce one. Check mode is a prediction, not a rehearsal: a task that is
skipped or cannot be simulated reports nothing, and later tasks may then
behave differently than they would in a real run.

### Package tasks (`ansible.builtin.apt`, `apt_repository`)

- Check mode asks the package manager what it would install, using the
  repositories that are configured **right now**. The role adds vendor
  repositories in the `repos` group and installs from them in the `packages`
  group. In check mode the repository is never really added, so on a machine
  the role has not run on, the preview stops at **Install all APT packages**:
  those packages have no install candidate yet, even though a real run would
  succeed.
- `--tags repos --check --diff` previews the whole repository setup. Each
  vendor keyring shows as a change ("Would download ...") without being
  downloaded or checked; the real run downloads it and verifies it.
- The preview lists nothing about *versions* it would pick. Tasks that use
  `state: latest` resolve to whatever the repository offers at run time.
- For a read-only look at what the package manager itself would do, use the
  simulate flag, which changes nothing: `apt-get -s dist-upgrade` or
  `apt-get -s install <package>`.

### Shell and command tasks (`ansible.builtin.command`, `ansible.builtin.shell`)

- Ansible does not run `command` or `shell` tasks in check mode, because it
  cannot know whether they are safe. They show as `skipping`. Anything they
  would have changed is invisible in the preview, for example the
  `dpkg-divert` steps in the log rotation group.
- A skipped task also records no output, so a later task that reads that
  output can fail in a preview where a real run would not.
- The role handles this where it matters. Read-only probes that later tasks
  depend on set `check_mode: false`, so they still run during a preview (for
  example in the vendor repository, keyring and verified-download tasks), and
  steps that need a file an earlier, simulated task never created are skipped
  with `when: not ansible_check_mode`. To list them on your checkout:

  ```bash
  grep -rl 'check_mode: false' roles/*/tasks roles/*/handlers
  grep -rl 'not ansible_check_mode' roles/*/tasks roles/*/handlers
  ```

- Practical rule: treat every `skipping` line for a command or shell task as
  "unknown", not "no change".

### Service tasks (`ansible.builtin.systemd`, `systemd_service`, handlers)

- The `systemd` module does run in check mode. It asks `systemctl` for the
  current state and reports whether `enabled`, `started` or `masked` would
  change. It needs a working `systemd` to answer: on a machine without it the
  task fails instead of previewing.
- `state: restarted` is reported as a change whether or not anything differs,
  so a preview over-reports. A service whose package or unit file does not
  exist yet, because the task that installs it was only simulated, can make the
  preview fail where a real run would succeed.
- Handlers (restart, reload) run in check mode when a task reports a change.
  Handlers that are `command` tasks are skipped like any other command task,
  and a skipped handler cannot notify the next one.
- A real run can restart live services such as networking, `rsyslog` or the
  display manager. A preview shows *that* a restart will be requested, not
  whether your session survives it. Do the first real run in a VM console you
  can reach without the network.

### Files, templates, users and the rest

`copy`, `template`, `file`, `lineinfile`, `blockinfile` and `user` tasks
simulate well and are where `--diff` is most useful. Tasks that handle
secrets, such as the Wi-Fi profile, are marked `no_log` and show no diff;
still read a `--diff` log before sharing it, because a file diff can contain
anything the file contains.

### The update transaction and the network

Groups that install managed software (tags such as `updates`, `packages`,
`languages`, `devops`; see `workstation_update_transaction_tags` in the role
defaults) are wrapped in an update transaction: query upstream releases,
snapshot the managed binaries, install, run health checks, roll back on
failure, and write a manifest to
`/var/lib/curiosity/workstation-updates/last-run.json`.

Check mode skips the upstream release queries, so a preview of those groups
stops at **Require latest metadata for every binary release source**. A
preview therefore does not show which release a run would pick, and it cannot
tell you whether that release will download, pass its SHA-256 check and health
check, or be rolled back.

On a restricted network the real run fails closed: when it cannot learn the
current release or verify a download, it stops instead of installing an older
or unverified file. [Restricted networks](quickstarts/restricted-networks.md)
lists each failure, the proxy settings and how to fill the download cache
offline. Do the first real run on the network the machine will use.

## Tag-scoped preview commands

All commands run from the repository root. They use the example playbook, which
targets the local machine; replace `-i localhost, -c local` with your VM's
inventory to preview a remote VM. Replace `dev_user` and `dev_user_home` with
the account the role should manage.

```bash
export ANSIBLE_ROLES_PATH=roles
COMMON=(-i localhost, -c local playbooks/site.yml
        -e "dev_user=$USER" -e "dev_user_home=$HOME")
```

List the tags the role defines (nothing is run):

```bash
ansible-playbook "${COMMON[@]}" --list-tags
```

List the top-level tasks one tag would run. `--list-tasks` starts with the
validation and pre-flight checks, shows each `include_tasks` as one line and
cannot expand the tasks inside it, so use it as a table of contents, not a
full list:

```bash
ansible-playbook "${COMMON[@]}" --tags timezone --list-tasks
```

Check only the configuration, without root. `--tags validate` selects the
validation and the local variables it reads; `--skip-tags preflight` skips the
root-only pre-flight checks by name, and `ansible_become=false` overrides the
playbook's `become`:

```bash
ansible-playbook "${COMMON[@]}" -e @vm-vars.yml -e ansible_become=false \
  --tags validate --skip-tags preflight --check
```

Preview one tag, with the before/after diff, and keep a copy of the output:

```bash
ansible-playbook "${COMMON[@]}" --tags timezone -e workstation_timezone=America/New_York \
  --become --ask-become-pass --check --diff | tee preview-timezone.log
```

Preview several small tags together, or everything except some tags:

```bash
ansible-playbook "${COMMON[@]}" --tags logrotate,git,timezone \
  --become --ask-become-pass --check --diff | tee preview.log
ansible-playbook "${COMMON[@]}" --skip-tags ziti,wifi,grub \
  --become --ask-become-pass --check --diff | tee preview-full.log
```

Reading the result:

- `PLAY RECAP` at the end counts `changed` (would change), `skipped`
  (including every command and shell task, see above) and `failed`.
- Search the log for `changed:` to list what would change, and for `skipping:`
  to list what the preview could not see:

  ```bash
  grep -E '^(changed|fatal):' preview.log
  grep -B1 '^skipping:' preview.log | grep '^TASK'
  ```

- A `failed` line in a preview is not automatically a problem with the role:
  see the package, update transaction and service limits above. A failure in
  **Stop before any change when the public configuration is invalid** is
  real: fix the settings it names.

Safe, small tags to start with: `timezone`, `logrotate`, `git`, `keyboard`,
`fonts`. Slow or high-impact tags to leave until later: `packages`, `updates`
and the other update transaction tags, `firewall`, `networking`, `wifi`,
`grub`, `ziti`.

## Printing variables without leaking secrets

Before running, check which values will apply. Do **not** paste raw
`ansible-playbook -vvv` output, `ansible -m setup`, or a plain
`ansible-inventory --list` into an issue or chat: they print variables such as
tokens and Wi-Fi keys exactly as written.

`playbooks/preview-vars.yml` prints the role's public variables as the host
will resolve them, with secrets replaced. It loads the host-local variables
file the way the role does and runs the role's configuration validation, so an
invalid setting stops it with the same message a real run would print. It needs
no root and changes nothing:

```bash
export ANSIBLE_ROLES_PATH=roles

# Every public variable, with defaults, local-vars.yml and templates resolved
ansible-playbook -i localhost, -c local playbooks/preview-vars.yml

# Add your vars file and an override, show only matching names (a regular expression)
ansible-playbook -i localhost, -c local playbooks/preview-vars.yml \
  -e @vm-vars.yml -e workstation_profile=workspace -e preview_match='profile|user|timezone'

# The values your inventory gives a VM, redacted the same way
ansible-playbook -i vm-inventory.ini -l vm.example.org playbooks/preview-vars.yml
```

What it redacts:

- Every variable the role's `meta/argument_specs.yml` marks `no_log`, which is
  every variable that holds a secret (`tests/test_argument_specs.py` fails when
  a secret is not marked), shows as `$REDACTED$`. An empty one stays visible,
  so you can see what is unset.
- The login part of a URL (`https://user:password@host`) shows as `***@`.

Limits you should know about:

- It prints the public variables only, the ones in the argument spec and
  [reference.md](reference.md), not internal ones or facts. Variables whose
  default needs host facts are not in the spec.
- A secret placed in a variable that is not meant for one, such as a token
  pasted into a free-text option, is not redacted. Read the output before you
  share it.

To redact the inventory's own variables without the role, filter
`ansible-inventory` with `jq`. This redacts by name, so it is weaker than the
playbook:

```bash
ansible-inventory -i vm-inventory.ini --host vm.example.org | jq '
  with_entries(
    if (.key | test("pass|psk|token|secret|credential|_key$|_key_file$|webhook"; "i"))
       and (.value | tostring | length > 0)
    then .value = "$REDACTED$" else . end)'
```

## Before and after summary

`--check --diff` cannot produce a reliable "this is what changed" report, for
the reasons above (skipped commands, simulated packages and services). What
exists:

- **Before the run:** the preview log, `PLAY RECAP` and the `grep` commands
  above: a task-by-task list of predicted file changes.
- **After a run:** the [maintenance summary](maintenance-summary.md). The role
  saves a plain-language summary of each update run, readable by everyone on
  the machine. It shows the status, which managed tools were added, updated or
  removed, the releases it resolved, health checks, skipped optional
  integrations and whether a rollback happened:

  ```bash
  curiosity-update-summary
  ```

- **After a failed run:** the diagnostics summary (catalog#18). With
  `catalog_tools_enabled: true` (off by default, with
  `catalog_tools_install_url` and `catalog_tools_install_sha256` set) the role
  installs `catalog-tools`. Its summary says in plain words what needs
  attention: missing or broken managed commands, failed services, low disk
  space, and files that changed since the last update. It only reads, writes
  nothing and sends nothing; see
  [troubleshooting](troubleshooting.md#sending-a-maintainer-a-diagnostic-bundle):

  ```bash
  catalog-tools diagnostics summary
  ```

- The run manifest `/var/lib/curiosity/workstation-updates/last-run.json`
  (root only) and `sbom.cdx.json` next to it, and rollback snapshots of the
  managed binaries under `/var/lib/curiosity/workstation-updates/snapshots/`.

These cover the software the update transaction manages. They do not list APT
packages, units or files in `/etc` that the rest of the role changed. To get
that on a VM, record the machine yourself around a real run. This reads state
only:

```bash
snap() {   # usage: snap before | snap after
  d=snapshots/$1; mkdir -p "$d"
  dpkg-query -W -f='${Package}\t${Version}\n' | sort > "$d/packages.tsv"
  systemctl list-unit-files --no-pager --no-legend | sort > "$d/unit-files.txt"
  systemctl list-units --failed --no-pager --no-legend > "$d/failed-units.txt"
  sudo find /etc -xdev -type f -exec sha256sum {} + 2>/dev/null | sort -k2 > "$d/etc.sha256"
}

snap before
# ... take a VM snapshot, then do the real run ...
snap after

diff -u snapshots/before/packages.tsv   snapshots/after/packages.tsv   # packages added or changed
diff -u snapshots/before/unit-files.txt snapshots/after/unit-files.txt # units enabled or disabled
diff -u snapshots/before/etc.sha256     snapshots/after/etc.sha256     # files in /etc that changed
diff -u snapshots/before/failed-units.txt snapshots/after/failed-units.txt
```

Review these files before you share them; `/etc` file names and package lists
can identify a machine.

A built-in report of package, unit and managed-file changes across the whole
role would need new role tasks and is not planned in this document.

## How these statements are checked

These CI jobs run on every merge request, in throwaway containers, never
against a host:

- `quickstart-check-preview` (`scripts/quickstart_check_preview.sh`) runs the
  laptop quickstart's `--check --diff` preview on a clean Debian 13 container.
  It proves that the pre-flight checks stop a preview without root,
  `--tags repos` previews every vendor keyring as a change, and the preview
  with packages stops only at a task the
  [laptop quickstart](quickstarts/laptop.md) names.
- `config-validation` (`scripts/config_validation_check.sh`) runs every valid
  and invalid configuration example. An invalid one must stop the role in
  validation before any other task, with no secret in the output. It also runs
  `playbooks/preview-vars.yml` with secrets that must not appear in its output,
  even at `-vvv`.
- `quickstarts` runs `tests/test_restricted_network.py`, which reproduces the
  fail-closed network behaviour on loopback.

The statements about service tasks and containers follow from how Ansible's
check mode works; check them in your own VM rather than trusting this page.
