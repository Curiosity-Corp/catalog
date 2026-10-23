# PIA VPN split tunnel and managed WiFi

Two pieces of workstation networking are managed declaratively by the
`developer_workstation` role: the Private Internet Access (PIA) daemon
settings, and a netplan WiFi profile. No credentials live in this repository.

## PIA: only Brave goes through the VPN

Applied by `tasks/pia-vpn.yml` on hosts whose hardware profile sets
`pia_vpn: true`.

| Setting | Value | Why |
|---|---|---|
| `splitTunnelEnabled` | `true` | Per-app routing instead of whole-host VPN. |
| `defaultRoute` | `false` | Apps not listed bypass the VPN. |
| `splitTunnelRules` | `pia_split_tunnel_apps`, mode `include` | Listed apps (default `/opt/brave.com/brave/brave`) are forced through the VPN. `piactl` rejects mode `vpn`. |
| `splitTunnelDNS` | `true` | PIA DNS applies only to tunnelled apps, so Ziti and internal names such as `vault.focuspass.com` still resolve for the rest of the host. Without it PIA takes over DNS host-wide and internal names stop resolving. |
| `killswitch` | `auto` | Tunnelled apps are blocked, not leaked, if the VPN drops. The host is never blocked. |
| `persistDaemon` | `true` | The daemon and killswitch stay active without the PIA GUI. |

How it works: PIA places every Brave process in the `piavpnonly` `net_cls`
cgroup (`/opt/piavpn/etc/cgroup/net_cls/piavpnonly`) and routes that cgroup
through the `wgpia0` interface. Nothing else is in the cgroup.

The role converges by comparing `piactl -u dump daemon-settings` with the
desired values, then applies only the drifted keys with `piactl -u
applysettings`, so a converged host reports no change. The daemon persists the
result in `/opt/piavpn/etc/settings.json`, which survives reboots.

Login and autostart:

- `piactl login` runs only when `/opt/piavpn/etc/account.json` says logged out
  and `pia_login_file` points at an existing root-only (0600) file with the
  username on line one and the password on line two. Provision that file from
  Bitwarden outside git; the task uses `no_log`.
- Connecting at desktop login comes from the PIA GUI autostart entry
  (`~/.config/autostart/pia-client.desktop`) plus the client setting
  `connectOnLaunch: true` in `~/.config/privateinternetaccess/clientsettings.json`.
  If the VPN is not up, Brave has no connectivity (killswitch) while the rest of
  the host works.

### Verify

```sh
# Host (direct) public IP
curl -s https://api.ipify.org; echo
# Public IP of a process inside the VPN cgroup (what Brave sees)
sudo sh -c 'echo $$ > /opt/piavpn/etc/cgroup/net_cls/piavpnonly/cgroup.procs; curl -s https://api.ipify.org'; echo
/opt/piavpn/bin/piactl get connectionstate
getent hosts vault.focuspass.com
for p in $(pgrep -x brave); do grep net_cls /proc/$p/cgroup; done
```

The two IPs must differ, the second must be a PIA exit address, the state must
be `Connected`, the internal name must resolve, and every Brave PID must show
`/piavpnonly`.

### Bitwarden mapping (names only)

| Bitwarden item | Status |
|---|---|
| `Privateinternetaccess / p1793859` | Active. This is the account the client logs in with. |
| `Privateinternetaccess / p4443450` | Deprecated, do not use. |
| `Privateinternetaccess / p7891906` | Deprecated, do not use. |

## Managed WiFi (netplan, NetworkManager renderer)

Applied by `tasks/wifi-netplan.yml` when `wifi_ssid` is set. Supply `wifi_ssid`,
`wifi_psk` and `wifi_nm_uuid` from extra-vars or a vault; they default to empty
and the task is skipped.

The role renders `/etc/netplan/90-wifi-<wifi_netplan_name>.yaml` (root, 0600)
with `renderer: NetworkManager` and `match: name: "wl*"`, validates it with
`netplan generate --root-dir` in a scratch directory first, and backs up then
removes legacy `50-wifi-*.yaml` files and the NM-written `90-NM-<uuid>.yaml`
for the same connection. The handler runs `netplan generate` only. Run
`netplan apply` in a maintenance window, since it can bounce live links. The
PSK is system-owned in `/etc/netplan`, not in a user keyring, and NetworkManager
autoconnects by default.

### Postmortem: WiFi lost after the 2026-10-23 reboot

Symptom: after reboot WiFi was disconnected and the password had to be
re-entered.

Root cause: not PIA. The WiFi card moved from PCI `0000:06:00.0` to
`0000:08:00.0`, so its interface name changed from `wlp6s0` to `wlp8s0`. The
only WiFi profile, `/etc/netplan/50-wifi-ravonics.yaml`, was pinned to `wlp6s0`
(networkd renderer), so after the rename no profile matched `wlp8s0` and
NetworkManager had nothing to connect with. `netplan-wpa-wlp6s0.service` waited
about 90 seconds for the missing device, which also delayed `network.target`
and therefore `piavpn.service`, which started about 90 seconds late. PIA's
killswitch only starts with the daemon and never touches WiFi association, so
it was not involved. Re-entering the password in nm-applet created
`90-NM-<uuid>.yaml`, which was still pinned to `wlp8s0`.

Fix: a single interface-agnostic profile (`wl*`) in `/etc/netplan`, the stale
`wlp6s0` file removed, PSK stored system-wide, autoconnect on. A future slot
change cannot orphan the profile.
