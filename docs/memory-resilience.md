# Memory resilience

A research workstation that runs many terminal and agent sessions next to a
browser can run out of memory in a way that hurts the wrong thing. On one
61 GB workstation a browser grew to 257 processes and about 46 GB, and the
kernel's out-of-memory killer ran seven times in one day (catalog#95). The
earlyoom of the time (`-m 5 -s 50`) never acted first, so the kernel picked
its victims itself.

The role answers that with three parts, each behind its own variable, all in
`tasks/system-tuning.yml` (tags `tuning, system`; desktop profile with
`system_tuning_enabled`):

| Part | Variable | Default | Effect |
| --- | --- | --- | --- |
| [zram swap](#zram-swap) | `zram_swap_enabled` | `false` | Compressed swap in RAM, used before the swap file |
| [earlyoom](#earlyoom) | `earlyoom_*` | on, `-r 300 -m 8,4 -s 15,8` | Stops the browser before the kernel OOM killer runs |
| [Memory-capped browser](#memory-capped-browser) | `browser_memory_cap_enabled` | `false` | Slows, then stops, a runaway browser inside its own cgroup |

Turn all three on for a busy workstation, for example in
`/etc/ansible/local-vars.yml` or the host's variables:

```yaml
zram_swap_enabled: true
browser_memory_cap_enabled: true
```

The earlyoom defaults already apply. `tests/memory_resilience/play-fixture.yml`
(run by the `config-validation` CI job) renders every file below and runs the
browser launcher tasks against a throwaway home.

## zram swap

`zram_swap_enabled: true` installs `systemd-zram-generator` and writes
`/etc/systemd/zram-generator.conf`:

| Variable | Default |
| --- | --- |
| `zram_swap_size_expression` | `min(ram / 2, 32768)` (MiB: half the RAM, at most 32 GiB) |
| `zram_swap_algorithm` | `zstd` |
| `zram_swap_priority` | `100` |
| `vm_swappiness` | `100` with zram, else `10` |
| `vm_page_cluster` | `0` with zram, else not written (kernel default 3) |

The `/swap.img` file of `swap_size_gb` stays as a fallback at priority `-1`,
so the kernel fills zram first and the disk file only after it. With swap in
compressed RAM, anonymous pages cost about as much to reclaim as page cache,
hence swappiness 100; zram has no seek cost, so swap-in readahead
(page-cluster) is off. Both sysctls change only when zram is on.

The device is added at once. When a running device has another size it is
resized only while it holds no pages, otherwise at the next boot. `false`
leaves a zram device that something else set up alone.

**Why zram stays off by default.** It changes how a machine swaps, it uses RAM
for the compressed pages, and a small or memory-starved machine (a thin
client, a VM with 2 GB) gains little and can lose page cache. A lab turns it
on where the workload fits: many long-lived sessions on a machine with 16 GB
or more.

## earlyoom

earlyoom runs on every desktop-profile host. `/etc/default/earlyoom` is
written from `templates/earlyoom.default.j2`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `earlyoom_mem_percent` | `"8,4"` | SIGTERM below 8% available memory, SIGKILL below 4% |
| `earlyoom_swap_percent` | `"15,8"` | SIGTERM below 15% free swap, SIGKILL below 8% |
| `earlyoom_report_interval` | `300` | A memory report in the journal every 5 minutes (`0`: none) |
| `earlyoom_prefer_regex` | browsers | Processes earlyoom picks first |
| `earlyoom_avoid_regex` | sessions, terminals, desktop, media | Processes earlyoom picks last |

earlyoom acts only when memory **and** swap are both below their level. With
zram the swap fills quickly, so the old 50% swap level was crossed long before
memory ran out and the 5% memory level came too late; 8,4 and 15,8 act on a
machine that is really out of both, before the kernel does. A single number
(`"5"`) still works and means SIGKILL at half of it.

The regexes are POSIX extended expressions matched against the process name
the kernel keeps (`/proc/PID/comm`, at most 15 characters, so
`streamstack-console` is `streamstack-con`). The defaults:

- **prefer**: `brave`, `chrome`, `chromium`, `msedge`, `vivaldi-bin`,
  `opera`, `firefox`, `firefox-bin` and Firefox's helpers (`Web Content`,
  `WebExtensions`, `Isolated Web Co`, `Privileged Cont`, `RDD Process`,
  `Utility Process`), and `headless_shell`.
- **avoid**: agent sessions (`claude`, `codex`, `hyperharness*`, `hh-*`,
  `tmux*`, `screen`, `sshd*`, `mosh-server`); terminals (`alacritty`, `kitty`,
  `foot`, `wezterm*`, `gnome-terminal*`, `xfce4-terminal`, `konsole`, `xterm`,
  `urxvt`, `terminator`, `tilix`, `ptyxis*`); the desktop session
  (`systemd*`, `dbus-*`, `Xorg`, `Xwayland`, `gnome-shell`, `openbox`,
  `lightdm`, `gdm*`, `sddm*`, `tint2`, `picom`, `dunst`); media and streaming,
  so a live call keeps its sound and picture (`pipewire*`, `wireplumber`,
  `pulseaudio`, `streamstack*`, `sunshine`, `gst-launch-1.0`, `ffmpeg`,
  `mediamtx`, `obs`, `zoom`, `ZoomWebviewHost`, `teams-for-linux`); and
  `ziti-edge-tunn*` and `earlyoom` itself.

`--avoid` lowers a process's place in earlyoom's choice; it does not make it
untouchable. With nothing else left, earlyoom can still pick an avoided
process, which is better than the kernel choosing at random. A call held in a
browser tab goes with the browser; use a desktop call client for calls that
must survive memory pressure.

To add names, set the whole regex in the host's variables, for example to
also prefer compilers and test runners on a build-heavy machine:

```yaml
earlyoom_prefer_regex: '^(brave|chrome|chromium|firefox|Web Content|Isolated Web Co|cc1plus|rustc|pytest)$'
```

Quotes, backslashes and line breaks are refused by the configuration check:
they would break the quoting of `/etc/default/earlyoom`. An empty regex adds
no `--prefer` or `--avoid`.

**What a change restarts.** A changed earlyoom setting rewrites
`/etc/default/earlyoom` and restarts `earlyoom.service`, nothing else. No
browser, session, StreamStack or Sunshine unit is started, stopped or
restarted. The new defaults reach a host at its next converge as one earlyoom
restart.

## Memory-capped browser

`browser_memory_cap_enabled: true` gives each account in `dev_users` a
launcher per installed browser of `browser_memory_cap_browsers`:

| File | Content |
| --- | --- |
| `~/.local/bin/<name>-capped` | Starts the browser with `systemd-run --user --scope --slice=app-browser.slice -p MemoryHigh=… -p MemoryMax=…` |
| `~/.local/share/applications/<desktop_id>` | The system entry from `/usr/share/applications` with every `Exec=` command replaced by the launcher; arguments, actions and every other line kept |

The user entry takes precedence over the system one, so the menu, the
desktop's default-browser setting and `xdg-open` start the capped browser.
At `MemoryHigh` the kernel reclaims the browser's own memory, which slows the
browser down; at `MemoryMax` the kernel's OOM killer acts inside the
browser's scope only, and the rest of the machine never notices.

| Variable | Default |
| --- | --- |
| `browser_memory_cap_enabled` | `false` |
| `browser_memory_cap_slice` | `app-browser.slice` |
| `browser_memory_cap_browsers` | Brave: `/usr/bin/brave-browser-stable`, `brave-browser.desktop`, 16G / 24G |

Each entry has `name` (the launcher is `<name>-capped`), `command` (the
absolute path of the browser's own launcher), `desktop_id`, `memory_high` and
`memory_max`. Sizes are systemd sizes (`16G`, `8192M`), a percentage of RAM
(`25%`) or `infinity`; `memory_high` may not be above `memory_max`. A second
browser:

```yaml
browser_memory_cap_browsers:
  - {name: brave-browser, command: /usr/bin/brave-browser-stable, desktop_id: brave-browser.desktop, memory_high: 16G, memory_max: 24G}
  - {name: google-chrome, command: /usr/bin/google-chrome-stable, desktop_id: google-chrome.desktop, memory_high: 25%, memory_max: 40%}
```

For one launch, `BROWSER_MEMORY_HIGH=20G BROWSER_MEMORY_MAX=28G
brave-browser-capped` overrides the limits. Without a systemd user manager
(no login session) the launcher starts the browser uncapped and logs that to
the journal rather than not starting it.

Rules and limits:

- **Only on its next start.** Nothing is started or stopped by the role. A
  browser that is already running stays uncapped; and because Chromium-based
  browsers hand a new window to the running instance, quit the browser once
  after turning the cap on.
- **Installed browsers only.** An entry whose `command` is missing gets no
  launcher; an entry whose system desktop entry is missing gets a launcher but
  no override.
- **PIA tunnel browsers are skipped.** A browser routed through the
  [PIA tunnel](pia-tunnel.md) (`pia_tunnel_apps`) is already capped by the
  tunnel launcher's own scope (`pia_tunnel_app_definitions`, `scope`), and the
  tunnel owns its desktop entries. A second override would flap with it and
  could start the browser outside the tunnel, so the role writes none.
- **Snap browsers.** `snap run` moves the browser into a scope of its own, so
  a cap around it does not hold. List deb or tarball installs.
- **Turning it off.** `false`, or removing an entry, deletes the launchers
  and overrides this role wrote (they carry the marker
  `curiosity-browser-memory-cap`) at the next converge. Files without the
  marker are never touched.
- **Accounts.** A `dev_users` entry may carry `group` when the account's
  primary group is not named after it; the default is the account name.

## Checking a host

Read-only commands, safe on a running machine:

```sh
swapon --show                         # /dev/zram0 at priority 100, /swap.img at -1
sysctl vm.swappiness vm.page-cluster  # 100 and 0 with zram
ps -o args= -C earlyoom               # earlyoom -r 300 -m 8,4 -s 15,8 --prefer ... --avoid ...
journalctl -u earlyoom --since today  # reports every 5 minutes, and any kill
systemctl --user list-units 'brave-browser-capped-*'   # after starting the browser from the menu
systemctl --user show -p MemoryHigh -p MemoryMax 'brave-browser-capped-*'
grep oom_kill /proc/vmstat            # kernel OOM kills since boot
```

[configuration.md](configuration.md#swap-zram-and-earlyoom) has the variable
summary and [reference.md](reference.md) every default.
