"""Network quality for calls: RTT, jitter and loss to the services a call depends on, and what is probably wrong.

Pure logic plus a small transport (`SocketTransport`) that is the only code touching the network, so every rule here
is tested with a fake transport and a fake clock:

    stats = summarize(window.samples())          # rtt, jitter, loss over the last RECENT probes
    rating = rate(stats.rtt_ms, stats.jitter_ms, stats.loss_pct)   # good | degraded | poor
    cause = diagnose(...)                        # "Wi-Fi", "your local network", "your internet provider", a service

What is sent, and nothing else: a 20-byte STUN binding request (UDP, answered by Teams' and Google's public STUN/TURN
servers) or a TCP connect (Zoom, anything that does not answer STUN), and a single ICMP echo (`ping`) to the default
gateway. A few hundred bytes per probe: a few kB a minute at the default 5 s interval. The one exception is the
on-demand bandwidth test (`measure_bandwidth`), which sends real payload for a few seconds and is therefore explicit,
rate-limited and confirmed during a call.
"""

from __future__ import annotations

import contextlib
import errno
import itertools
import re
import secrets
import socket
import statistics
import struct
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from streamstack import system

INTERVAL_S = 5.0
PROBE_TIMEOUT_S = 1.5
RECENT = 20  # probes the statistics look at (100 s at the default interval)
HISTORY = 60  # points of the sparkline (5 minutes at the default interval)
DNS_TTL_S = 300.0

# The rating bands. Injected 150 ms of jitter or 5 % loss is "degraded"; a call is unusable at the "poor" ones.
DEGRADED_RTT_MS, POOR_RTT_MS = 150.0, 400.0
DEGRADED_JITTER_MS, POOR_JITTER_MS = 30.0, 250.0
DEGRADED_LOSS_PCT, POOR_LOSS_PCT = 2.0, 10.0
RATINGS = ("good", "degraded", "poor", "offline")  # worst last

NEEDED_UP_MBPS = 3.0  # a 1080p30 call
ENOUGH_UP_FACTOR = 1.5  # headroom: 4.5 Mbit/s up is comfortable

MAX_TARGETS = 8
HOST_RE = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


@dataclass(frozen=True)
class Target:
    name: str
    host: str
    port: int
    kind: str  # "stun" (UDP binding request) or "tcp" (a connect)


DEFAULT_TARGETS: tuple[Target, ...] = (
    Target("Microsoft Teams", "worldaz.tr.teams.microsoft.com", 3478, "stun"),
    Target("Google Meet", "stun.l.google.com", 19302, "stun"),
    Target("Zoom", "zoom.us", 443, "tcp"),
)


def parse_target(raw: object) -> Target:
    """A target from a settings entry; ValueError (with a sentence for the person) when it is not usable."""
    if not isinstance(raw, dict):
        raise ValueError("a target is an object with name, host, port and kind")
    name, host, port, kind = raw.get("name"), raw.get("host"), raw.get("port"), raw.get("kind", "tcp")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
        raise ValueError("a target needs a name of 1 to 40 characters")
    if not isinstance(host, str) or not (HOST_RE.match(host.strip()) or _is_ip(host.strip())):
        raise ValueError(f"{host!r} is not a host name or an IP address")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("a target port is a number from 1 to 65535")
    if kind not in ("stun", "tcp"):
        raise ValueError("a target kind is stun or tcp")
    return Target(name.strip(), host.strip(), port, kind)


def _is_ip(text: str) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, text)
        except OSError:
            continue
        return True
    return False


# -- statistics -------------------------------------------------------------------------------------------------------


class ProbeError(Exception):
    """A probe that did not answer. `kind` is timeout, dns or no-route."""

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message or kind)
        self.kind = kind


@dataclass(frozen=True)
class Sample:
    at: float
    rtt_ms: float | None  # None: lost
    error: str = ""  # timeout | dns | no-route when lost


