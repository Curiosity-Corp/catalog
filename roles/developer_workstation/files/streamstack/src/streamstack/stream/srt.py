"""SRT link measurements: the round-trip time to a receiver, and the latency that follows from it.

ffmpeg's libsrt does not report statistics, so StreamStack measures the path itself. An SRT listener answers the
first packet of any caller's handshake (the "induction" request) without keeping a connection, so sending that one
packet from a plain UDP socket and timing the reply gives the real round trip, on the real port, over the real route.
It opens nothing on the receiver and carries no stream.

The receiver's latency buffer has to cover a retransmission: a lost packet is noticed after about one round trip and
asked for again, taking another. The SRT deployment guide recommends 3 to 7 round trips (more when more is lost), so
`recommended_latency_ms` multiplies the measured round trip by 4 on a clean link and by more when probes went
unanswered, never below `MIN_LATENCY_MS`.

What is *not* here: packet loss and retransmission counts of the stream itself. They live inside libsrt, and ffmpeg
does not expose them; the Console shows what StreamStack can measure (round trip and its spread, how many probes
were answered, the latency in force) and says so.
"""

from __future__ import annotations

import os
import socket
import statistics
import struct
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

MIN_LATENCY_MS = 80  # below this a single retransmission over any real network is already late
MAX_LATENCY_MS = 8000  # the most a destination may ask for (model.py)
DEFAULT_LATENCY_MS = 120  # used when nothing could be measured
PROBE_TIMEOUT_S = 0.6
MONITOR_S = 10.0
WINDOW = 30  # round trips kept for the spread
FRESH_S = 30.0  # a measurement newer than this is reused when the sink reconnects

HANDSHAKE_SIZE = 64
INDUCTION = 1
CONTROL_HANDSHAKE = 0x80000000


def induction_request(socket_id: int, timestamp_us: int) -> bytes:
    """The 64 bytes a caller sends first: a control packet of type handshake, handshake type INDUCTION."""
    header = struct.pack(">IIII", CONTROL_HANDSHAKE, 0, timestamp_us & 0xFFFFFFFF, 0)
    body = struct.pack(
        ">IHHIIIIII",
        4,  # version 4: what a caller says in induction
        0,  # encryption field
        2,  # extension field
        int.from_bytes(os.urandom(3), "big"),  # initial packet sequence number
        1500,  # MTU
        8192,  # maximum flow window
        INDUCTION,
        socket_id,
        0,  # SYN cookie: the listener assigns it in its answer
    )
    return header + body + bytes(16)  # peer IP address field


def is_induction_reply(data: bytes, socket_id: int) -> bool:
    """True for a listener's answer to our induction request (and not for anything else that reached the socket)."""
    if len(data) < HANDSHAKE_SIZE:
        return False
    word0, _, _, dest = struct.unpack_from(">IIII", data, 0)
    kind = struct.unpack_from(">I", data, 16 + 20)[0]  # the handshake type: 7th field of the 48 byte body
    return word0 == CONTROL_HANDSHAKE and kind == INDUCTION and dest == socket_id


