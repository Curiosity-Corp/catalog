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

### Postmortem: PIA left Disconnected after OOM kills (2026-10-23)

Symptom: PIA showed Disconnected and Brave had no network.

Root cause: the kernel OOM killer killed `piavpn.service` three times (18:45,
18:56 and 19:09). systemd restarted the daemon each time (`Restart=always`),
but a restarted daemon does not reconnect by itself. Auto-connect
(`connectOnLaunch`) only fires from the GUI client, which the same OOM event
had also killed. With the VPN down the killswitch kept Brave blocked, as
designed, while the rest of the host stayed online.

Fix: the drop-in `/etc/systemd/system/piavpn.service.d/10-autoconnect-oom.conf`
(template `piavpn-autoconnect-oom.conf.j2`) sets `OOMScoreAdjust=-900`, so the
daemon is a last-choice OOM victim, and an `ExecStartPost` waits for the daemon
socket and runs `piactl connect` on every start. Variables:
`pia_daemon_oom_score_adjust` and `pia_autoconnect_on_daemon_start`. A converge
only runs `systemctl daemon-reload`; it never restarts the daemon, so a live VPN
is not bounced and the drop-in takes effect at the next daemon restart.

Verified on the host with `systemctl restart piavpn` and with
`systemctl kill -s KILL piavpn` (the signal the OOM killer sends): both times
the daemon came back Connected on its own, Brave's cgroup exited through a PIA
address, the host stayed on its normal line, and `oom_score_adj` read `-900`.

#### What caused the memory exhaustion (found 2026-10-23)

The machine was not leaking memory on its own. Between 17:45 and 18:30 three
unbounded `sudo systemd-run` jobs launched from a session in
`~/repos/hypervoid/overview` overlapped: `hh-seed`
(`/var/lib/hh-storage/seed.sh`, 38.1G memory peak over 16 min),
`hh-backup-test` (`/usr/local/sbin/hh-backup`, restic, 9.9G peak) and
`hh-verify` (`hh-storage-sync verify`, 6G peak). None had `MemoryMax`, so
together they used all 61G of RAM and the 16G of swap (`earlyoom` logged swap
at 0% free). The kernel then killed small processes one after another
(`piavpn`, claude sessions, `canonctl-ptp-webcam`, `ffmpeg`, `rsync`) before
the jobs themselves ended with `Failed with result 'oom-kill'`. The 12 minute
rhythm of the `piavpn` restarts matches those jobs, not a PIA problem.

Follow-up for the owner of the `hh-*` scripts: launch them with
`--property=MemoryMax=<limit>` (and `MemorySwapMax=0`) so a runaway job is
killed inside its own cgroup instead of taking the host's services with it,
and do not run the seed, backup and verify jobs at the same time. The
`OOMScoreAdjust=-900` on `piavpn.service` above only protects the VPN daemon.

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

## PARKED: WiFi persistence proof awaits a reboot (2026-10-24)

The only unverified claim is that WiFi autoconnects after a boot with the managed
`90-wifi-wifi.yaml` profile. Everything else is verified on the live host.

- **Why parked:** the workstation runs many live sessions, so rebooting, logging out or
  restarting the display manager is deferred to a maintenance window. Policy is tracked
  in the `policy-gap` item "Maintenance-window and reboot policy" in `mrh/okto`.
- **Deferred since:** the netplan fix was applied 2026-10-23 18:00 local; the machine
  last booted 2026-10-23 15:12, before the fix.
- **What runs after the next reboot:** `bash scripts/pia-vpn-postreboot-verify.sh`
  (read-only, idempotent; exits 2 while the boot predates the fix, 1 on a failed check,
  0 when everything passes).
- **Evidence that proves it:** exit 0 means the netplan-wpa failure and "secrets are
  required" are absent from this boot's journal, `wlp*` is connected with autoconnect
  `yes` and `psk-flags 0`, PIA is Connected, and Brave's cgroup egress IP differs from
  the host's. If a check fails, fix forward from its output.
- **Rollback:** see the header of the script (restore `/var/backups/netplan-wifi`).