@dataclass(frozen=True)
class Stats:
    rtt_ms: float | None  # mean of the answered probes; None when none answered
    jitter_ms: float | None  # mean absolute difference between consecutive answered probes (RFC 3550 style)
    loss_pct: float
    count: int
    last_error: str = ""


class Window:
    """The recent probes of one target."""

    def __init__(self, size: int = HISTORY) -> None:
        self._samples: deque[Sample] = deque(maxlen=size)

    def add(self, sample: Sample) -> None:
        self._samples.append(sample)

    def clear(self) -> None:
        self._samples.clear()

    def samples(self) -> list[Sample]:
        return list(self._samples)

    def stats(self, recent: int = RECENT) -> Stats:
        return summarize(self.samples()[-recent:])


def summarize(samples: Sequence[Sample]) -> Stats:
    if not samples:
        return Stats(None, None, 0.0, 0)
    answered = [s.rtt_ms for s in samples if s.rtt_ms is not None]
    lost = [s for s in samples if s.rtt_ms is None]
    loss = 100.0 * len(lost) / len(samples)
    rtt = sum(answered) / len(answered) if answered else None
    jitter = (
        sum(abs(b - a) for a, b in itertools.pairwise(answered)) / (len(answered) - 1)
        if len(answered) > 1
        else (0.0 if answered else None)
    )
    return Stats(rtt, jitter, loss, len(samples), lost[-1].error if lost else "")


def rate(rtt_ms: float | None, jitter_ms: float | None, loss_pct: float) -> str:
    """good | degraded | poor from the three numbers (offline is decided by the caller: nothing answered)."""
    if loss_pct >= POOR_LOSS_PCT or (rtt_ms or 0.0) >= POOR_RTT_MS or (jitter_ms or 0.0) >= POOR_JITTER_MS:
        return "poor"
    if loss_pct >= DEGRADED_LOSS_PCT or (rtt_ms or 0.0) >= DEGRADED_RTT_MS or (jitter_ms or 0.0) >= DEGRADED_JITTER_MS:
        return "degraded"
    return "good"


def worst(ratings: Iterable[str]) -> str:
    return max(ratings, key=RATINGS.index, default="good")


def problems(rtt_ms: float | None, jitter_ms: float | None, loss_pct: float) -> list[str]:
    """What is out of range, as short phrases ("150 ms jitter", "5% loss")."""
    found = []
    if loss_pct >= DEGRADED_LOSS_PCT:
        found.append(f"{loss_pct:.0f}% loss")
    if jitter_ms is not None and jitter_ms >= DEGRADED_JITTER_MS:
        found.append(f"{jitter_ms:.0f} ms jitter")
    if rtt_ms is not None and rtt_ms >= DEGRADED_RTT_MS:
        found.append(f"{rtt_ms:.0f} ms round trip")
    return found


def median(values: Iterable[float | None]) -> float | None:
    kept = [v for v in values if v is not None]
    return statistics.median(kept) if kept else None


@dataclass(frozen=True)
class Wifi:
    iface: str
    signal_dbm: float | None
    rate_mbps: float | None
    retry_pct: float | None

    @property
    def weak(self) -> bool:
        return (self.signal_dbm is not None and self.signal_dbm <= -75) or (
            self.retry_pct is not None and self.retry_pct >= 15
        )


def diagnose(rating: str, targets: dict[str, str], gateway_rating: str | None, wifi: Wifi | None) -> str:
    """The likely cause of a problem, in words. `targets` maps a service name to its own rating."""
    if rating == "good":
        return ""
    bad = [name for name, r in targets.items() if r != "good"]
    if rating == "offline":
        if gateway_rating in ("good", "degraded"):
            return "your internet provider: the router answers but nothing beyond it does"
        if wifi is not None:
            return "Wi-Fi: the router does not answer"
        return "your router or cable: nothing answers"
    if wifi is not None and wifi.weak:
        detail = (
            f"signal {wifi.signal_dbm:.0f} dBm"
            if wifi.signal_dbm is not None and wifi.signal_dbm <= -75
            else (f"{wifi.retry_pct:.0f}% retries")
        )
        return f"Wi-Fi ({detail}): move closer to the access point or use a cable"
    if gateway_rating in ("degraded", "poor", "offline"):
        return "your local network: the router itself is slow or dropping packets"
    if len(bad) == 1 and len(targets) > 1:
        return f"{bad[0]}: the other services are fine"
    return "your internet connection: the router is fine but every service is affected"


