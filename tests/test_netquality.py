"""Network quality: statistics, ratings, causes, parsers, the real probes against loopback servers, one warning."""

from __future__ import annotations

import socket
import struct
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from streamstack import netquality as nq
from streamstack.netquality import Alerter, ProbeError, Sample, Target, Wifi


def samples(rtts: list[float | None], error: str = "timeout") -> list[Sample]:
    return [Sample(float(i), r, "" if r is not None else error) for i, r in enumerate(rtts)]


def test_summarize_rtt_jitter_and_loss() -> None:
    stats = nq.summarize(samples([20.0, 30.0, None, 20.0]))
    assert stats.rtt_ms == pytest.approx(23.333, abs=0.01)
    assert stats.jitter_ms == pytest.approx(10.0)
    assert stats.loss_pct == 25.0
    assert stats.count == 4


def test_summarize_edge_cases() -> None:
    assert nq.summarize([]).rtt_ms is None
    one = nq.summarize(samples([12.0]))
    assert one.rtt_ms == 12.0 and one.jitter_ms == 0.0
    gone = nq.summarize(samples([None, None], "dns"))
    assert gone.rtt_ms is None and gone.loss_pct == 100.0 and gone.last_error == "dns"


@pytest.mark.parametrize(
    ("rtt", "jitter", "loss", "expected"),
    [
        (30, 3, 0, "good"),
        (30, 150, 0, "degraded"),  # the acceptance case: injected 150 ms jitter
        (30, 3, 5, "degraded"),  # and 5 % loss
        (180, 3, 0, "degraded"),
        (30, 300, 0, "poor"),
        (30, 3, 12, "poor"),
        (500, 3, 0, "poor"),
    ],
)
def test_rate(rtt: float, jitter: float, loss: float, expected: str) -> None:
    assert nq.rate(rtt, jitter, loss) == expected


def test_worst_and_median() -> None:
    assert nq.worst(["good", "poor", "degraded"]) == "poor"
    assert nq.worst([]) == "good"
    assert nq.median([None, 3.0, 1.0, 2.0]) == 2.0
    assert nq.median([None]) is None


def test_problems_name_what_is_out_of_range() -> None:
    assert nq.problems(30, 150, 5) == ["5% loss", "150 ms jitter"]
    assert nq.problems(30, 3, 0) == []


def test_diagnose() -> None:
    all_bad = {"Teams": "degraded", "Meet": "degraded", "Zoom": "degraded"}
    one_bad = {"Teams": "degraded", "Meet": "good", "Zoom": "good"}
    assert nq.diagnose("good", all_bad, "good", None) == ""
    assert "internet connection" in nq.diagnose("degraded", all_bad, "good", None)
    assert "local network" in nq.diagnose("degraded", all_bad, "degraded", None)
    assert nq.diagnose("degraded", one_bad, "good", None).startswith("Teams")
    weak = Wifi("wlan0", -82.0, 54.0, 3.0)
    assert "Wi-Fi (signal -82 dBm)" in nq.diagnose("degraded", all_bad, "good", weak)
    retries = Wifi("wlan0", -50.0, 300.0, 30.0)
    assert "30% retries" in nq.diagnose("poor", all_bad, "good", retries)
    assert "provider" in nq.diagnose("offline", all_bad, "good", None)
    assert "router" in nq.diagnose("offline", all_bad, "offline", None)
    assert nq.diagnose("offline", all_bad, "offline", Wifi("wlan0", None, None, None)).startswith("Wi-Fi")


def test_parse_target_validation() -> None:
    assert nq.parse_target({"name": " Teams ", "host": "a.example.com", "port": 3478, "kind": "stun"}) == Target(
        "Teams", "a.example.com", 3478, "stun"
    )
    assert nq.parse_target({"name": "v6", "host": "2001:db8::1", "port": 443}).kind == "tcp"
    for bad in (
        "text",
        {"name": "", "host": "a.com", "port": 1},
        {"name": "x", "host": "bad host!", "port": 1},
        {"name": "x", "host": "a.com", "port": 0},
        {"name": "x", "host": "a.com", "port": True},
        {"name": "x", "host": "a.com", "port": 1, "kind": "icmp"},
    ):
        with pytest.raises(ValueError):
            nq.parse_target(bad)


def test_parsers() -> None:
    route = "Iface\tDestination\tGateway\tFlags\nwlan0\t00000000\t0101A8C0\t0003\nwlan0\t0001A8C0\t00000000\t0001\n"
    assert nq.default_gateway(route) == "192.168.1.1"
    assert nq.default_gateway("Iface\tDestination\tGateway\tFlags\n") is None
    wireless = (
        "Inter-| sta-|   Quality        |   Discarded packets\n face | tus | link level noise |  nwid\n"
        " wlan0: 0000   58.  -52.  -256        0      0\n"
    )
    assert nq.parse_proc_wireless(wireless) == {"wlan0": -52.0}
    link = "Connected to aa:bb\n\tsignal: -61 dBm\n\ttx bitrate: 866.7 MBit/s VHT-MCS 9\n"
    assert nq.parse_iw_link(link) == {"signal_dbm": -61.0, "rate_mbps": 866.7}
    assert nq.parse_iw_retry_pct("\ttx packets:\t1000\n\ttx retries:\t150\n") == 15.0
    assert nq.parse_iw_retry_pct("nothing") is None


def test_wifi_weak() -> None:
    assert Wifi("w", -80.0, None, None).weak
    assert Wifi("w", -50.0, None, 20.0).weak
    assert not Wifi("w", -50.0, None, 2.0).weak
    assert not Wifi("w", None, None, None).weak


