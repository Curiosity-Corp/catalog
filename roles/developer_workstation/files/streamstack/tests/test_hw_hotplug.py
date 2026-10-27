"""Hot-plug: udev events become one debounced re-probe, and only a real difference is emitted."""

from __future__ import annotations

import queue
import shutil
import threading
import time
from pathlib import Path

import pytest
from hardware_support import FakeMachine, studio_wave

from streamstack.hardware import schema
from streamstack.hardware.hotplug import SUBSYSTEMS, Change, Monitor, changed_sources, parse_udev_line
from streamstack.hardware.plan import resolve

CATALOGUE = schema.packaged()
WAIT = 5.0


class FakeUdev:
    """A scripted `udevadm monitor`: lines pushed by the test come out of `stdout`; terminate() ends the stream."""

    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.stdout = self
        self.terminated = False

    def __iter__(self):
        return self

    def __next__(self) -> str:
        line = self.lines.get(timeout=WAIT)
        if line is None:
            raise StopIteration
        return line

    def push(self, line: str) -> None:
        self.lines.put(line + "\n")

    def terminate(self) -> None:
        self.terminated = True
        self.lines.put(None)

    def wait(self, timeout: float | None = None) -> int:
        return 0


class Harness:
    def __init__(
        self, machine: FakeMachine, *, udev: bool = True, debounce: float = 0.05, backoff=(0.01,), clock=time.monotonic
    ) -> None:
        self.machine = machine
        self.changes: queue.Queue[Change] = queue.Queue()
        self.probes = 0
        self.processes: list[FakeUdev] = []
        self.argvs: list[list[str]] = []

        def take():
            self.probes += 1
            return machine.snapshot()

        def spawn(argv: list[str]) -> FakeUdev:
            self.argvs.append(argv)
            process = FakeUdev()
            self.processes.append(process)
            return process

        self.monitor = Monitor(
            take,
            lambda snap: resolve(snap, CATALOGUE),
            self.changes.put,
            debounce=debounce,
            poll=0.05,
            spawn=spawn,
            which=(lambda name: "/usr/bin/udevadm") if udev else (lambda name: None),
            backoff=backoff,
            clock=clock,
        )

    def wait_process(self, count: int = 1) -> FakeUdev:
        deadline = time.monotonic() + WAIT
        while len(self.processes) < count and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(self.processes) >= count, "udevadm monitor was not started"
        return self.processes[count - 1]

    def next_change(self) -> Change:
        return self.changes.get(timeout=WAIT)

    def quiet(self, seconds: float = 0.3) -> bool:
        try:
            self.changes.get(timeout=seconds)
        except queue.Empty:
            return True
        return False


@pytest.fixture
def machine(tmp_path: Path) -> FakeMachine:
    return studio_wave(FakeMachine(tmp_path))


@pytest.fixture
def harness(machine: FakeMachine):
    h = Harness(machine)
    h.monitor.start()
    yield h
    h.monitor.stop()


def unplug_wave(machine: FakeMachine) -> None:
    machine.remove_usb("0fd9:007d")
    machine.remove_alsa(3)
    machine.set_pipewire([], [], [])


def test_udev_event_lines_are_recognised() -> None:
    assert parse_udev_line("UDEV  [1234.5] add      /devices/pci0000:00/usb1/1-4 (usb)")
    assert parse_udev_line("UDEV  [1.0] remove   /devices/x/sound/card3 (sound)")
    assert not parse_udev_line("monitor will print the received events for:")
    assert not parse_udev_line("UDEV - the event which udev sends out after rule processing")
    assert not parse_udev_line("")
    assert not parse_udev_line("KERNEL[1.0] add /devices/x (usb)")


def test_changed_sources_ignores_volatile_fields(machine: FakeMachine) -> None:
    first = machine.snapshot()
    assert changed_sources(first, machine.snapshot()) == ()


def test_the_monitor_asks_udev_for_the_probe_subsystems(harness: Harness) -> None:
    harness.wait_process()
    argv = harness.argvs[0]
    assert argv[:3] == ["udevadm", "monitor", "--udev"]
    assert {a.split("=", 1)[1] for a in argv if a.startswith("--subsystem-match=")} == set(SUBSYSTEMS)
    assert harness.monitor.mode == "udev"