# -- the transport: the only code that touches the network -------------------------------------------------------------


class Transport:
    """What a probe needs. The real one is `SocketTransport`; tests and the demo rig bring their own."""

    def stun(self, host: str, port: int, timeout: float) -> float:
        raise NotImplementedError

    def tcp(self, host: str, port: int, timeout: float) -> float:
        raise NotImplementedError

    def ping(self, host: str, timeout: float) -> float | None:
        """Round trip to a host by one ICMP echo, or None when ping is not available. Raises ProbeError when lost."""
        raise NotImplementedError

    def gateway(self) -> str | None:
        raise NotImplementedError

    def wifi(self) -> Wifi | None:
        raise NotImplementedError

    def probe(self, target: Target, timeout: float) -> float:
        return (
            self.stun(target.host, target.port, timeout)
            if target.kind == "stun"
            else self.tcp(target.host, target.port, timeout)
        )


STUN_COOKIE = 0x2112A442


def stun_request(txid: bytes | None = None) -> tuple[bytes, bytes]:
    """A STUN binding request (RFC 5389): 20 bytes, no attributes; and its transaction id."""
    txid = txid or secrets.token_bytes(12)
    return struct.pack(">HHI", 0x0001, 0, STUN_COOKIE) + txid, txid


def is_stun_answer(data: bytes, txid: bytes) -> bool:
    """A binding success (0x0101) or error (0x0111) response carrying our transaction id."""
    return (
        len(data) >= 20
        and data[4:8] == struct.pack(">I", STUN_COOKIE)
        and data[8:20] == txid
        and (struct.unpack(">H", data[:2])[0] in (0x0101, 0x0111))
    )


def _unreachable(exc: OSError) -> bool:
    return exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.EADDRNOTAVAIL)


