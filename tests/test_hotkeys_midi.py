"""The MIDI source: raw ports read as byte streams (FIFOs stand in for /dev/snd/midiC*D*), hot-plug and names."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from streamstack import hotkeys_midi
from streamstack.hotkeys import MidiEvent
from streamstack.hotkeys_midi import MidiSource


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, MidiEvent]] = []
        self.changes: list[list[dict]] = []
        self.lock = threading.Lock()

    def event(self, port: str, event: MidiEvent) -> None:
        with self.lock:
            self.events.append((port, event))

    def change(self, devices: list[dict]) -> None:
        with self.lock:
            self.changes.append(devices)

    def wait(self, predicate, seconds: float = 4.0) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            with self.lock:
                if predicate(self):
                    return True
            time.sleep(0.02)
        return False


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    dev, proc = tmp_path / "snd", tmp_path / "asound"
    dev.mkdir()
    (proc / "card1").mkdir(parents=True)
    (proc / "card1" / "id").write_text("nanoKONTROL2\n")
    return dev, proc


def plug(dev: Path, name: str = "midiC1D0") -> Path:
    path = dev / name
    os.mkfifo(path)
    return path


def writer(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_NONBLOCK)


def test_a_port_is_named_after_its_card_and_read(tree: tuple[Path, Path]) -> None:
    dev, proc = tree
    port = plug(dev)
    sink = Sink()
    source = MidiSource(sink.event, sink.change, dev, proc, scan_seconds=0.05)
    assert source.start() is None
    try:
        assert sink.wait(lambda s: source.devices() == [{"id": "midiC1D0", "name": "nanoKONTROL2", "status": "ok"}])
        fd = writer(port)
        os.write(fd, bytes([0x90, 60, 100, 0xB0, 7, 99]))
        assert sink.wait(lambda s: len(s.events) == 2)
        assert sink.events == [
            ("midiC1D0", MidiEvent("note", 1, 60, 100)),
            ("midiC1D0", MidiEvent("cc", 1, 7, 99)),
        ]
        os.close(fd)
    finally:
        source.stop()


def test_a_second_device_of_a_card_gets_a_suffix_and_an_unknown_card_a_fallback(tree: tuple[Path, Path]) -> None:
    dev, proc = tree
    plug(dev, "midiC1D1")
    plug(dev, "midiC7D0")
    source = MidiSource(Sink().event, None, dev, proc, scan_seconds=0.05)
    assert source.start() is None
    try:
        sink = Sink()
        assert sink.wait(lambda s: len(source.devices()) == 2)
        assert {d["name"] for d in source.devices()} == {"nanoKONTROL2.1", "MIDI card 7"}
    finally:
        source.stop()


def test_hot_plug_adds_and_removes_ports(tree: tuple[Path, Path]) -> None:
    dev, proc = tree
    sink = Sink()
    source = MidiSource(sink.event, sink.change, dev, proc, scan_seconds=0.05)
    assert source.start() is None
    try:
        time.sleep(0.15)
        assert source.devices() == []
        port = plug(dev)
        assert sink.wait(lambda s: len(source.devices()) == 1)
        fd = writer(port)
        os.write(fd, bytes([0xC0, 9]))
        assert sink.wait(lambda s: len(s.events) == 1)
        os.close(fd)
        port.unlink()
        assert sink.wait(lambda s: source.devices() == [])
        assert sink.changes[-1] == []
    finally:
        source.stop()


def test_messages_split_across_reads_are_joined(tree: tuple[Path, Path]) -> None:
    dev, proc = tree
    port = plug(dev)
    sink = Sink()
    source = MidiSource(sink.event, None, dev, proc, scan_seconds=0.05)
    assert source.start() is None
    try:
        assert sink.wait(lambda s: len(source.devices()) == 1)
        fd = writer(port)
        os.write(fd, bytes([0xB0, 7]))
        time.sleep(0.15)
        os.write(fd, bytes([64]))
        assert sink.wait(lambda s: len(s.events) == 1)
        assert sink.events[0][1] == MidiEvent("cc", 1, 7, 64)
        os.close(fd)
    finally:
        source.stop()


def test_a_port_whose_writer_left_is_reopened(tree: tuple[Path, Path]) -> None:
    dev, proc = tree
    port = plug(dev)
    sink = Sink()
    source = MidiSource(sink.event, None, dev, proc, scan_seconds=0.05)
    assert source.start() is None
    try:
        assert sink.wait(lambda s: len(source.devices()) == 1)
        for note in (60, 61):
            fd = writer(port)
            os.write(fd, bytes([0x90, note, 90]))
            assert sink.wait(lambda s, n=note: any(e.number == n for _, e in s.events))
            os.close(fd)  # end of stream: the source reopens the port
            time.sleep(0.2)
    finally:
        source.stop()


def test_a_port_that_cannot_be_opened_is_listed_with_its_status(tree: tuple[Path, Path]) -> None:
    dev, proc = tree
    path = dev / "midiC1D0"
    path.write_bytes(b"")
    path.chmod(0)
    if os.access(path, os.R_OK):
        pytest.skip("running as a user that can read anything")
    source = MidiSource(Sink().event, None, dev, proc, scan_seconds=0.05)
    assert source.start() is None
    try:
        assert Sink().wait(lambda s: source.devices() and source.devices()[0]["status"] == "denied")
    finally:
        source.stop()


def test_a_failing_handler_does_not_stop_the_reader(tree: tuple[Path, Path]) -> None:
    dev, proc = tree
    port = plug(dev)
    seen: list[int] = []

    def handler(_port: str, event: MidiEvent) -> None:
        seen.append(event.number)
        raise RuntimeError("boom")

    source = MidiSource(handler, None, dev, proc, scan_seconds=0.05)
    assert source.start() is None
    try:
        assert Sink().wait(lambda s: len(source.devices()) == 1)
        fd = writer(port)
        os.write(fd, bytes([0x90, 1, 90, 0x90, 2, 90]))
        assert Sink().wait(lambda s: seen == [1, 2])
        assert source.reason and "boom" in source.reason
        os.close(fd)
    finally:
        source.stop()


def test_without_sound_devices_the_reason_is_given(tmp_path: Path) -> None:
    source = MidiSource(Sink().event, None, tmp_path / "absent", tmp_path)
    assert "no sound devices" in (source.start() or "")
    assert hotkeys_midi.available(tmp_path / "absent")
    assert hotkeys_midi.available(tmp_path) is None
    source.stop()


def test_stop_is_idempotent_and_closes_ports(tree: tuple[Path, Path]) -> None:
    dev, proc = tree
    plug(dev)
    source = MidiSource(Sink().event, None, dev, proc, scan_seconds=0.05)
    assert source.start() is None
    assert Sink().wait(lambda s: len(source.devices()) == 1)
    source.stop()
    source.stop()
    assert source._open == {}
