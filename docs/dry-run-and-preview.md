# Previewing changes before you run the role

The role runs as root and changes packages, services, users, firewall rules
and system files. This guide shows how to find out what it would do first, and
is honest about how much a preview can tell you.

**Short version.** Try the role in a throwaway virtual machine (VM). Use
`--check --diff` there to read a preview, then run it for real inside the VM
and compare the machine before and after. Only after that, repeat the steps on
a machine you care about. Ansible's check mode is useful for *file changes* and
weak for *packages, commands and services*; the sections below explain why.

## Contents

- [VM first](#vm-first)
- [What check mode can and cannot tell you](#what-check-mode-can-and-cannot-tell-you)
- [Tag-scoped preview commands](#tag-scoped-preview-commands)
- [Dumping variables without leaking secrets](#dumping-variables-without-leaking-secrets)
- [Before and after summary](#before-and-after-summary)
- [Evidence for the statements above](#evidence-for-the-statements-above)

## VM first

Recommended order, safest first:

1. **Disposable VM** (a cloud instance, or a local VM from your hypervisor of
   choice) running a [supported platform](compatibility.md), such as Ubuntu
   24.04. Take a snapshot before you start so you can roll the whole machine
   back, not just what the role knows about.
2. In the VM: run the [preview commands](#tag-scoped-preview-commands), then
   the real run, then the [before and after](#before-and-after-summary)
   comparison.
3. **A second VM that matches your real machine** (same Ubuntu release, same
   variables) when the real machine has special hardware or private services.
   A VM cannot exercise a GPU, a capture card, a TV or a vendor VPN, so those
   task groups are untested until they run on the real hardware. Leave them
   out (`--skip-tags`) or enable them one tag at a time.
4. **Your real machine**, one tag at a time, starting with the tags whose
   preview was clean.

A plain container is fine for reading file diffs, but it has no `systemd`, so
service tasks and handlers fail there (see [evidence](#evidence-for-the-statements-above)).
Do not use a container as proof that a run will succeed.

Never run a first pass with `--become` against a laptop or workstation you
cannot easily rebuild, and never run it as root from a shell you need to keep.

## What check mode can and cannot tell you

`ansible-playbook --check` asks every task "would you change anything?" without
changing anything. `--diff` adds a before/after text diff for tasks that can
produce one. Check mode is a prediction, not a rehearsal: a task that is
skipped or cannot be simulated reports nothing, and later tasks may then
behave differently than they would in a real run.

### Package tasks (`ansible.builtin.apt`, `apt_repository`)

- Check mode asks the package manager what it would install, using the
  repositories that are configured **right now**. The role adds third-party
  repositories in the `repos` group and installs from them in the `packages`
  group. In check mode the repository is never really added, so the later
  install of a package from that repository fails with "No package matching
  ... is available", even though a real run would succeed. Previewing
  `--tags packages` alone on a fresh machine fails the same way.
- The preview lists nothing about *versions* it would pick. Tasks that use
  `state: latest` resolve to whatever the repository offers at run time.
- The role has no special handling for apt tasks (none of its roughly 75 apt
  tasks sets `check_mode`), so everything above applies to all of them.
- For a read-only look at what the package manager itself would do, use the
  simulate flag, which changes nothing: `apt-get -s dist-upgrade` or
  `apt-get -s install <package>`.

### Shell and command tasks (`ansible.builtin.command`, `ansible.builtin.shell`)

- Ansible does not run `command` or `shell` tasks in check mode, because it
  cannot know whether they are safe. They show as `skipping`. Anything they
  would have changed is invisible in the preview, for example the
  `dpkg-divert` steps in the log rotation group.
- A skipped task also records no output, and the role has tasks that read that
  output. `packages.yml` runs `apt-cache pkgnames` to find the newest OpenJDK
  and .NET packages; under `--check` that probe is skipped and the next task
  then tries to install a package with an empty name, which fails the run.
  The same pattern exists in `packages-workspace.yml`. This is a known
  limitation of preview for the `packages` group, not a sign that a real run
  will fail.
- The role does handle this in other places. Of its roughly 195 `command` and
  `shell` tasks and handlers, all but a few set `changed_when` (so a real run
  reports honest "changed" results and read-only probes report `ok`), and the
  rest use `creates` or `removes`. Four tasks set `check_mode: false` so a
  read-only probe still runs during a preview (`pro-mimic-ssh`,
  `wifi-netplan`, `i915-pcon-frl-444` and `bitwarden-desktop`), and a handful of
  tasks use `when: not ansible_check_mode` to skip steps that depend on a file
  an earlier, simulated task never created (`console-video`, `grub-autoboot`,
  `wifi-netplan`, `i915-pcon-frl-444`, `bitwarden-desktop`, `minimal-desktop`).
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
  Handlers that are `command` tasks, such as the font cache, TLP and Netplan
  handlers, are skipped like any other command task, and a skipped handler
  cannot notify the next one.
- A real run can restart live services such as networking, `rsyslog` or the
  display manager. Preview shows *that* a restart will be requested, not whether your session
  survives it. Do the first real run in a VM console you can reach without the
  network.

### Files, templates, users and the rest

`copy`, `template`, `file`, `lineinfile`, `blockinfile` and `user` tasks
simulate well and are where `--diff` is most useful. Tasks that handle
secrets, such as the Wi-Fi profile, are marked `no_log` and show no diff;
still read a `--diff` log before sharing it, because a file diff can contain
anything the file contains.

### The update transaction

Groups that install managed software (tags such as `updates`, `packages`,
`languages`, `devops`; see `workstation_update_transaction_tags` in the role
defaults) are wrapped in an update transaction: snapshot the managed binaries,
install, run health checks, roll back on failure, and write a manifest to
`/var/lib/curiosity/workstation-updates/last-run.json`. A tag-scoped preview of
any of these groups therefore also walks the transaction wrapper, which makes
it slow (it stats every managed path) and means a rollback path can trigger
inside a preview when an earlier simulated step fails. A preview does not
download or install software, but it cannot tell you whether an upstream
release will install cleanly, pass its health check, or be rolled back.

### Two more things worth knowing

- `--tags` runs **only tagged tasks**. The role's untagged pre-flight checks
  (are you root, does `dev_user` exist, is the profile valid) are skipped in a
  tag-scoped run, so a preview with `--tags` does not validate your variables.
  Run a full-play preview too (see below) to get those checks.
- I did not verify which network tasks (GitHub release lookups, downloads)
  contact the network during a preview. Assume they may, and preview only on a
  machine where that is acceptable.

## Tag-scoped preview commands

All commands run from the repository root. They use the example playbook, which
targets the local machine; replace `-i localhost, -c local` with your inventory
to preview a remote VM. Replace `dev_user` and `dev_user_home` with the account
the role should manage.

```bash
export ANSIBLE_ROLES_PATH=roles
COMMON=(-i localhost, -c local playbooks/site.yml
        -e "dev_user=$USER" -e "dev_user_home=$HOME")
```

List the tags the role defines (nothing is run):

```bash
ansible-playbook "${COMMON[@]}" --list-tags
```

List the top-level tasks one tag would run. `--list-tasks` shows each
`include_tasks` as one line and cannot expand the tasks inside it, so use it as
a table of contents, not a full list:

```bash
ansible-playbook "${COMMON[@]}" --tags timezone --list-tasks
```

Preview one tag, with the before/after diff, and keep a copy of the output:

```bash
ansible-playbook "${COMMON[@]}" --tags timezone -e workstation_timezone=America/New_York \
  --become --ask-become-pass --check --diff | tee preview-timezone.log
```

Preview several small tags together, or everything except one tag:

```bash
ansible-playbook "${COMMON[@]}" --tags logrotate,git,timezone \
  --become --ask-become-pass --check --diff | tee preview.log
ansible-playbook "${COMMON[@]}" --skip-tags streamstack,sunshine,ziti \
  --become --ask-become-pass --check --diff | tee preview-full.log
```

The untagged pre-flight checks only run in a full-play preview (the
`--skip-tags` command above), not with `--tags`.

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
  see the package and service limits above.

Safe, small tags to start with: `timezone`, `logrotate`, `git`, `keyboard`,
`fonts`. Slow or high-impact tags to leave until later: `packages`, `updates`
and the other update transaction tags, `firewall`, `networking`, `wifi`,
`grub`, `ziti`, `sunshine`, `streamstack`.

## Dumping variables without leaking secrets

Before running, check which variables will apply. Do **not** paste raw
`ansible-playbook -vvv` output, `ansible -m setup`, or a plain
`ansible-inventory --list` into an issue or chat: they print variables such as
tokens, Wi-Fi keys and Pro tokens exactly as written.

`scripts/preview_vars.py` is a read-only helper that merges the role defaults,
any vars files and any `-e` overrides, then prints the result with secrets
replaced by `***REDACTED***`:

```bash
# Everything the role would default to, in the same precedence order Ansible uses
python3 scripts/preview_vars.py

# Add your host-local file and an override, show only matching names
python3 scripts/preview_vars.py --vars-file /etc/ansible/local-vars.yml \
  -e workstation_profile=workspace --match 'profile|user|timezone'

# Redact the variables your inventory resolves for a host
ansible-inventory -i hosts.ini --host myvm | python3 scripts/preview_vars.py --stdin --format json
```

What it redacts:

- non-empty values whose variable name looks sensitive: password, passphrase,
  token, secret, credential, `psk`, private key, `api_key`, anything ending in
  `_key` or `_key_file`, and similar. Empty values stay visible so you can see
  what is unset. Fingerprints, key URLs and keyring paths are not redacted.
- values that look like credentials under any name: private key blocks, GitHub,
  GitLab, Slack, AWS and `sk-` style tokens, JSON web tokens, and the password
  part of `https://user:password@host` URLs.

Limits you should know about:

- It shows values as written. A Jinja expression such as
  `"{{ some_var }}"` is shown as text, not evaluated, so the dump tells you
  what is configured, not what the host will compute. It also reads only
  variable files, not facts.
- Redaction is by name and pattern. A secret stored under an innocuous name in
  a free-text value may survive. Read the output before you share it.

## Before and after summary

**Planned limitation.** The role does not produce a human-readable "this is
what changed" report, and `--check --diff` cannot produce one for the reasons
above (skipped commands, simulated packages and services). What exists today:

- The preview log, `PLAY RECAP` and the `grep` commands above: a task-by-task
  list of predicted file changes.
- After a real run, the role writes
  `/var/lib/curiosity/workstation-updates/last-run.json` (run id, status,
  profile, update ring, catalog commit, the upstream releases it resolved) and
  `sbom.cdx.json` next to it (a CycloneDX inventory, from Trivy when it is
  installed, otherwise a fallback listing installed packages). These describe
  the run, not a diff against the previous one. Rollback snapshots of
  managed binaries are kept under
  `/var/lib/curiosity/workstation-updates/snapshots/`
  (`workstation_update_snapshot_keep` of them).

To get a real before and after on a VM, record the machine yourself around a
real run. This reads state only:

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

A built-in before/after report (package, unit and managed-file changes written
by the role itself) would need new role tasks and is not planned in this
document.

## Evidence for the statements above

These were observed on 2026-11-03 against a throwaway Ubuntu 24.04 container
(rootless Podman, `ansible-core` 2.21.6, repository at commit `4880ec3`), never
against a host:

- `--tags timezone --check --diff` completed (`ok=5 changed=1`) and printed the
  `Etc/UTC` to `America/New_York` diff.
- `--tags logrotate --check --diff` printed file and directory diffs, showed
  both `dpkg-divert` command tasks as `skipping`, and then failed in the
  `Reload systemd` handler with `Failed to find required executable
  "systemctl"` because a container has no `systemd`.
- Including `packages.yml` alone with `--check` failed with `No package
  matching 'code' is available` (the third-party repository from the `repos`
  group was not configured).
- The `apt-cache pkgnames` OpenJDK probe copied from `packages.yml` showed
  `skipping` under `--check`, and the following install task then failed with
  `No package matching '' is available`.
- A full `--tags packages --check --diff` was started and stopped after about
  ten minutes, still inside the update transaction's managed-path checks, so
  its final result is unknown.
- Task counts come from reading `roles/*/tasks` and `roles/*/handlers`
  (`command` and `shell` tasks, `changed_when`, `check_mode`,
  `ansible_check_mode`). They will drift as the role changes; re-count rather
  than trusting the numbers.
