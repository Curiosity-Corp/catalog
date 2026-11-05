# PIA tunnel for chosen apps (fail-closed network namespace)

Catalog#145. Some apps must reach the internet only through Private Internet
Access (PIA), and must have no network at all when the VPN is down. The
`developer_workstation` role does this with a Linux network namespace instead of
PIA's desktop app split tunnel. Everything not listed (Chrome, Ziti, the system,
StreamStack and the rest) keeps the normal network.

Applied by `tasks/pia-tunnel.yml` on any desktop host whose hardware profile sets
`pia_tunnel_apps` to a non-empty list. `deskmeet-b760` lists `brave-browser`.

## How it works

```
 host network namespace                         namespace "pia"
 ----------------------                         -----------------------------
 curiosity-pia-tunnel (root) ---- PIA API --->  lo   (loopback)
   token, server list, addKey                   wgpia (WireGuard)  --> default route
   UDP socket stays in the host namespace  ---> nothing else: no eth, no wlan
 normal apps -> normal route                    nftables: policy drop except lo/wgpia
                                                IPv6 disabled, resolver = PIA DNS only
```

* The namespace holds loopback and one WireGuard interface. The WireGuard UDP
  socket is created in the host namespace and the interface is then moved into
  `pia`, so the encrypted packets leave through the host link while the app sees
  only the tunnel. If the tunnel is down the app has no route, so it has no
  network. There is nothing to leak through: no other interface, no IPv6, no
  host resolver (`/etc/netns/pia/resolv.conf` and `nsswitch.conf` are
  bind-mounted over the host's by `ip netns exec`).
* `curiosity-pia-tunnel.service` (root, oneshot, `RemainAfterExit`) registers
  with PIA and builds the tunnel at boot. `curiosity-pia-tunnel-watchdog.timer`
  checks every `pia_tunnel_watchdog_interval` (default 1 min); when the
  last WireGuard handshake is older than `pia_tunnel_max_handshake_age` (default
  240 s), the interface is gone, or the session is older than
  `pia_tunnel_session_max_hours` (default 0 = never), it rebuilds the tunnel. A
  rebuild registers the new key first and replaces the old tunnel only after
  that works, so a PIA API outage never removes a working tunnel.
* `curiosity-pia-run APP` (`/usr/local/bin`) starts an app inside the namespace.
  The user stage runs the app in a memory-capped `systemd-run --user --scope`
  (where the app definition has one) and calls `sudo -n curiosity-pia-run --exec`.
  The sudoers rule (`/etc/sudoers.d/curiosity-pia-tunnel`) allows only that
  subcommand. The root stage re-validates the environment against an allow-list
  (display, session bus, locale, XDG, GTK, Qt; never `LD_*`), enters the namespace,
  drops to the calling user with `setpriv` and starts the command that the
  root-owned `config.json` names for that app. The caller cannot choose the
  command.
* Refusals (the app does not start, with a desktop notification and a message):
  exit 71 when the namespace does not exist, 72 when the app is already running
  outside the tunnel (a second launch would silently attach to that instance;
  quit it completely first).
* Every launch by name or path goes through the launcher:
  * `/usr/bin/brave-browser-stable` (the package's symlink) is moved aside with
    `dpkg-divert` (diversion owned by `curiosity-pia-tunnel`, file kept as
    `brave-browser-stable.distrib`, package upgrades unpack to that name) and the
    tunnel shim sits at the original path. Everything that resolves there is
    tunnelled: `/usr/bin/brave-browser`, the `x-www-browser` and
    `gnome-www-browser` alternatives, `xdg-open` when Brave is the default browser,
    autostart entries, systemd units, shell scripts and menus. The app's `command`
    is the package's own script (`/opt/brave.com/brave/brave-browser`), so the shim
    never calls itself (the launcher also refuses a command that is a shim).
  * System desktop entries are copied to `/usr/local/share/applications` with each
    `Exec=` rewritten (marker `X-Curiosity-Pia-Tunnel-App`); a user entry in
    `~/.local/share/applications` that shadows the system one is replaced (the
    original is kept as `*.pre-pia-tunnel`).
  * `legacy_wrappers` (home-relative paths such as `.local/bin/brave-capped`) that
    exist are replaced by a shim into the launcher, the original kept as
    `*.pre-pia-tunnel`. A wrapper the user does not have is not created.
  * Removing an app from the list restores all of it: the diversion is removed and
    the package's file returns, entries and wrappers come back.
* The memory cap (`scope`, `app-browser.slice`, `MemoryHigh` 16G, `MemoryMax` 24G
  for Brave) is the old `brave-capped` wrapper's. `scope_env` keeps its overrides:
  `BRAVE_MEMORY_HIGH=8G brave-browser` applies to that launch.
* Brave also gets the managed policy `WebRtcIPHandling:
  default_public_interface_only`, so WebRTC offers only the tunnel address.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `pia_tunnel_apps` | profile key `pia_tunnel_apps`, else `[]` | Apps routed through PIA (keys of `pia_tunnel_app_definitions`). Empty removes the feature. |
| `pia_tunnel_region` | `auto` | `auto` picks the lowest-latency real region (virtual geo regions are skipped); or a PIA region id such as `us_chicago`. `auto` can choose another region after a rebuild, so the exit IP changes; set a region for a stable exit. |
| `pia_tunnel_login_file` | `pia_login_file`, else `/etc/curiosity/pia-tunnel/login` | Root-only login file (see below). |
| `pia_tunnel_netns`, `pia_tunnel_interface` | `pia`, `wgpia` | Names. |
| `pia_tunnel_mtu`, `pia_tunnel_keepalive` | 1420, 25 | WireGuard settings. |
| `pia_tunnel_max_handshake_age` | 240 | Seconds without a handshake before the tunnel counts as dead. |
| `pia_tunnel_session_max_hours` | 0 | Rebuild at this age (0 = never). |
| `pia_tunnel_watchdog_interval` | `1min` | Watchdog period. |
| `pia_tunnel_users` | `dev_users` names | Users the sudoers rule covers. |

The feature is mutually exclusive with `pia_vpn` (the PIA desktop app split
tunnel, see `docs/pia-vpn-and-wifi.md`); the role asserts that.

## Provision the PIA login (once per host)

No credentials are in git. The login file has the username on line 1 and the
password on line 2, mode 0600, owner root. Provision it from the Bitwarden item
for PIA (vault.focuspass.com) without printing either value:

```sh
source scripts/bw-session.sh        # unlocks bw in this shell (do not pipe it)
{ bw get username "<PIA item>"; echo; bw get password "<PIA item>"; echo; } \
  | sudo install -m 0600 -o root -g root /dev/stdin /etc/curiosity/pia-tunnel/login
unset BW_SESSION
```

`bw get` prints no trailing newline, so the `echo` between the values is
required. Until the file exists the role installs everything but does not start
the tunnel, and the routed app does not launch (no tunnel, no network, by
design). The next ansible-pull starts the tunnel.

## Add an app

1. Make sure `pia_tunnel_app_definitions` (`defaults/main.yml`) has an entry
   for it. Brave is the template:
   `command` (the real binary, as a list), `guard_exe` (the executable the
   stray-process guard looks for), `desktop_ids`, `desktop_commands` (every
   command string the package's `Exec=` lines start with), `diverted` (absolute
   paths the package owns that must hold the shim; the app's `command` must then
   name the real executable, not one of these) or `shims` (extra names in
   `/usr/local/bin`), optional `legacy_wrappers`, `scope` (slice and memory caps),
   `scope_env` and `managed_policies`.
2. Add its name to the host's hardware profile:

   ```yaml
   hardware_profiles:
     deskmeet-b760:
       pia_tunnel_apps: [brave-browser, thunderbird]
   ```

That is the whole change. Commit it; the next pull wraps the desktop entry and
the shim. `curiosity-pia-run thunderbird` then runs it in the namespace.

## Operate and verify

```sh
sudo curiosity-pia-tunnel status                 # connected: yes/no, region, peer, handshake age
sudo ip netns exec pia curl -s https://www.privateinternetaccess.com/api/client/v2/ip
curl -s https://www.privateinternetaccess.com/api/client/v2/ip   # the normal IP, for comparison
journalctl -u curiosity-pia-tunnel -u curiosity-pia-tunnel-watchdog
```

Fail-closed check: `sudo systemctl stop curiosity-pia-tunnel` leaves the
namespace in place with no interface; the app loses its network, not its jail.
`systemctl start curiosity-pia-tunnel` brings it back.

## Known limits

* Running the Brave executable itself, `/opt/brave.com/brave/brave` or
  `/opt/brave.com/brave/brave-browser`, starts it in the normal network. Every
  other way to start it is routed (see above). Decision (catalog#146): no extra
  guard. An nftables rule cannot tell such a process from the tunnelled one by
  cgroup (they share the user's session cgroups), and diverting the AppArmor
  confined ELF would break its profile. The stray-process guard still refuses a
  tunnelled launch while a plain Brave runs.
* The default browser of an account (`xdg-settings get default-web-browser`) is its
  own choice. On dm45s2 it is Firefox, so links opened from other apps open
  Firefox, outside the tunnel. To open them in Brave, set it to
  `brave-browser.desktop` (the entry that the role wraps); then `xdg-open`, Slack
  and terminals start the tunnelled Brave, or hand the link to the running one
  (Brave's single-instance socket is a file, shared between host and namespace).
* The stray-process guard blocks a tunnelled launch while the app is running
  outside the tunnel. After the role is first applied, quit the app completely
  once and start it again from its entry.
* With `region: auto` the exit region can differ between rebuilds.
