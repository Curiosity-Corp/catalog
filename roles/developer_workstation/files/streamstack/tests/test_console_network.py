"""The Console's network monitor: ratings from fake probes, one warning, targets, the bandwidth rules, the demo twin."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from streamstack import netquality as nq
from streamstack.console.errors import ActionError
from streamstack.console.network import DemoNetwork, NetworkController, empty_state, read_settings
from streamstack.console.rig import Rig
from streamstack.netquality import ProbeError, Transport, Wifi


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeTransport(Transport):
    """Per-host scripted round trips, cycled: a number is a round trip, None or an error name is a lost probe."""

    def __init__(self) -> None:
        self.script: dict[str, list[float | str | None]] = {}
        self.gw: str | None = "10.0.0.1"
        self.gw_rtt: float | None = 2.0
        self.wifi_link: Wifi | None = None
        self.calls: list[str] = []
        self.counts: dict[str, int] = {}

    def _next(self, host: str) -> float:
        self.calls.append(host)
        seq = self.script.get(host, [20.0])
        n = self.counts.get(host, 0)
        self.counts[host] = n + 1
        item = seq[n % len(seq)]
        if item is None or isinstance(item, str):
            raise ProbeError(item or "timeout")
        return float(item)

    def stun(self, host: str, port: int, timeout: float) -> float:
        return self._next(host)

    tcp = stun

    def ping(self, host: str, timeout: float) -> float | None:
        if self.gw_rtt is None:
            raise ProbeError("timeout")
        return self.gw_rtt

    def gateway(self) -> str | None:
        return self.gw

    def wifi(self) -> Wifi | None:
        return self.wifi_link


class FakeFetcher(nq.Fetcher):
    def __init__(self) -> None:
        self.runs = 0

    def download(self, url: str, seconds: float) -> tuple[int, float]:
        self.runs += 1
        return 25_000_000, 4.0

    def upload(self, url: str, payload: bytes, timeout: float) -> tuple[int, float]:
        return 1_500_000, 2.0


class Call:
    def __init__(self) -> None:
        self.on = False

    def active(self) -> bool:
        return self.on


HOSTS = [t.host for t in nq.DEFAULT_TARGETS]


def make(tmp_path: Path | None = None) -> tuple[NetworkController, FakeTransport, Clock, list[tuple[str, str]], Call]:
    clock, transport, call = Clock(), FakeTransport(), Call()
    toasts: list[tuple[str, str]] = []
    controller = NetworkController(
        lambda level, text: toasts.append((level, text)),
        transport=transport,
        fetcher=FakeFetcher(),
        call=call,
        settings_path=None if tmp_path is None else tmp_path / "net.json",
        clock=clock,
        parallel=False,
    )
    return controller, transport, clock, toasts, call


def run(controller: NetworkController, clock: Clock, rounds: int) -> None:
    for _ in range(rounds):
        clock.now += 5
        controller.tick()


def test_a_healthy_link_is_good() -> None:
    controller, _, clock, toasts, _ = make()
    assert controller.state()["rating"] == "unknown"
    run(controller, clock, 5)
    state = controller.state()
    assert state["rating"] == "good" and state["cause"] == ""
    assert state["rtt_ms"] == 20.0 and state["jitter_ms"] == 0.0 and state["loss_pct"] == 0.0
    assert [t["name"] for t in state["targets"]] == ["Microsoft Teams", "Google Meet", "Zoom"]
    assert state["gateway"]["host"] == "10.0.0.1" and state["gateway"]["rtt_ms"] == 2.0
    assert len(state["history"]) == 5
    assert toasts == []


def test_jitter_degrades_it_with_one_warning_and_a_cause() -> None:
    controller, transport, clock, toasts, _ = make()
    run(controller, clock, 4)
    transport.script = {h: [20.0, 170.0] for h in HOSTS}
    run(controller, clock, 30)
    state = controller.state()
    assert state["rating"] == "degraded"
    assert state["jitter_ms"] == pytest.approx(150.0)
    assert "150 ms jitter" in state["text"]
    assert "internet connection" in state["cause"]
    assert [t[0] for t in toasts] == ["warn"]  # one notification, not a storm
    assert "Likely cause" in toasts[0][1]


def test_loss_degrades_it() -> None:
    controller, transport, clock, toasts, _ = make()
    transport.script = {h: [20.0] * 19 + [None] for h in HOSTS}
    run(controller, clock, 40)
    state = controller.state()
    assert state["rating"] == "degraded"
    assert state["loss_pct"] == 5.0
    assert "5% loss" in state["text"]
    assert [t[0] for t in toasts] == ["warn"]


def test_the_gateway_is_blamed_when_it_is_slow() -> None:
    controller, transport, clock, _, _ = make()
    for host in HOSTS:
        transport.script[host] = [200.0]
    transport.gw_rtt = 400.0
    run(controller, clock, 6)
    state = controller.state()
    assert state["rating"] == "degraded"
    assert "local network" in state["cause"]


def test_wifi_is_blamed_when_the_signal_is_weak() -> None:
    controller, transport, clock, _, _ = make()
    for host in HOSTS:
        transport.script[host] = [200.0]
    transport.wifi_link = Wifi("wlan0", -83.0, 20.0, 5.0)
    run(controller, clock, 6)
    state = controller.state()
    assert "Wi-Fi" in state["cause"]
    assert state["wifi"]["weak"] is True


def test_no_route_is_offline_and_recovers() -> None:
    controller, transport, clock, toasts, _ = make()
    run(controller, clock, 3)
    for host in HOSTS:
        transport.script[host] = ["no-route"]
    transport.gw = None
    run(controller, clock, 30)
    state = controller.state()
    assert state["rating"] == "offline" and "No route" in state["text"]
    assert state["history"][-1] is None
    assert toasts[-1][0] == "error"
    transport.script = {h: [20.0] for h in HOSTS}
    transport.gw = "10.0.0.1"
    run(controller, clock, 30)
    assert controller.state()["rating"] == "good"
    assert toasts[-1] == ("ok", "The network is back to normal.")


def test_one_blocked_service_does_not_degrade_the_rest() -> None:
    controller, transport, clock, _, _ = make()
    transport.script["zoom.us"] = ["timeout"]
    run(controller, clock, 30)
    state = controller.state()
    assert state["rating"] == "good"
    assert {t["name"]: t["rating"] for t in state["targets"]}["Zoom"] == "offline"


def test_the_warning_says_when_a_call_is_on() -> None:
    controller, transport, clock, toasts, call = make()
    call.on = True
    transport.script = {h: [300.0] for h in HOSTS}
    run(controller, clock, 6)
    assert "during your call" in toasts[0][1]


def test_targets_are_configurable_validated_and_saved(tmp_path: Path) -> None:
    controller, _, clock, _, _ = make(tmp_path)
    run(controller, clock, 4)
    controller.act("network.set", {"targets": [{"name": "Office VPN", "host": "10.1.2.3", "port": 443, "kind": "tcp"}]})
    state = controller.state()
    assert [t["name"] for t in state["settings"]["targets"]] == ["Office VPN"]
    assert state["rating"] == "unknown" and state["history"] == []  # a new set of targets starts fresh
    assert read_settings(tmp_path / "net.json")["targets"][0]["host"] == "10.1.2.3"
    controller.act("network.set", {"reset_targets": True})
    assert len(controller.state()["settings"]["targets"]) == 3
    controller.act("network.set", {"enabled": False})
    assert controller.state()["enabled"] is False


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"nope": 1},
        {"targets": []},
        {"targets": "x"},
        {"targets": [{"name": "a", "host": "bad host", "port": 1}]},
        {"targets": [{"name": "a", "host": "a.com", "port": 1}, {"name": "a", "host": "b.com", "port": 1}]},
        {"enabled": "yes"},
    ],
)
def test_bad_settings_are_a_400(params: dict[str, Any]) -> None:
    controller, *_ = make()
    with pytest.raises(ActionError) as err:
        controller.act("network.set", params)
    assert err.value.status == 400
    assert len(controller.state()["settings"]["targets"]) == 3


def test_damaged_settings_fall_back_to_the_defaults(tmp_path: Path) -> None:
    path = tmp_path / "net.json"
    path.write_text('{"enabled": false, "targets": [{"name": "x", "host": "bad host", "port": 1}]}', encoding="utf-8")
    settings = read_settings(path)
    assert settings["enabled"] is False and len(settings["targets"]) == 3
    path.write_text("not json", encoding="utf-8")
    assert read_settings(path)["enabled"] is True


def wait_for(controller: NetworkController, done: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = controller.state()
        if done(state["bandwidth"]):
            return state["bandwidth"]
        time.sleep(0.02)
    raise AssertionError("the bandwidth test did not finish")


def test_bandwidth_runs_reports_and_is_rate_limited() -> None:
    controller, _, clock, toasts, _ = make()
    controller.act("network.bandwidth", {})
    result = wait_for(controller, lambda b: b["state"] == "done")
    assert result["down_mbps"] == 50.0 and result["up_mbps"] == 6.0 and result["verdict"] == "enough"
    assert "plenty for" in toasts[-1][1]
    with pytest.raises(ActionError) as err:
        controller.act("network.bandwidth", {})
    assert err.value.status == 409 and "Wait" in str(err.value)
    clock.now += 61
    controller.act("network.bandwidth", {})
    wait_for(controller, lambda b: b["state"] == "done" and b["blocked"] is not None)


def test_bandwidth_needs_confirmation_during_a_call() -> None:
    controller, _, _, _, call = make()
    call.on = True
    with pytest.raises(ActionError) as err:
        controller.act("network.bandwidth", {})
    assert err.value.status == 409 and "confirm" in str(err.value)
    assert controller.state()["bandwidth"]["state"] == "idle"
    with pytest.raises(ActionError) as err:
        controller.act("network.bandwidth", {"confirm": "yes"})
    assert err.value.status == 400
    controller.act("network.bandwidth", {"confirm": True})
    wait_for(controller, lambda b: b["state"] == "done")


def test_a_failed_bandwidth_test_says_so() -> None:
    controller, _, _, toasts, _ = make()

    class Down(nq.Fetcher):
        def download(self, url: str, seconds: float) -> tuple[int, float]:
            raise ProbeError("no-route", "offline")

        def upload(self, url: str, payload: bytes, timeout: float) -> tuple[int, float]:
            raise ProbeError("no-route", "offline")

    controller._fetcher = Down()
    controller.act("network.bandwidth", {})
    result = wait_for(controller, lambda b: b["state"] == "error")
    assert "failed" in result["error"]
    assert toasts[-1][0] == "error"


def test_facts_for_preflight() -> None:
    controller, _, clock, _, _ = make()
    assert controller.facts() is None
    run(controller, clock, 4)
    facts = controller.facts()
    assert facts is not None and facts["rtt_ms"] == 20.0 and facts["up_mbps"] is None
    controller.act("network.bandwidth", {})
    wait_for(controller, lambda b: b["state"] == "done")
    facts = controller.facts()
    assert facts is not None and facts["up_mbps"] == 6.0
    clock_wall = controller._wall()
    controller._wall = lambda: clock_wall + 3600  # a result an hour old is not used
    stale = controller.facts()
    assert stale is not None and stale["up_mbps"] is None


def test_the_empty_state_is_unavailable() -> None:
    assert empty_state()["available"] is False


# -- the demo rig ------------------------------------------------------------------------------------------------------


@pytest.fixture
def rig() -> Iterator[Rig]:
    rig = Rig.demo()
    rig.start()
    yield rig
    rig.stop()


def until(rig: Rig, probe: Callable[[dict[str, Any]], bool], timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = rig.snapshot()["network"]
        if probe(state):
            return state
        time.sleep(0.05)
    raise AssertionError(f"timed out: {rig.snapshot()['network']['text']}")


def test_demo_is_good_then_150ms_jitter_degrades_with_one_toast(rig: Rig) -> None:
    toasts: list[tuple[str, str]] = []
    rig.on_toast = lambda level, text: toasts.append((level, text))
    until(rig, lambda n: n["rating"] == "good")
    rig.act("network.demo", {"jitter_ms": 150})
    state = until(rig, lambda n: n["rating"] == "degraded")
    assert "150 ms jitter" in state["text"] or "jitter" in state["text"]
    time.sleep(1.5)
    assert len([t for t in toasts if t[0] == "warn"]) == 1


def test_demo_five_percent_loss_degrades(rig: Rig) -> None:
    until(rig, lambda n: n["rating"] == "good")
    rig.act("network.demo", {"loss_pct": 5})
    state = until(rig, lambda n: n["rating"] == "degraded", timeout=15)
    # The window holds a few dozen probes, so one lost probe reads as 3-7 %, not exactly 5 %.
    assert state["loss_pct"] == pytest.approx(5.0, abs=2.0)


def test_demo_no_route_and_reset(rig: Rig) -> None:
    rig.act("network.demo", {"route": False})
    until(rig, lambda n: n["rating"] == "offline")
    rig.act("network.demo_reset", {})
    until(rig, lambda n: n["rating"] == "good")


def test_demo_wifi_and_bandwidth(rig: Rig) -> None:
    rig.act("network.demo", {"rtt_ms": 300, "wifi": {"signal_dbm": -85}})
    state = until(rig, lambda n: n["rating"] == "degraded")
    assert "Wi-Fi" in state["cause"]
    rig.act("network.demo", {"wifi": None, "rtt_ms": 28, "up_mbps": 2})
    rig.act("network.bandwidth", {})
    state = until(rig, lambda n: n["bandwidth"]["state"] == "done")
    assert state["bandwidth"]["verdict"] == "short"


def test_demo_script_is_validated(rig: Rig) -> None:
    with pytest.raises(ActionError):
        rig.act("network.demo", {"loss_pct": 400})
    with pytest.raises(ActionError):
        rig.act("network.demo", {"wifi": "weak"})
    assert isinstance(rig.network, DemoNetwork)
