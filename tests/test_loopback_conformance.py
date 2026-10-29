from __future__ import annotations

import subprocess

from streamstack import system
from streamstack.hardware import conformance

INFO_FED = (
    "Driver Info:\n\tDriver name : v4l2 loopback\nDevice Caps      : 0x85208000\n\t\tVideo Capture\n\t\tVideo Output\n"
)
INFO_UNFED = "Driver Info:\n\tDriver name : v4l2 loopback\nDevice Caps      : 0x85208000\n\t\tVideo Output\n"
GOOD = [("YUYV", 1280, 720, 30.0)]


class Node:
    """A fake loopback: Video Capture is advertised only while a writer is attached."""

    def __init__(self) -> None:
        self.writers = 0

    def writer(self, device):
        self.writers += 1
        return Proc(self)

    def listed(self, device):
        return self.writers > 0


class Proc:
    def __init__(self, node: Node) -> None:
        self.node = node
        self.stderr = None

    def poll(self):
        return None


def run(node: Node, *, modes=GOOD, frames=True, monkeypatch=None):
    return conformance.run_conformance(
        "/dev/video50",
        writer=node.writer,
        listed=node.listed,
        modes=lambda d: modes,
        frames=lambda d: frames,
        wait=lambda predicate, seconds, sleep=None: bool(predicate()),
    )


def stop_writers(monkeypatch, node: Node) -> None:
    monkeypatch.setattr(conformance, "_stop", lambda proc: setattr(node, "writers", node.writers - 1))


def test_a_conforming_node_passes_every_step(monkeypatch) -> None:
    node = Node()
    stop_writers(monkeypatch, node)
    steps = run(node)
    assert [s.id for s in steps] == ["unfed", "fed", "contract", "frames", "unfed again", "refed"]
    assert conformance.passed(steps), steps
    assert node.writers == 0  # every writer was stopped


def test_a_node_that_offers_the_wrong_format_fails_the_contract_step(monkeypatch) -> None:
    node = Node()
    stop_writers(monkeypatch, node)
    steps = {s.id: s for s in run(node, modes=[("MJPG", 1280, 720, 30.0)])}
    assert not steps["contract"].ok and "MJPG" in steps["contract"].detail


def test_a_node_that_never_lists_with_a_writer_fails_and_skips_the_later_checks(monkeypatch) -> None:
    node = Node()
    stop_writers(monkeypatch, node)
    node.listed = lambda device: False  # exclusive_caps off, or the module is wrong
    steps = run(node)
    assert not conformance.passed(steps)
    assert "contract" not in [s.id for s in steps]


def test_capture_listed_reads_the_device_caps_block() -> None:
    def info(text):
        return lambda argv, timeout=10: subprocess.CompletedProcess(argv, 0, text, "")

    assert conformance.capture_listed("/dev/video50", info(INFO_FED)) is True
    assert conformance.capture_listed("/dev/video50", info(INFO_UNFED)) is False
    assert conformance.capture_listed("/dev/video50", lambda argv, timeout=10: None) is None


def test_the_program_node_and_real_devices_are_refused(monkeypatch, tmp_path) -> None:
    assert "Console feeds" in conformance.refuse("/dev/video43", {})
    assert "does not exist" in conformance.refuse(str(tmp_path / "nope"), {})
    node = tmp_path / "video7"
    node.touch()
    monkeypatch.setattr(
        system, "run", lambda argv, timeout=10: subprocess.CompletedProcess(argv, 0, "Driver name : uvcvideo", "")
    )
    assert "not a v4l2loopback" in conformance.refuse(str(node), {})
    monkeypatch.setattr(system, "run", lambda argv, timeout=10: subprocess.CompletedProcess(argv, 0, INFO_UNFED, ""))
    monkeypatch.setattr(conformance.contention, "holders", lambda devices: [])
    assert conformance.refuse(str(node), {}) is None