# -- the real probes, against servers on loopback ----------------------------------------------------------------------


@contextmanager
def stun_server(answer: bool = True) -> Iterator[int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.2)
    done = threading.Event()

    def serve() -> None:
        while not done.is_set():
            try:
                data, peer = sock.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError:
                return
            if answer and len(data) == 20:
                sock.sendto(struct.pack(">HHI", 0x0101, 0, nq.STUN_COOKIE) + data[8:20], peer)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield sock.getsockname()[1]
    finally:
        done.set()
        thread.join(2)
        sock.close()


def test_stun_probe_measures_a_round_trip() -> None:
    with stun_server() as port:
        rtt = nq.SocketTransport().stun("127.0.0.1", port, 1.0)
    assert 0.0 < rtt < 500.0


def test_stun_probe_times_out_without_an_answer() -> None:
    with stun_server(answer=False) as port, pytest.raises(ProbeError) as err:
        nq.SocketTransport().stun("127.0.0.1", port, 0.2)
    assert err.value.kind == "timeout"


def test_tcp_probe_and_a_refused_port() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        assert nq.SocketTransport().tcp("127.0.0.1", port, 1.0) >= 0.0
    finally:
        server.close()
    with pytest.raises(ProbeError):
        nq.SocketTransport().tcp("127.0.0.1", port, 0.3)


def test_an_unresolvable_host_is_a_dns_error() -> None:
    with pytest.raises(ProbeError) as err:
        nq.SocketTransport().tcp("no-such-host.invalid", 443, 0.5)
    assert err.value.kind == "dns"


def test_stun_answer_check() -> None:
    request, txid = nq.stun_request()
    assert len(request) == 20 and request[8:20] == txid
    good = struct.pack(">HHI", 0x0101, 0, nq.STUN_COOKIE) + txid
    assert nq.is_stun_answer(good, txid)
    assert not nq.is_stun_answer(good, b"x" * 12)
    assert not nq.is_stun_answer(b"short", txid)


# -- the bandwidth test ------------------------------------------------------------------------------------------------


class FakeFetcher(nq.Fetcher):
    def __init__(self, down: tuple[int, float] | None, up: tuple[int, float] | None) -> None:
        self.down, self.up = down, up

    def download(self, url: str, seconds: float) -> tuple[int, float]:
        if self.down is None:
            raise ProbeError("no-route", "down failed")
        return self.down

    def upload(self, url: str, payload: bytes, timeout: float) -> tuple[int, float]:
        if self.up is None:
            raise ProbeError("no-route", "up failed")
        assert len(payload) == nq.BANDWIDTH_UP_BYTES
        return self.up


def test_bandwidth_and_its_verdict() -> None:
    result = nq.measure_bandwidth(FakeFetcher((25_000_000, 4.0), (1_500_000, 2.0)))
    assert result.down_mbps == 50.0 and result.up_mbps == 6.0
    assert result.verdict == "enough"
    assert nq.Bandwidth(3.5, 50.0).verdict == "tight"
    assert nq.Bandwidth(1.0, 50.0).verdict == "short"
    assert nq.Bandwidth(None, 50.0).verdict == "unknown"


def test_bandwidth_with_one_direction_failing_still_reports_the_other() -> None:
    result = nq.measure_bandwidth(FakeFetcher(None, (1_500_000, 4.0)))
    assert result.down_mbps is None and result.up_mbps == 3.0


def test_bandwidth_with_no_route_raises() -> None:
    with pytest.raises(ProbeError):
        nq.measure_bandwidth(FakeFetcher(None, None))


# -- one warning per episode -------------------------------------------------------------------------------------------


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def feed(alerter: Alerter, clock: Clock, ratings: list[str], step: float = 5.0) -> list[tuple[str, str] | None]:
    out = []
    for r in ratings:
        clock.now += step
        out.append(alerter.update(r))
    return out


def test_one_warning_then_one_recovery() -> None:
    clock = Clock()
    alerter = Alerter(clock=clock)
    out = feed(alerter, clock, ["good", "degraded", "degraded", "degraded", "degraded", "good", "good", "good", "good"])
    assert [o for o in out if o] == [("warn", "degraded"), ("ok", "good")]


def test_a_single_bad_sample_is_not_a_warning() -> None:
    clock = Clock()
    assert not any(feed(Alerter(clock=clock), clock, ["good", "poor", "good", "poor", "good"]))


def test_a_worse_rating_warns_once_more() -> None:
    clock = Clock()
    out = feed(Alerter(clock=clock), clock, ["degraded"] * 3 + ["poor"] * 3)
    assert [o for o in out if o] == [("warn", "degraded"), ("warn", "poor")]


def test_flapping_inside_the_cooldown_stays_quiet() -> None:
    clock = Clock()
    alerter = Alerter(clock=clock, cooldown_s=120.0)
    ratings = (["degraded"] * 2 + ["good"] * 3) * 4  # four episodes in 100 s
    out = [o for o in feed(alerter, clock, ratings) if o and o[0] == "warn"]
    assert len(out) == 1


def test_unknown_ratings_are_ignored_and_reset_clears() -> None:
    clock = Clock()
    alerter = Alerter(clock=clock)
    assert alerter.update("unknown") is None
    feed(alerter, clock, ["poor", "poor"])
    alerter.reset()
    assert feed(alerter, clock, ["poor", "poor"])[-1] == ("warn", "poor")
