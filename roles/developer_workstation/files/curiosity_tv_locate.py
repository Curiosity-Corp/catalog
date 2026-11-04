"""Find the Samsung TV on the LAN by MAC address (shared by curiosity-tv and the display guard).

DHCP moves the TV (192.168.1.250, then .5, then .11), so a fixed address
goes stale. The MAC does not change: look it up in the kernel neighbour table
and, when it is not there, make the kernel ARP the local subnet once a minute.
While the entry is missing, the last address found by MAC wins over the fixed
host: the TV is on Wi-Fi, its entry ages out of the table, and falling back to
a dead lease made a running TV look powered off (catalog#138). The fixed host
is the fallback only until the MAC has been seen once. Installed to
/usr/local/lib/curiosity/.
"""
from __future__ import annotations

import contextlib
import ipaddress
import socket
import subprocess
import time

SWEEP_INTERVAL = 60
MAX_SWEEP_PREFIX = 22  # never sweep a subnet larger than a /22

_sweep_at = 0.0
_last_found: dict[str, str] = {}  # MAC -> last address the neighbour table gave


def _ip(*args: str) -> str:
    try:
        return subprocess.run(["ip", "-4", *args], capture_output=True, text=True,
                              timeout=5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def neighbour_ip(mac: str) -> str | None:
    """The IPv4 address the neighbour table holds for this MAC (reachable or stale)."""
    mac = mac.lower()
    for line in _ip("neigh", "show").splitlines():
        parts = line.split()
        if "lladdr" in parts and parts[parts.index("lladdr") + 1].lower() == mac \
                and parts[-1] not in ("FAILED", "INCOMPLETE"):
            return parts[0]
    return None


def arp_sweep() -> None:
    """Make the kernel ARP every address of the local subnets (one UDP datagram each, to port 9)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        for line in _ip("-o", "addr", "show", "scope", "global").splitlines():
            fields = line.split()
            if "inet" not in fields:
                continue
            try:
                net = ipaddress.ip_interface(fields[fields.index("inet") + 1]).network
            except ValueError:
                continue
            if net.prefixlen < MAX_SWEEP_PREFIX:
                continue
            for host in net.hosts():
                with contextlib.suppress(OSError):
                    sock.sendto(b"\0", (str(host), 9))


def resolve_tv_host(host: str, mac: str) -> str:
    """The TV's current address: found by MAC when one is given, else the last
    address found by MAC, else `host`."""
    global _sweep_at
    if not mac:
        return host
    key = mac.lower()
    found = neighbour_ip(mac)
    if found is None and time.time() - _sweep_at >= SWEEP_INTERVAL:
        _sweep_at = time.time()
        arp_sweep()
        time.sleep(2)
        found = neighbour_ip(mac)
    if found:
        _last_found[key] = found
        return found
    return _last_found.get(key, host)
