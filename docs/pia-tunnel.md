# PIA tunnel for chosen apps (fail-closed network namespace)

Catalog#145. Some apps must reach the internet only through Private Internet
Access (PIA), and must have no network at all when the VPN is down. The
`developer_workstation` role does this with a Linux network namespace instead of
PIA's desktop app split tunnel. Everything not listed (Chrome, Ziti, the system,
StreamStack and the rest) keeps the normal network.

Applied by `tasks/pia-tunnel.yml` on any desktop host whose hardware profile sets
`pia_tunnel_apps` to a non-empty list. `deskmeet-b760` lists `brave-browser` and
`qbittorrent` (catalog#147).

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
* Refusals (the app does not start): exit 71 when the namespace does not exist,
  72 when the app is already running outside the tunnel (a second launch would
  silently attach to that instance; quit it completely first). A menu launch has
  no terminal, so the launcher also says it where the person looks, as a desktop
  notification (`notify-send` as the user, on their session bus) and as a journal
  line (`journalctl -t curiosity-pia-run`):
  * 72: "Brave is already open without the VPN. Quit Brave (Ctrl+Shift+Q) and
    open it again to use PIA."
  * 71: "The PIA tunnel is down, so Brave was not started. It would run without
    the VPN otherwise."
  * Namespace present but no tunnel interface in it (the tunnel is being rebuilt):
    the app starts and has no network, the notification says "The PIA tunnel is
    down; Brave stays offline until it reconnects."

  The wording comes from the app definition (`display_name`, `quit_hint`).
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

## Port forwarding (qBittorrent)

An app definition with a `port_forward` entry takes PIA's forwarded port as its
listen port. `qbittorrent` has one, so `pia_tunnel_port_forward` is on for
`deskmeet-b760` (it defaults to on exactly when a routed app takes the port).

* **API.** PIA's `getSignature` and `bindPort` (port 19999 on the server's
  in-tunnel address, `server_vip` from `addKey`, TLS verified against PIA's CA by
  server name) answer only inside the tunnel, so `curiosity-pia-tunnel` calls them
  from inside the namespace (`ip netns exec pia curl ...`). `getSignature` returns
  a signed payload (port, expiry about two months out, and the account token) and
  a signature; `bindPort` with both holds the port for about 15 minutes. Payload,
  signature and token go to curl on stdin and never appear in an argument list,
  the journal or git. The signed grant is kept in `/var/lib/curiosity-pia-tunnel/portforward.json`
  (root, 0600), so a rebuilt tunnel to the same server gets the same port back
  without a new signature; a grant for another server, or with under a day left, is
  replaced.
* **Region.** A forwarded port belongs to one server, and PIA offers it only in
  some regions (none in the US). With port forwarding on, the tunnel therefore
  connects only to such regions, and with `pia_tunnel_region: auto` it keeps the
  server it had (the lowest-latency one at first) for as long as that server
  answers. An explicit `pia_tunnel_region` must be a port-forwarding region, or
  `up` fails and says which ones are. **Everything in the namespace, Brave included,
  leaves through that region**, so its exit IP is no longer in the US.
* **Renewal.** `curiosity-pia-tunnel-bind.timer` (every `pia_tunnel_bind_interval`,
  default 10 min, at most 14) runs `curiosity-pia-tunnel bind`, which re-binds the
  stored grant (or gets a new one when PIA refuses it), and then
  `curiosity-pia-run port-sync`. `up` does the same right after it builds a tunnel.
  A port failure never takes the tunnel down (the browser keeps its VPN); the bind
  unit fails visibly and retries. The bound port is published in
  `/run/curiosity-pia-tunnel/forwarded-port` (removed whenever the tunnel is torn
  down) and shown by `curiosity-pia-tunnel status` as `forwarded_port`.
* **App config.** `port_forward.config` names the app's config file (relative to
  the home). For qBittorrent the launcher sets `Session\Port` and
  `Connection\PortRangeMin` to the forwarded port and empties the interface and
  address bindings (`Session\Interface*`, `Connection\Interface*`). The old
  binding to the PIA desktop app's `wgpia0` would leave qBittorrent without a
  listen socket in the namespace, whose only interface is the tunnel (recreated
  with a new index at every rebuild). The first change keeps the user's file as
  `qBittorrent.conf.pre-pia-tunnel`. This runs at every launch (before the app
  starts) and every bind. **A running app is never edited**, because it rewrites
  its config on exit: if the port changes while qBittorrent runs, the user gets one
  notification ("PIA gave qBittorrent a new port (N). Quit qBittorrent (Ctrl+Q) and
  open it again to use it.") and the file is updated once it has quit. The port
  changes only when the server changes or the grant expires.
* **Retired helper.** `~/.local/bin/pia-qbt-port-sync` and the user units
  `pia-qbt-port-sync.{service,timer}` (hand-made, they read the PIA desktop app's
  port with `piactl`) are moved aside as `*.pre-pia-tunnel` backups, their
  enablement links removed, and the timer is stopped in the running user manager.
  The catalog-managed equivalent is the bind timer above; it logs every port
  change to the journal (`journalctl -t curiosity-pia-run -u curiosity-pia-tunnel-bind`).
* **qBittorrent has no network outside the namespace.** Its binary is diverted
  (`/usr/bin/qbittorrent` becomes the shim, the program runs from
  `qbittorrent.distrib`), so the menu, magnet links, `xdg-open` and a shell all
  start it in the tunnel. Its WebUI is disabled on dm45s2 (`WebUI\Enabled=false`);
  if it is enabled it listens inside the namespace, which the host cannot reach
  (the namespace has its own loopback). Reach it from the host with
  `sudo ip netns exec pia curl http://127.0.0.1:8080/`; a host-side forward is not
  built because the owner does not use the WebUI.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `pia_tunnel_apps` | profile key `pia_tunnel_apps`, else `[]` | Apps routed through PIA (keys of `pia_tunnel_app_definitions`). Empty removes the feature. |
| `pia_tunnel_region` | `auto` | `auto` picks the lowest-latency real region (virtual geo regions are skipped); or a PIA region id such as `us_chicago`. Without port forwarding `auto` can choose another region after a rebuild, so the exit IP changes; set a region for a stable exit. With port forwarding only port-forwarding regions qualify and `auto` keeps its server. |
| `pia_tunnel_port_forward` | on when a routed app has `port_forward` | Get and keep a forwarded port (see "Port forwarding"). |
| `pia_tunnel_bind_interval` | `10min` | Re-bind period, 1 to 14 minutes. |
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
   `scope_env`, `managed_policies`, `packages` (apt packages installed first),
   `display_name` and `quit_hint` (the refusal notices), `port_forward.config`
   (the app config that gets PIA's forwarded port) and `retire_files` /
   `retire_user_units` (hand-made helpers the routing replaces). qBittorrent is
   the template for an app whose binary is the package's ELF: divert it and set
   `command` to the `.distrib` file.
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
* With `region: auto` and no port forwarding the exit region can differ between
  rebuilds.
* Port forwarding moves the exit to a non-US region for every tunnelled app. If
  Brave must exit in the US, the choice is a second tunnel (a second namespace,
  not built) or no forwarded port.
* qBittorrent's second instance (a magnet link while it runs) attaches to the
  running one; it does not need the forwarded port to be applied again.
* PIA ports are available only while the tunnel is up. After a reboot, qBittorrent
  started before the first bind (about a minute after boot) listens on the port
  it had; the next bind prepares the config and notifies if the port differs.