def induction_rtt_ms(host: str, port: int, timeout: float = PROBE_TIMEOUT_S) -> float | None:
    """One round trip in milliseconds, or None when no listener answered in `timeout` seconds."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    except OSError:
        return None
    family, kind, proto, _, address = infos[0]
    socket_id = int.from_bytes(os.urandom(4), "big") or 1
    try:
        sock = socket.socket(family, kind, proto)
    except OSError:
        return None
    with sock:
        sock.settimeout(timeout)
        try:
            sent = time.perf_counter()
            sock.sendto(induction_request(socket_id, int(sent * 1e6)), address)
            deadline = sent + timeout
            while True:
                data, _ = sock.recvfrom(2048)
                received = time.perf_counter()
                if is_induction_reply(data, socket_id):
                    return (received - sent) * 1000.0
                if received >= deadline:
                    return None
        except OSError:  # a timeout, an ICMP refusal or an unreachable network all mean "no answer"
            return None


@dataclass
class Probe:
    sent: int
    rtts_ms: list[float] = field(default_factory=list)

    @property
    def answered(self) -> int:
        return len(self.rtts_ms)

    @property
    def loss_percent(self) -> float:
        return round(100.0 * (self.sent - self.answered) / self.sent, 1) if self.sent else 0.0

    @property
    def rtt_ms(self) -> float | None:
        """The median: one slow answer must not move the latency."""
        return statistics.median(self.rtts_ms) if self.rtts_ms else None


def probe(
    host: str,
    port: int,
    count: int = 3,
    gap_s: float = 0.05,
    timeout: float = PROBE_TIMEOUT_S,
    rtt: Callable[[str, int, float], float | None] = induction_rtt_ms,
) -> Probe:
    result = Probe(sent=count)
    for i in range(count):
        value = rtt(host, port, timeout)
        if value is not None:
            result.rtts_ms.append(value)
        if i + 1 < count:
            time.sleep(gap_s)
    return result


def recommended_latency_ms(rtt_ms: float, loss_percent: float = 0.0) -> int:
    """The receiver latency that covers retransmissions on a path with this round trip."""
    multiplier = 4 if loss_percent <= 0 else 5 if loss_percent <= 10 else 6 if loss_percent <= 20 else 7
    return int(min(MAX_LATENCY_MS, max(MIN_LATENCY_MS, round(rtt_ms * multiplier))))


Prober = Callable[[str, int], Probe]


class SrtLink:
    """What StreamStack knows about one SRT destination's path: probes it while the destination runs.

    `configured` is the destination's `latency_ms` option: a number of milliseconds, or "auto" to follow the
    measured round trip. A connection's latency is fixed when it is made (SRT cannot change it mid-stream), so
    `resolve()` is asked each time the sink (re)connects; `stats()` is what the Console shows.
    """

    def __init__(
        self,
        host: str,
        port: int,
        mode: str,
        configured: int | str,
        prober: Prober | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.host, self.port, self.mode = host, port, mode
        self.auto = configured == "auto"
        self.configured_ms = DEFAULT_LATENCY_MS if self.auto else int(configured)
        self._prober = prober or (lambda h, p: probe(h, p))
        self._clock, self._wall = clock, wall
        self._lock = threading.Lock()
        self._window: deque[float] = deque(maxlen=WINDOW)
        self._last: Probe | None = None
        self._measured_at: float | None = None
        self._probes = 0
        self._unanswered = 0
        self._applied: int | None = None
        self._first = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- measuring ------------------------------------------------------------------------------------------

    @property
    def measurable(self) -> bool:
        """A listener does not know where its caller is, so only a caller can measure the path."""
        return self.mode == "caller"

    def start(self) -> None:
        if not self.measurable or self._thread is not None:
            self._first.set()
            return
        self._thread = threading.Thread(target=self._monitor, name=f"srt-{self.host}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._first.set()

    def _monitor(self) -> None:
        while not self._stop.is_set():
            self.measure()
            self._first.set()
            self._stop.wait(MONITOR_S)

    def measure(self) -> Probe:
        result = self._prober(self.host, self.port)
        with self._lock:
            self._last = result
            self._measured_at = self._wall()
            self._probes += result.sent
            self._unanswered += result.sent - result.answered
            self._window.extend(result.rtts_ms)
        return result

    # -- the latency in force --------------------------------------------------------------------------------

    def resolve(self, wait_s: float = 0.8) -> int:
        """The latency for a connection made now (a fixed option, or derived from the measured round trip)."""
        if not self.auto:
            latency = self.configured_ms
        else:
            if self.measurable and self._fresh() is None:
                self._first.wait(wait_s)
            latency = self._derived() or DEFAULT_LATENCY_MS
        self._applied = latency
        return latency

    def _fresh(self) -> Probe | None:
        with self._lock:
            if self._last is None or self._measured_at is None or self._wall() - self._measured_at > FRESH_S:
                return None
            return self._last

    def _derived(self) -> int | None:
        with self._lock:
            last = self._last
        if last is None or last.rtt_ms is None:
            return None
        return recommended_latency_ms(last.rtt_ms, last.loss_percent)

    # -- what the Console shows ------------------------------------------------------------------------------

    def stats(self) -> dict[str, object]:
        with self._lock:
            window = list(self._window)
            last, at = self._last, self._measured_at
            probes, unanswered = self._probes, self._unanswered
        rtt = last.rtt_ms if last else None
        recommended = recommended_latency_ms(rtt, last.loss_percent) if (last and rtt is not None) else None
        applied = self._applied if self._applied is not None else (None if self.auto else self.configured_ms)
        return {
            "mode": self.mode,
            "auto": self.auto,
            "measurable": self.measurable,
            "latency_ms": applied,
            "configured_ms": None if self.auto else self.configured_ms,
            "recommended_ms": recommended,
            "rtt_ms": None if rtt is None else round(rtt, 1),
            "rtt_min_ms": round(min(window), 1) if window else None,
            "rtt_max_ms": round(max(window), 1) if window else None,
            "probes": probes,
            "probe_loss_percent": round(100.0 * unanswered / probes, 1) if probes else 0.0,
            "age_s": None if at is None else round(max(0.0, self._wall() - at), 1),
            "hint": self._hint(rtt, recommended, applied, probes, unanswered),
        }

    def _hint(self, rtt: float | None, recommended: int | None, applied: int | None, probes: int, lost: int) -> str:
        if not self.measurable:
            return "Listening: the round trip is not known until a caller connects, so the latency is the number set."
        if probes and lost == probes:
            return "The receiver did not answer the round-trip probe (a firewall may drop it); latency is not derived."
        if rtt is None:
            return "Measuring the round trip."
        if not self.auto and applied is not None and recommended is not None and applied < recommended * 0.75:
            return (
                f"Latency {applied} ms is under 4 round trips of {rtt:.0f} ms: a lost packet may arrive too late. "
                f"About {recommended} ms, or Auto, is safer."
            )
        if self.auto and applied is not None and recommended is not None and recommended > applied * 1.5:
            return f"The path got slower; the latency becomes {recommended} ms at the next reconnect."
        return ""
