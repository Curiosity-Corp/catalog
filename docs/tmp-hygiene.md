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

The drop-in is named `00-curiosity-tmp.conf` on purpose. When several
tmpfiles.d files declare `/tmp`, systemd keeps the first by file name and logs
the rest as `Duplicate line for path "/tmp", ignoring`, so this file wins over
the vendor `tmp.conf` and over any other local file that declares `/tmp`.

## Pressure relief

When the filesystem holding `/tmp` is at or above `tmp_hygiene_pressure_threshold`
the helper

1. logs the percentage and runs `systemd-tmpfiles --clean --prefix=/tmp`
   (the standard rules, scoped to `/tmp`; `/var/tmp` is never swept);
2. if usage is still at or above the threshold and
   `tmp_hygiene_pressure_age` is set, runs one more pass with that shorter
   age from `/etc/curiosity/tmp-pressure-tmpfiles.conf`. At run time the helper
   merges that file's static exclusions with every `x`/`X` rule under `/tmp`
   that installed packages ship (`systemd-tmpfiles --cat-config`), writes the
   result to the unit's runtime directory under `/run`, and passes only that
   file, so package-declared protections stay in force;
3. logs `before% -> after%`, and warns if `/tmp` is still over the threshold.
   The service still exits 0.

Below the threshold it exits silently. Nothing is deleted by name pattern, and
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
| `tmp_hygiene_pressure_threshold` | `85` | Percent usage that triggers the helper (1-100) |
| `tmp_hygiene_pressure_age` | `1d` | Age for the second pass; empty means warn only |
| `tmp_hygiene_pressure_interval` | `1h` | Time between pressure checks |
| `tmp_hygiene_exclusions` | X11, ICE, XIM, font sockets, `systemd-private-%b-*`, `tmux-*`, `ssh-*`, `snap-private-tmp`, Chromium singletons, AppImage mounts, `tsx-*`, `claude-*`, OpenZiti tunneler IPC sockets (`.ziti`) | tmpfiles `x`/`X` lines excluded from aging |
| `tmp_hygiene_extra_exclusions` | `[]` | Host-specific additions, for example `X /tmp/my-long-lived-dir` |
| `tmp_hygiene_tmpfiles_path` | `/etc/tmpfiles.d/00-curiosity-tmp.conf` | Drop-in location |
| `tmp_hygiene_pressure_script_path` | `/usr/local/sbin/curiosity-tmp-pressure` | Helper location |
| `tmp_hygiene_pressure_config_path` | `/etc/curiosity/tmp-pressure-tmpfiles.conf` | Second-pass rules |

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
