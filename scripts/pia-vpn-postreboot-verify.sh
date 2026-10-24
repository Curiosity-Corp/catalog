#!/usr/bin/env bash
# Read-only, idempotent post-reboot verification for the PIA split tunnel (Brave only)
# and the managed WiFi netplan profile. Changes nothing on the host.
#
# Run after the first reboot that follows applying the developer_workstation role:
#   bash scripts/pia-vpn-postreboot-verify.sh
# Exit codes: 0 all checks passed, 1 a check failed, 2 boot predates the WiFi fix
# (the boot-journal checks would be meaningless, so nothing is judged yet).
#
# Rollback note: nothing to roll back here. To undo the role's WiFi change, restore the
# netplan backups in /var/backups/netplan-wifi, run `sudo netplan generate`, and
# `sudo netplan apply` (never in a live multi-session window; see docs/pia-vpn-and-wifi.md).
set -u
P=/opt/piavpn/bin/piactl
fail=0
ok()  { printf 'PASS  %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fail=1; }
chk() { local msg=$1; shift; if "$@" >/dev/null 2>&1; then ok "$msg"; else bad "$msg"; fi; }

echo "boot: $(uptime -s)"

# The log-based WiFi checks only prove persistence on a boot that began after the fix.
fix_ts=$(sudo -n stat -c %Y /etc/netplan/90-wifi-wifi.yaml 2>/dev/null || echo 0)
boot_ts=$(date -d "$(uptime -s)" +%s)
if [ "$fix_ts" -gt "$boot_ts" ]; then
  echo "PENDING  this boot ($(uptime -s)) predates the WiFi fix ($(date -d "@$fix_ts" '+%F %T')); reboot, then re-run"
  exit 2
fi

# --- WiFi (netplan, NetworkManager renderer, interface-agnostic) ---
chk "managed netplan file is on disk"            sudo -n test -f /etc/netplan/90-wifi-wifi.yaml
chk "no stale per-interface wifi netplan files"  bash -c '! sudo -n ls /etc/netplan | grep -E "^(50-wifi-|90-NM-)"'
chk "no netplan-wpa-wlp* unit generated"         bash -c '! ls /run/systemd/system 2>/dev/null | grep -q "netplan-wpa-wl"'
chk "no netplan-wpa failure this boot"           bash -c '! journalctl -b --no-pager 2>/dev/null | grep -q "Dependency failed for netplan-wpa"'
wl=$(nmcli -t -f DEVICE,TYPE device | awk -F: '$2=="wifi"{print $1; exit}')
chk "wifi device ${wl:-?} is connected"          bash -c "nmcli -t -f DEVICE,STATE device | grep -q '^${wl}:connected'"
chk "Ravonics autoconnect is yes"                bash -c '[ "$(nmcli -g connection.autoconnect connection show Ravonics)" = yes ]'
chk "Ravonics PSK is system-owned (flags 0)"     bash -c '[ "$(nmcli -g 802-11-wireless-security.psk-flags connection show Ravonics)" = 0 ]'
# NetworkManager always logs "secrets are required" once, then "No new secrets needed" when it
# finds the system-owned PSK. A manual password shows up as a need-auth that never resolves
# (reason 'no-secrets') or an extra "secrets are required" without the matching resolution.
chk "wifi came up without a manual password"     bash -c '
  j=$(journalctl -b --no-pager -u NetworkManager 2>/dev/null)
  req=$(grep -c "secrets are required" <<<"$j"); ok=$(grep -c "No new secrets needed" <<<"$j")
  ! grep -q "reason .no-secrets." <<<"$j" && [ "$req" -le "$ok" ]'

# --- PIA daemon and split tunnel ---
chk "piavpn enabled"                             bash -c '[ "$(systemctl is-enabled piavpn)" = enabled ]'
chk "piavpn active"                              systemctl is-active --quiet piavpn
chk "PIA connected"                              bash -c "[ \"\$($P get connectionstate)\" = Connected ]"
chk "PIA logged in"                              sudo -n jq -e '.loggedIn == true' /opt/piavpn/etc/account.json
chk "split tunnel: Brave-only include rule"      sudo -n jq -e '.splitTunnelEnabled == true and .defaultRoute == false and .splitTunnelDNS == true and (.splitTunnelRules | length == 1) and .splitTunnelRules[0].path == "/opt/brave.com/brave/brave" and .splitTunnelRules[0].mode == "include"' /opt/piavpn/etc/settings.json
chk "daemon OOM score is -900"                   bash -c '[ "$(cat /proc/$(systemctl show piavpn -p MainPID --value)/oom_score_adj)" = -900 ]'
chk "internal DNS still resolves (Ziti)"         getent hosts vault.focuspass.com

# --- Egress: host direct, Brave's cgroup through the VPN ---
host_ip=$(curl -s --max-time 10 https://api.ipify.org)
brave_ip=$(sudo -n sh -c 'echo $$ > /opt/piavpn/etc/cgroup/net_cls/piavpnonly/cgroup.procs; curl -s --max-time 10 https://api.ipify.org')
echo "host IP: ${host_ip:-none}   Brave-path IP: ${brave_ip:-none}"
[ -n "$host_ip" ] && [ -n "$brave_ip" ] && [ "$host_ip" != "$brave_ip" ] && ok "Brave egress differs from host (VPN in use)" || bad "Brave egress differs from host (VPN in use)"
vpn=$($P get vpnip 2>/dev/null)
echo "PIA tunnel address: ${vpn:-unknown}"
chk "only Brave processes are in the VPN cgroup" bash -c 'for p in $(cat /opt/piavpn/etc/cgroup/net_cls/piavpnonly/cgroup.procs); do tr "\0" " " </proc/$p/cmdline 2>/dev/null | grep -q "^/opt/brave.com/brave/brave" || [ "$p" = "$$" ] || exit 1; done'

echo
[ "$fail" -eq 0 ] && echo "ALL CHECKS PASSED" || echo "SOME CHECKS FAILED"
exit "$fail"