class SocketTransport(Transport):
    """Real sockets. DNS answers are cached for DNS_TTL_S so a probe measures the path, not the resolver."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._dns: dict[tuple[str, int, int], tuple[float, tuple[int, Any]]] = {}

    def _resolve(self, host: str, port: int, kind: int, timeout: float) -> tuple[int, Any]:
        key = (host, port, kind)
        now = self._clock()
        cached = self._dns.get(key)
        if cached is not None and now - cached[0] < DNS_TTL_S:
            return cached[1]
        try:
            infos = socket.getaddrinfo(host, port, type=kind)
        except socket.gaierror as exc:
            raise ProbeError("dns", f"cannot resolve {host}: {exc.strerror or exc}") from exc
        except OSError as exc:
            raise ProbeError("no-route" if _unreachable(exc) else "dns", str(exc)) from exc
        if not infos:
            raise ProbeError("dns", f"{host} has no address")
        family, _type, _proto, _canon, sockaddr = infos[0]
        found = (family, sockaddr)
        self._dns[key] = (now, found)
        return found

    def stun(self, host: str, port: int, timeout: float) -> float:
        family, addr = self._resolve(host, port, socket.SOCK_DGRAM, timeout)
        request, txid = stun_request()
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            start = time.perf_counter()
            try:
                sock.sendto(request, addr)
                while True:
                    data, _ = sock.recvfrom(2048)
                    if is_stun_answer(data, txid):
                        return (time.perf_counter() - start) * 1000.0
                    if time.perf_counter() - start > timeout:
                        raise ProbeError("timeout")
            except TimeoutError as exc:
                raise ProbeError("timeout") from exc
            except OSError as exc:
                raise ProbeError("no-route" if _unreachable(exc) else "timeout", str(exc)) from exc

    def tcp(self, host: str, port: int, timeout: float) -> float:
        family, addr = self._resolve(host, port, socket.SOCK_STREAM, timeout)
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            start = time.perf_counter()
            try:
                sock.connect(addr)
            except TimeoutError as exc:
                raise ProbeError("timeout") from exc
            except OSError as exc:
                raise ProbeError("no-route" if _unreachable(exc) else "timeout", str(exc)) from exc
            return (time.perf_counter() - start) * 1000.0

    def ping(self, host: str, timeout: float) -> float | None:
        if not system.have("ping"):
            return None
        done = system.run(["ping", "-n", "-c", "1", "-W", str(max(1, round(timeout))), host], timeout=timeout + 2)
        if done is None:
            return None
        match = re.search(r"time[=<]([\d.]+)\s*ms", done.stdout or "")
        if done.returncode != 0 or not match:
            raise ProbeError("timeout")
        return float(match.group(1))

    def gateway(self) -> str | None:
        try:
            with open("/proc/net/route", encoding="ascii") as handle:
                return default_gateway(handle.read())
        except OSError:
            return None

    def wifi(self) -> Wifi | None:
        try:
            with open("/proc/net/wireless", encoding="ascii") as handle:
                found = parse_proc_wireless(handle.read())
        except OSError:
            return None
        if not found:
            return None
        iface, signal = next(iter(found.items()))
        rate_mbps, retry = None, None
        if system.have("iw"):
            link = system.run(["iw", "dev", iface, "link"], timeout=3)
            station = system.run(["iw", "dev", iface, "station", "dump"], timeout=3)
            if link is not None:
                parsed = parse_iw_link(link.stdout or "")
                signal = parsed.get("signal_dbm", signal)
                rate_mbps = parsed.get("rate_mbps")
            if station is not None:
                retry = parse_iw_retry_pct(station.stdout or "")
        return Wifi(iface, signal, rate_mbps, retry)


def default_gateway(route_table: str) -> str | None:
    """The default gateway from /proc/net/route (little-endian hex), or None."""
    for line in route_table.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 4 and cols[1] == "00000000" and int(cols[3], 16) & 2:
            with contextlib.suppress(ValueError):
                return socket.inet_ntoa(struct.pack("<I", int(cols[2], 16)))
    return None


def parse_proc_wireless(text: str) -> dict[str, float]:
    """Interface name to signal level in dBm from /proc/net/wireless."""
    found: dict[str, float] = {}
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        cols = rest.split()
        if len(cols) >= 3:
            with contextlib.suppress(ValueError):
                found[name.strip()] = float(cols[2].rstrip("."))
    return found


def parse_iw_link(text: str) -> dict[str, float]:
    found: dict[str, float] = {}
    if m := re.search(r"signal:\s*(-?\d+(?:\.\d+)?)\s*dBm", text):
        found["signal_dbm"] = float(m.group(1))
    if m := re.search(r"tx bitrate:\s*([\d.]+)\s*MBit/s", text):
        found["rate_mbps"] = float(m.group(1))
    return found


def parse_iw_retry_pct(text: str) -> float | None:
    """Retries as a share of transmitted packets from `iw dev X station dump`."""
    retries = re.search(r"tx retries:\s*(\d+)", text)
    packets = re.search(r"tx packets:\s*(\d+)", text)
    if not retries or not packets or int(packets.group(1)) == 0:
        return None
    return 100.0 * int(retries.group(1)) / int(packets.group(1))


# -- the bandwidth test ------------------------------------------------------------------------------------------------

BANDWIDTH_DOWN_URL = "https://speed.cloudflare.com/__down?bytes=10000000"
BANDWIDTH_UP_URL = "https://speed.cloudflare.com/__up"
BANDWIDTH_DOWN_S = 4.0
BANDWIDTH_UP_BYTES = 1_500_000


@dataclass(frozen=True)
class Bandwidth:
    up_mbps: float | None
    down_mbps: float | None

    @property
    def verdict(self) -> str:
        """enough | tight | short for a 1080p30 call's upload, or unknown."""
        if self.up_mbps is None:
            return "unknown"
        if self.up_mbps >= NEEDED_UP_MBPS * ENOUGH_UP_FACTOR:
            return "enough"
        return "tight" if self.up_mbps >= NEEDED_UP_MBPS else "short"