def test_unplugging_emits_one_change_with_the_plan_diff(harness: Harness, machine: FakeMachine) -> None:
    process = harness.wait_process()
    unplug_wave(machine)
    process.push("UDEV  [1.0] remove /devices/x/usb1/1-2 (usb)")
    change = harness.next_change()
    assert set(change.sources) == {"usb", "alsa", "pipewire"}
    assert "devices" in change.sections and "audio" in change.sections and "gpu_roles" not in change.sections
    assert change.old_plan.has("wave-xlr") and not change.new_plan.has("wave-xlr")
    assert change.new_plan.audio_interface() is None


def test_plugging_in_emits_a_change(machine: FakeMachine) -> None:
    unplug_wave(machine)
    h = Harness(machine)
    h.monitor.start()
    try:
        process = h.wait_process()
        machine.add_usb("0fd9:007d", "Elgato Wave XLR")
        process.push("UDEV  [2.0] add /devices/x/usb1/1-2 (usb)")
        change = h.next_change()
        assert change.new_plan.has("wave-xlr") and not change.old_plan.has("wave-xlr")
        assert "usb" in change.sources
    finally:
        h.monitor.stop()


def test_a_burst_of_events_is_one_probe(machine: FakeMachine, monkeypatch: pytest.MonkeyPatch) -> None:
    """The debounce is a quiet-period timer, so the burst is driven on a fake clock: on a CPU-starved runner the
    reader thread can stall longer than the real debounce between two lines, which is a real gap and a second probe
    (pipeline 96222). Time stands still while the 20 events arrive, then moves past the debounce once."""
    now = [100.0]
    h = Harness(machine, clock=lambda: now[0])
    pokes = threading.Semaphore(0)
    poke = h.monitor.poke

    def counted() -> None:
        poke()
        pokes.release()

    monkeypatch.setattr(h.monitor, "poke", counted)  # the reader thread calls self.poke() once per event line
    h.monitor.start()
    try:
        process = h.wait_process()
        baseline = h.probes
        unplug_wave(machine)
        for i in range(20):
            process.push(f"UDEV  [1.{i}] change /devices/x/usb1/1-2/1-2:1.{i % 3} (usb)")
        for _ in range(20):
            assert pokes.acquire(timeout=WAIT), "the reader did not take every event"
        assert h.probes == baseline, "a probe ran before the debounce interval passed"
        now[0] += 1.0
        h.next_change()
        assert h.quiet(0.3)
        assert h.probes - baseline == 1
    finally:
        h.monitor.stop()


def test_events_that_change_nothing_emit_nothing(harness: Harness) -> None:
    process = harness.wait_process()
    baseline = harness.probes
    process.push("UDEV  [1.0] change /devices/x/drm/card0 (drm)")
    deadline = time.monotonic() + WAIT
    while harness.probes == baseline and time.monotonic() < deadline:
        time.sleep(0.005)
    assert harness.probes == baseline + 1
    assert harness.quiet(0.2)


def test_header_lines_do_not_trigger_a_probe(harness: Harness) -> None:
    process = harness.wait_process()
    baseline = harness.probes
    process.push("monitor will print the received events for:")
    assert harness.quiet(0.3)
    assert harness.probes == baseline


def test_the_debounce_waits_for_a_quiet_interval(machine: FakeMachine) -> None:
    h = Harness(machine, debounce=0.3)
    h.monitor.start()
    try:
        process = h.wait_process()
        baseline = h.probes
        unplug_wave(machine)
        started = time.monotonic()
        process.push("UDEV  [1.0] remove /devices/x (usb)")
        time.sleep(0.15)
        process.push("UDEV  [1.1] remove /devices/y (usb)")  # restarts the interval
        h.next_change()
        assert time.monotonic() - started >= 0.4
        assert h.probes - baseline == 1
    finally:
        h.monitor.stop()


