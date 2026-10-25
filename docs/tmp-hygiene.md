# /tmp hygiene

Workstations that mount `/tmp` as tmpfs fill it with build output, screenshots
and test artefacts until applications fail with `ENOSPC`. The `tmp_hygiene`
task group (`roles/developer_workstation/tasks/tmp-hygiene.yml`, tags
`tmp-hygiene, system`) bounds that with native systemd tooling and no custom
cleaner.

It is **off by default** (`tmp_hygiene_enabled: false`) because it deletes
files. Enable it per host or group, for example in
`/etc/ansible/local-vars.yml`:

```yaml
tmp_hygiene_enabled: true
```

It runs on the `desktop`, `thin-client` and `byod-kiosk` profiles on systemd
hosts, and is skipped on `workspace` and inside containers. When the switch is
set back to `false`, the next pull removes everything the group installed.

## What it installs

| Path | Purpose |
| --- | --- |
| `/etc/tmpfiles.d/00-curiosity-tmp.conf` | `q /tmp 1777 root root <age>` plus exclusions; read by the stock `systemd-tmpfiles-clean.timer` (daily) |
| `/usr/local/sbin/curiosity-tmp-pressure` | Pressure helper (shell, reads no config of its own) |
| `/etc/systemd/system/curiosity-tmp-pressure.{service,timer}` | Hourly oneshot, `ConditionPathIsMountPoint=/tmp`, `Nice=10`, idle I/O class |
| `/etc/curiosity/tmp-pressure-tmpfiles.conf` | Shorter-age rules for the second pressure pass; deliberately not in `tmpfiles.d` |
| `/etc/systemd/system/tmp.mount.d/50-curiosity-inodes.conf` | Raises the tmpfs inode cap (`nr_inodes`) of `/tmp`; see [Inode cap](#inode-cap) |

The drop-in is named `00-curiosity-tmp.conf` on purpose. When several
tmpfiles.d files declare `/tmp`, systemd keeps the first by file name and logs
the rest as `Duplicate line for path "/tmp", ignoring`, so this file wins over
the vendor `tmp.conf` and over any other local file that declares `/tmp`.

## Inode cap

systemd mounts `/tmp` as a tmpfs with `nr_inodes=1m`, 1,048,576 files and
directories, whatever the RAM. On 2026-10-25 that limit stopped agents on mrh
and deskmeet1 with `ENOSPC` while the tmpfs was only 23% full: many concurrent
Claude agent sessions each leave a `/tmp/tmp.XXXXXXXX/claude-1000/...` tree,
plus pytest directories and virtualenvs, all made of tiny files. Run
`df -i /tmp` to see it; `df -h` looks healthy.

`tmp_hygiene_nr_inodes` raises the cap with a drop-in for `tmp.mount`:

```ini
# Managed by curiosity.workstation (tmp_hygiene_nr_inodes). Do not edit.
[Mount]
Options=mode=1777,strictatime,nosuid,nodev,size=50%%,nr_inodes=7831k,x-systemd.graceful-option=usrquota
```

### Sizing (`auto`)

The default, `auto`, is the tmpfs size divided by 4 KiB. With systemd's
`size=50%` that is `ansible_memtotal_mb * 128` inodes, half of RAM in pages,
which is also the kernel's own default for a tmpfs mounted without
`nr_inodes`. Every non-empty file takes at least one 4 KiB page, so with this
cap inodes and space run out at about the same point, and as many agents fit as
the memory allows.

- The value is rounded to whole `k` (written as `m` when it is a whole number
  of `m`). A 61 GB host (MemTotal 64155296 kB) gets `7831k`, about 7.8m.
- It is never below 1m: hosts with 8 GB of RAM or less resolve to systemd's
  default, get no drop-in, and behave exactly as before.
- It is never above 16m (reached at 128 GB).
- An explicit value such as `4m`, `2048k` or `4194304` overrides `auto`;
  empty leaves the systemd default and removes the drop-in.

`auto` assumes systemd's `size=50%`. If the host sets a different tmpfs size,
set `tmp_hygiene_nr_inodes` explicitly.

### Memory cost

A higher cap is cheap: an inode costs memory only once a file exists. Each
existing file or directory pins kernel memory for its inode and dentry, about
1 KB in the worst case, and that slab memory cannot be swapped out. At the
`auto` cap a host full of empty files would hold about 7.8 GB of that on a
61 GB machine, the same order as the 50% of RAM the tmpfs may already fill
with data. The safety net is the hourly pressure check below, which cleans
`/tmp` when either blocks or inodes reach 85%, long before either runs out.

### How the drop-in is built

The `Options=` line is not hardcoded. The role asks systemd which unit file
defines `tmp.mount` (`systemctl show --property=FragmentPath`: the vendor
file, an admin override in `/etc`, or the fstab generator's unit), reads its
last `Options=` line and changes only `nr_inodes` (replaced in place, or
appended), so the other options, their order and specifier escaping such as
`%%` stay exactly as the vendor ships them. On a current Ubuntu the result is
byte-identical to the hand-placed file used during the incident.

The drop-in is installed only when that unit is `Type=tmpfs` and its
`Options=` is a plain comma list (no line continuation or spaces). Otherwise,
for example when `/tmp` is an ext4 filesystem from fstab or `tmp.mount` does
not exist, the role leaves the cap alone, says so in the play output and
removes any drop-in it installed before; adding `nr_inodes` to a non-tmpfs
mount would break it at boot.

When the drop-in changes the role runs `systemctl daemon-reload` and, if
`/tmp` is currently a tmpfs mount point (`findmnt`), applies the new cap live
with `mount -o remount,nr_inodes=<value> /tmp`; files in `/tmp` are not
affected. tmpfs refuses a cap below the inodes already in use; the play then
prints a warning and the cap takes effect at the next boot. The drop-in is
removed when `tmp_hygiene_enabled` is false, when the value resolves to empty,
or when `tmp.mount` cannot be managed; the running mount keeps its current cap
until the next reboot.

## Pressure relief

The helper reads both `df -P` (blocks) and `df -iP` (inodes) for the
filesystem holding `/tmp`. When blocks are at or above
`tmp_hygiene_pressure_threshold` or inodes are at or above
`tmp_hygiene_pressure_inode_threshold`, it

1. logs both percentages and runs `systemd-tmpfiles --clean --prefix=/tmp`
   (the standard rules, scoped to `/tmp`; `/var/tmp` is never swept);
2. if either is still at or above its threshold and
   `tmp_hygiene_pressure_age` is set, runs one more pass with that shorter
   age from `/etc/curiosity/tmp-pressure-tmpfiles.conf`. At run time the helper
   merges that file's static exclusions with every `x`/`X` rule under `/tmp`
   that installed packages ship (`systemd-tmpfiles --cat-config`), writes the
   result to the unit's runtime directory under `/run`, and passes only that
   file, so package-declared protections stay in force;
3. logs `cleanup done: blocks before% -> after%, inodes before% -> after%`,
   and warns if `/tmp` is still over a threshold. The service still exits 0.

Below both thresholds it exits silently. A filesystem that reports no inode
counts (`df -iP` prints `-`) is judged on blocks alone. Nothing is deleted by name pattern, and
the `systemd-private-*` directories of running services are never removed
(the current-boot ones are excluded; stale ones from earlier boots are
removed by the stock tmpfiles rules).

View activity with `journalctl -u curiosity-tmp-pressure --since today`; check
the schedule with `systemctl list-timers curiosity-tmp-pressure.timer
systemd-tmpfiles-clean.timer`.

## Variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `tmp_hygiene_enabled` | `false` | Master switch |
| `tmp_hygiene_max_age` | `3d` | Age for the daily cleanup (systemd span: `m`, `h`, `d`, `w`) |
| `tmp_hygiene_pressure_enabled` | `true` | Install the hourly pressure timer (when the master switch is on) |
| `tmp_hygiene_pressure_threshold` | `85` | Percent block usage that triggers the helper (1-100) |
| `tmp_hygiene_pressure_inode_threshold` | `85` | Percent inode usage that triggers the helper (1-100) |
| `tmp_hygiene_pressure_age` | `1d` | Age for the second pass; empty means warn only |
| `tmp_hygiene_pressure_interval` | `1h` | Time between pressure checks |
| `tmp_hygiene_exclusions` | X11, ICE, XIM, font sockets, `systemd-private-%b-*`, `tmux-*`, `ssh-*`, `snap-private-tmp`, Chromium singletons, AppImage mounts, `tsx-*`, `claude-*`, OpenZiti tunneler IPC sockets (`.ziti`) | tmpfiles `x`/`X` lines excluded from aging |
| `tmp_hygiene_extra_exclusions` | `[]` | Host-specific additions, for example `X /tmp/my-long-lived-dir` |
| `tmp_hygiene_tmpfiles_path` | `/etc/tmpfiles.d/00-curiosity-tmp.conf` | Drop-in location |
| `tmp_hygiene_pressure_script_path` | `/usr/local/sbin/curiosity-tmp-pressure` | Helper location |
| `tmp_hygiene_pressure_config_path` | `/etc/curiosity/tmp-pressure-tmpfiles.conf` | Second-pass rules |
| `tmp_hygiene_nr_inodes` | `auto` | tmpfs inode cap for `/tmp`: `auto` (RAM in 4 KiB pages / 2, 1m to 16m), an explicit count such as `4m`, or empty for the systemd default |
| `tmp_hygiene_nr_inodes_dropin_path` | `/etc/systemd/system/tmp.mount.d/50-curiosity-inodes.conf` | Inode cap drop-in |

## Behaviour to know about

- Aging uses the newest of access, modification and change time, so a file a
  process keeps open but never touches can still be removed once it is older
  than the age.
- Age is judged from file times, not liveness. A long-running program whose
  socket or lock file in `/tmp` is older than the age loses it unless a rule
  excludes it (a socket's timestamps do not advance while it is in use). The
  defaults cover X11, tmux, ssh-agent, systemd, snap, Chromium/Chrome,
  AppImage, tsx IPC pipes and Claude Code session directories. tmpfiles.d has
  no way to filter by file type (it cannot say "keep sockets and FIFOs"), so
  protection is by path only; add anything else your tools keep in `/tmp` with
  `tmp_hygiene_extra_exclusions`, for example `X /tmp/my-tool-*`.
- Disk-backed `/tmp` still gets the daily age-based cleanup; the pressure timer
  does nothing there because `/tmp` is not a mount point.
- Systemd units that use `PrivateTmp=` have their own directories under
  `/tmp/systemd-private-*`; they are neither measured nor cleaned by the
  pressure helper beyond the stock rules.

## Replacing a hand-installed cleaner

Hosts that previously ran an untracked `tmp-cleaner` or `tmp-pressure`
service and timer should retire them once this group has converged:

```sh
sudo systemctl disable --now tmp-cleaner.timer tmp-pressure.timer
sudo rm -f /etc/systemd/system/tmp-cleaner.{service,timer} \
  /etc/systemd/system/tmp-pressure.{service,timer}
sudo systemctl daemon-reload
```

and remove any older local tmpfiles drop-in that also declares `/tmp` (it is
harmless, because `00-curiosity-tmp.conf` wins, but it is dead configuration).
The role does not delete host-specific files it did not create. Confirm the
effective rule with `systemd-tmpfiles --cat-config | grep -B1 '^q /tmp '`.