class Fetcher:
    """The two transfers of the bandwidth test. Returns (bytes moved, seconds it took)."""

    def download(self, url: str, seconds: float) -> tuple[int, float]:
        raise NotImplementedError

    def upload(self, url: str, payload: bytes, timeout: float) -> tuple[int, float]:
        raise NotImplementedError


class UrlFetcher(Fetcher):
    def download(self, url: str, seconds: float) -> tuple[int, float]:
        request = urllib.request.Request(url, headers={"User-Agent": "streamstack-network"})
        start = time.perf_counter()
        total = 0
        try:
            with urllib.request.urlopen(request, timeout=seconds + 3) as response:
                while time.perf_counter() - start < seconds:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
        except (urllib.error.URLError, OSError) as exc:
            raise ProbeError("no-route", f"the download failed: {exc}") from exc
        return total, time.perf_counter() - start

    def upload(self, url: str, payload: bytes, timeout: float) -> tuple[int, float]:
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"User-Agent": "streamstack-network", "Content-Type": "application/octet-stream"},
        )
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read(1024)
        except (urllib.error.URLError, OSError) as exc:
            raise ProbeError("no-route", f"the upload failed: {exc}") from exc
        return len(payload), time.perf_counter() - start


def measure_bandwidth(fetcher: Fetcher) -> Bandwidth:
    """Download for a few seconds, then upload a small fixed payload. ProbeError when neither works."""
    down = up = None
    errors: list[str] = []
    try:
        moved, secs = fetcher.download(BANDWIDTH_DOWN_URL, BANDWIDTH_DOWN_S)
        if secs > 0 and moved > 0:
            down = moved * 8 / secs / 1e6
    except ProbeError as exc:
        errors.append(str(exc))
    try:
        moved, secs = fetcher.upload(BANDWIDTH_UP_URL, bytes(BANDWIDTH_UP_BYTES), 15.0)
        if secs > 0:
            up = moved * 8 / secs / 1e6
    except ProbeError as exc:
        errors.append(str(exc))
    if down is None and up is None:
        raise ProbeError("no-route", errors[0] if errors else "nothing was transferred")
    return Bandwidth(None if up is None else round(up, 1), None if down is None else round(down, 1))


# -- one warning per episode -------------------------------------------------------------------------------------------


class Alerter:
    """Turns a stream of ratings into at most one warning per episode, so a flapping link is not a storm.

    A warning needs `confirm` bad ratings in a row; a worse rating inside the same episode warns once more; the episode
    ends after `recover` good ratings in a row (one "back to normal"); and a new episode within `cooldown_s` of the last
    warning stays quiet until the cooldown is over."""

    def __init__(
        self,
        confirm: int = 2,
        recover: int = 3,
        cooldown_s: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._confirm, self._recover, self._cooldown, self._clock = confirm, recover, cooldown_s, clock
        self.reset()

    def reset(self) -> None:
        self._bad = self._good = 0
        self._warned: str | None = None
        self._last: float | None = None

    def update(self, rating: str) -> tuple[str, str] | None:
        """("warn", rating) when to warn, ("ok", "good") when it is back to normal, else None."""
        if rating not in RATINGS:
            return None
        now = self._clock()
        if rating != "good":
            self._bad, self._good = self._bad + 1, 0
            if self._bad < self._confirm:
                return None
            if self._warned is None:
                if self._last is not None and now - self._last < self._cooldown:
                    return None
            elif RATINGS.index(rating) <= RATINGS.index(self._warned):
                return None
            self._warned, self._last = rating, now
            return ("warn", rating)
        self._good, self._bad = self._good + 1, 0
        if self._warned is not None and self._good >= self._recover:
            self._warned = None
            return ("ok", "good")
        return None