def test_udevadm_ending_is_restarted_and_probes_for_missed_events(harness: Harness, machine: FakeMachine) -> None:
    first = harness.wait_process()
    unplug_wave(machine)
    first.lines.put(None)  # udevadm died
    second = harness.wait_process(2)
    assert second is not first
    change = harness.next_change()  # the restart probes because events may have been missed
    assert not change.new_plan.has("wave-xlr")
    assert harness.monitor.mode == "udev"


def test_without_udevadm_it_polls(machine: FakeMachine) -> None:
    h = Harness(machine, udev=False)
    h.monitor.start()
    try:
        assert h.monitor.mode == "polling" and "udevadm is not installed" in h.monitor.error
        unplug_wave(machine)
        change = h.next_change()
        assert not change.new_plan.has("wave-xlr")
        assert h.processes == []
    finally:
        h.monitor.stop()


def test_a_failing_subscriber_does_not_end_hot_plug(machine: FakeMachine) -> None:
    seen: list[Change] = []
    calls = {"n": 0}

    def flaky(change: Change) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("subscriber bug")
        seen.append(change)

    process = FakeUdev()
    monitor = Monitor(
        machine.snapshot,
        lambda s: resolve(s, CATALOGUE),
        flaky,
        debounce=0.02,
        spawn=lambda argv: process,
        which=lambda n: "/usr/bin/udevadm",
    )
    monitor.start()
    try:
        unplug_wave(machine)
        process.push("UDEV  [1.0] remove /devices/x (usb)")
        deadline = time.monotonic() + WAIT
        while calls["n"] < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        machine.add_usb("0fd9:007d", "Elgato Wave XLR")
        process.push("UDEV  [2.0] add /devices/x (usb)")
        while not seen and time.monotonic() < deadline:
            time.sleep(0.005)
        assert seen and "failed" in (monitor.error or "")
    finally:
        monitor.stop()


def test_a_failing_probe_is_survived(machine: FakeMachine) -> None:
    good = machine.snapshot()
    state = {"fail": False}

    def take():
        if state["fail"]:
            raise OSError("sysfs vanished")
        return machine.snapshot()

    process = FakeUdev()
    changes: queue.Queue[Change] = queue.Queue()
    monitor = Monitor(
        take,
        lambda s: resolve(s, CATALOGUE),
        changes.put,
        debounce=0.02,
        spawn=lambda argv: process,
        which=lambda n: "/usr/bin/udevadm",
    )
    monitor.start(good)
    try:
        state["fail"] = True
        process.push("UDEV  [1.0] remove /devices/x (usb)")
        time.sleep(0.2)
        state["fail"] = False
        unplug_wave(machine)
        process.push("UDEV  [2.0] remove /devices/x (usb)")
        assert not changes.get(timeout=WAIT).new_plan.has("wave-xlr")
    finally:
        monitor.stop()


def test_refresh_probes_now_and_returns_the_change(machine: FakeMachine) -> None:
    h = Harness(machine, udev=False)
    h.monitor._poll = 3600  # no polling during this test
    h.monitor.start()
    try:
        assert h.monitor.refresh() is None
        unplug_wave(machine)
        change = h.monitor.refresh()
        assert change is not None and not change.new_plan.has("wave-xlr")
        assert h.monitor.refresh() is None
    finally:
        h.monitor.stop()


def test_stop_ends_the_threads_and_the_process(harness: Harness) -> None:
    process = harness.wait_process()
    harness.monitor.stop()
    assert process.terminated and harness.monitor.mode == "stopped"
    assert not any(t.is_alive() for t in threading.enumerate() if t.name.startswith("hotplug-"))


def test_the_real_monitor_starts_and_stops_on_this_machine() -> None:
    """With the real `udevadm` when it is installed, else the polling fallback: either way it runs and stops."""
    monitor = Monitor(lambda: Snapshot_empty(), lambda s: resolve(s, CATALOGUE), lambda c: None, poll=3600)
    monitor.start()
    try:
        assert monitor.mode == ("udev" if shutil.which("udevadm") else "polling")
    finally:
        started = time.monotonic()
        monitor.stop()
    assert time.monotonic() - started < 10


def Snapshot_empty():
    from streamstack.hardware.model import Snapshot

    return Snapshot()
