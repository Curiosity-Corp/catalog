"""The idle governor, the inotify trigger and the reader watch (#128). Time is injected; nothing touches /proc."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from streamstack import camerause
from streamstack.engine import idle

RealWatch = idle.ReaderWatch  # the autouse fixture replaces idle.ReaderWatch with a null watch


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def governor(clock: Clock, **kw) -> idle.IdleGovernor:
    return idle.IdleGovernor(clock=clock, after=10.0, confirm=0.25, wake_timeout=8.0, **kw)


def step(gov: idle.IdleGovernor, *, readers=False, demand=False, running=True) -> idle.Verdict:
    return gov.step(readers=readers, demand=demand, engine_running=running)


# -- config ------------------------------------------------------------------------------------------------------


def test_config_defaults_and_bad_values() -> None:
    assert idle.idle_mode({}) == "auto"
    assert idle.idle_mode({"STREAMSTACK_ENGINE_IDLE": "OFF"}) == "off"
    assert idle.idle_mode({"STREAMSTACK_ENGINE_IDLE": "nonsense"}) == "auto"
    assert idle.idle_after({}) == idle.IDLE_AFTER
    assert idle.idle_after({"STREAMSTACK_ENGINE_IDLE_AFTER": "30"}) == 30.0
    assert idle.idle_after({"STREAMSTACK_ENGINE_IDLE_AFTER": "soon"}) == idle.IDLE_AFTER
    assert idle.idle_after({"STREAMSTACK_ENGINE_IDLE_AFTER": "0"}) >= 1.0  # never a hair trigger
    assert idle.idle_ignore({"STREAMSTACK_ENGINE_IDLE_IGNORE": "pipewire, wireplumber"}) == {"pipewire", "wireplumber"}
    assert idle.idle_ignore({}) == set()


# -- the state machine -------------------------------------------------------------------------------------------


def test_it_idles_only_after_the_debounce_of_quiet() -> None:
    clock = Clock()
    gov = governor(clock)
    assert step(gov).action is None
    clock.advance(9.9)
    assert step(gov).action is None and gov.state == idle.RUNNING
    clock.advance(0.2)
    verdict = step(gov)
    assert verdict.action == "idle" and gov.state == idle.IDLE
    assert "no app has read the camera" in verdict.reason


def test_a_reader_resets_the_quiet_timer() -> None:
    clock = Clock()
    gov = governor(clock)
    clock.advance(9)
    step(gov, readers=True)
    clock.advance(9)
    assert step(gov).action is None  # quiet again, but only just
    clock.advance(10.5)
    assert step(gov).action == "idle"


def test_a_probe_that_opens_and_closes_does_not_flap() -> None:
    """Edge opens the device to list it and closes it at once: before idle that only restarts the quiet timer, and
    while idle the reader must persist for the confirm time before the engine is woken."""
    clock = Clock()
    gov = governor(clock)
    clock.advance(11)
    assert step(gov).action == "idle"
    # a probe: present for 0.1 s then gone
    assert step(gov, readers=True).action is None
    clock.advance(0.1)
    assert step(gov, readers=False).action is None
    clock.advance(0.1)
    assert step(gov, readers=True).action is None  # the confirm window restarted
    assert gov.state == idle.IDLE and gov.wakes == 0


def test_a_real_reader_wakes_it_after_the_confirm_window() -> None:
    clock = Clock()
    gov = governor(clock)
    clock.advance(11)
    step(gov)
    assert step(gov, readers=True).action is None
    clock.advance(0.3)
    verdict = step(gov, readers=True)
    assert verdict.action == "wake" and gov.state == idle.WAKING and gov.wakes == 1


def test_wake_ms_is_measured_to_the_running_engine() -> None:
    clock = Clock()
    gov = governor(clock)
    clock.advance(11)
    step(gov)
    step(gov, readers=True)
    clock.advance(0.3)
    step(gov, readers=True)
    clock.advance(0.9)
    assert step(gov, readers=True, running=False).action is None and gov.state == idle.WAKING
    clock.advance(0.1)
    step(gov, readers=True, running=True)
    assert gov.state == idle.RUNNING
    assert gov.wake_ms == pytest.approx(1000.0, abs=1.0)


def test_demand_wakes_at_once_and_keeps_it_awake() -> None:
    clock = Clock()
    gov = governor(clock)
    clock.advance(11)
    step(gov)
    assert step(gov, demand=True).action == "wake"
    clock.advance(1)
    step(gov, demand=True, running=True)
    clock.advance(60)
    assert step(gov, demand=True).action is None and gov.state == idle.RUNNING


def test_keep_alive_never_idles_and_wakes_an_idle_engine() -> None:
    clock = Clock()
    gov = governor(clock, keep_alive=True)
    clock.advance(120)
    assert step(gov).action is None and gov.state == idle.RUNNING
    gov.keep_alive = False
    assert step(gov).action is None  # the debounce starts when keep awake is switched off, not before
    clock.advance(11)
    assert step(gov).action == "idle"
    gov.keep_alive = True
    assert step(gov).action == "wake"


def test_a_faulting_engine_is_never_idled() -> None:
    clock = Clock()
    gov = governor(clock)
    clock.advance(500)
    assert step(gov, running=False).action is None and gov.state == idle.RUNNING
    clock.advance(500)
    assert step(gov, running=True).action is None  # the timer starts when it runs


def test_a_wake_that_never_produces_a_frame_stops_waiting() -> None:
    clock = Clock()
    gov = governor(clock)
    clock.advance(11)
    step(gov)
    step(gov, demand=True)
    assert gov.state == idle.WAKING
    clock.advance(9)
    step(gov, demand=True, running=False)
    assert gov.state == idle.RUNNING  # the supervisor owns the failure now


def test_switched_off_never_idles_and_leaves_idle() -> None:
    clock = Clock()
    gov = governor(clock, enabled=False)
    clock.advance(1000)
    assert step(gov).action is None
    gov.state = idle.IDLE
    assert step(gov).action == "wake"
    assert gov.snapshot()["mode"] == "off"


def test_force_running_leaves_idle() -> None:
    clock = Clock()
    gov = governor(clock)
    clock.advance(11)
    step(gov)
    assert gov.force_running() is True and gov.state == idle.RUNNING
    assert gov.force_running() is False


def test_the_snapshot_shape() -> None:
    gov = governor(Clock())
    assert set(gov.snapshot()) == {"mode", "state", "keep_alive", "after_s", "since_s", "reason", "wakes", "wake_ms"}


# -- inotify ---------------------------------------------------------------------------------------------------


def test_inotify_reports_an_open_and_a_missing_node_is_not_an_error(tmp_path: Path) -> None:
    try:
        events = idle.Inotify()
    except OSError:
        pytest.skip("no inotify in this sandbox")
    try:
        node = tmp_path / "node"
        node.write_bytes(b"")
        assert events.watch(str(node)) is True
        assert events.watch(str(tmp_path / "absent")) is False
        assert events.wait(0.05) is False
        with open(node, "rb"):
            pass
        assert events.wait(1.0) is True
        assert events.wait(0.05) is False
        os.unlink(node)
        assert events.wait(1.0) is True  # delete-self: the watch is dropped so a recreated node is watched again
        node.write_bytes(b"")
        assert events.watch(str(node)) is True
    finally:
        events.close()


def test_open_events_falls_back_to_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args, **kwargs):
        raise OSError(24, "too many open files")

    monkeypatch.setattr(idle, "Inotify", broken)
    assert isinstance(idle.open_events(), idle.PollEvents)


# -- the reader watch ------------------------------------------------------------------------------------------


class FakeScanner:
    def __init__(self) -> None:
        self.apps: list[camerause.Consumer] = []
        self.fail = False
        self.calls: list[tuple[set[str], set[str]]] = []

    def scan(self, devices: set[str], ignore=()) -> camerause.Scan:
        self.calls.append((set(devices), set(ignore)))
        if self.fail:
            raise PermissionError("/proc is hidden")
        return camerause.Scan(consumers=list(self.apps))


class FakeEvents:
    """Events on demand: `fire()` makes the next wait() report an open."""

    def __init__(self) -> None:
        self._flag = threading.Event()
        self.closed = False
        self.watched: list[str] = []

    def watch(self, path: str) -> bool:
        self.watched.append(path)
        return True

    def fire(self) -> None:
        self._flag.set()

    def wait(self, timeout: float) -> bool:
        hit = self._flag.wait(timeout)
        self._flag.clear()
        return hit

    def close(self) -> None:
        self.closed = True


def wait_until(check, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.01)
    return False


def test_the_first_scan_happens_at_start_and_names_the_apps() -> None:
    scanner = FakeScanner()
    scanner.apps = [camerause.Consumer(4242, "msedge", "/dev/video43")]
    watch = RealWatch(lambda: "/dev/video43", scanner=scanner, events=FakeEvents(), ignore={"pipewire"})
    watch.start()
    try:
        assert watch.latest.present and watch.latest.ok and watch.fresh()
        assert watch.latest.public()[0]["pid"] == 4242
        assert scanner.calls[0] == ({"/dev/video43"}, {"pipewire"})
    finally:
        watch.stop()


def test_an_open_event_triggers_a_scan_and_every_scan_ticks() -> None:
    scanner, events, ticks = FakeScanner(), FakeEvents(), []
    watch = RealWatch(
        lambda: "/dev/video43",
        scanner=scanner,
        events=events,
        on_tick=lambda: ticks.append(1),
        safety=60.0,
    )
    watch.start()
    try:
        assert not watch.latest.apps
        scanner.apps = [camerause.Consumer(7, "zoom", "/dev/video43")]
        before = watch.scans
        events.fire()
        assert wait_until(lambda: watch.latest.apps)
        assert watch.scans > before
        assert wait_until(lambda: len(ticks) >= 2)  # ticks continue between scans: a debounce can expire
    finally:
        watch.stop()
    assert events.closed


def test_a_failing_scan_counts_as_a_reader_and_is_not_fresh() -> None:
    scanner = FakeScanner()
    scanner.fail = True
    watch = RealWatch(lambda: "/dev/video43", scanner=scanner, events=FakeEvents())
    watch.start()
    try:
        assert watch.latest.present  # fail awake
        assert not watch.fresh()
    finally:
        watch.stop()


def test_a_scan_goes_stale() -> None:
    clock = Clock()
    watch = RealWatch(lambda: "/dev/video43", scanner=FakeScanner(), events=FakeEvents(), clock=clock)
    watch.scan()
    assert watch.fresh()
    clock.advance(idle.FRESH_FOR + 1)
    assert not watch.fresh()


def test_a_crashing_tick_hook_does_not_kill_the_sensor() -> None:
    calls = []

    def boom() -> None:
        calls.append(1)
        raise RuntimeError("bug in the consumer")

    watch = RealWatch(lambda: "/dev/video43", scanner=FakeScanner(), events=FakeEvents(), on_tick=boom, safety=60.0)
    watch.start()
    try:
        assert wait_until(lambda: len(calls) >= 3)
    finally:
        watch.stop()


def test_the_null_watch_is_never_fresh() -> None:
    watch = idle.NullWatch()
    watch.start()
    assert not watch.fresh()
    assert watch.scan().present is True  # it never claims to have seen nothing
    watch.stop()
