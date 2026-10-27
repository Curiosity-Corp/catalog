"""The probe against a fake machine: each source alone, failures per source, and hot-plug differences."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from hardware_support import (
    ARC_VA,
    CAPS_CAPTURE,
    MACHINE_ID,
    FakeMachine,
    studio_wave,
)

from streamstack.hardware import parsers, probe
from streamstack.hardware.hotplug import changed_sources


@pytest.fixture
def machine(tmp_path: Path) -> FakeMachine:
    return FakeMachine(tmp_path)


@pytest.fixture
def studio(machine: FakeMachine) -> FakeMachine:
    return studio_wave(machine)


def states(snap) -> dict[str, str]:
    return {s.source: s.state for s in snap.sources}


def test_a_full_studio_reads_every_source(studio: FakeMachine) -> None:
    snap = studio.snapshot()
    assert {k: v for k, v in states(snap).items() if v != "ok"} == {"vulkan": "absent"}
    assert {"0fd9:007d", "0fd9:0067", "04a9:319b", "046d:c901"} <= snap.usb_ids()
    node = snap.render_node_for("8086:56a5")
    assert node is not None and node.path == "/dev/dri/renderD128" and node.access == "rw"
    assert node.va is not None and node.va.encode_codecs() == ("h264", "hevc", "av1")
    card = snap.alsa[0]
    assert (card.id, card.usb_id, card.rates("playback"), card.formats("capture")) == (
        "XLR",
        "0fd9:007d",
        (48000, 96000),
        ("S24_3LE",),
    )
    assert snap.pipewire.cards[0].usb_id == "0fd9:007d"
    capture = next(n for n in snap.v4l2 if n.path == "/dev/video0")
    assert capture.kind == "capture" and capture.usb_id == "0fd9:0067" and capture.formats[0].fps == 60.0
    assert [d.label for d in snap.loopback.devices] == ["Canon EOS 50D", "StreamStack Camera"]
    assert snap.machine == parsers.machine_hash(MACHINE_ID)
    assert "av1_vaapi" in snap.ffmpeg.encoders
    assert snap.taken_at == 1000.0


def test_the_machine_id_is_hashed_never_kept(studio: FakeMachine) -> None:
    snap = studio.snapshot()
    assert MACHINE_ID not in repr(snap)
    assert snap.machine and len(snap.machine) == 32


def test_the_host_name_is_never_read(studio: FakeMachine, monkeypatch: pytest.MonkeyPatch) -> None:
    import platform
    import socket

    def refuse(*_a: object, **_k: object) -> str:
        raise AssertionError("the probe read the host name")

    monkeypatch.setattr(socket, "gethostname", refuse)
    monkeypatch.setattr(platform, "node", refuse)
    monkeypatch.setenv("HOSTNAME", "not-read")
    assert studio.snapshot().sources


def test_render_node_numbers_are_not_identity(machine: FakeMachine) -> None:
    machine.add_gpu(128, "0000:00:02.0", "8086:a780", "Intel Corporation Raptor Lake-S GT1 [UHD Graphics 770]", ())
    machine.add_gpu(129, "0000:03:00.0", "8086:56a5", "Intel Corporation DG2 [Arc A380]", ARC_VA)
    snap = machine.snapshot()
    assert snap.render_node_for("8086:56a5").path == "/dev/dri/renderD129"
    assert snap.render_node_for("8086:a780").path == "/dev/dri/renderD128"


def test_a_missing_gpu_is_an_empty_drm_source_not_a_failure(machine: FakeMachine) -> None:
    (machine.sys / "class" / "drm").mkdir(parents=True)
    snap = machine.snapshot()
    assert snap.render_nodes == ()
    assert states(snap)["drm"] == "ok"
    assert states(snap)["vaapi"] == "ok"


def test_a_denied_render_node_is_marked_and_vainfo_is_not_run_on_it(studio: FakeMachine) -> None:
    studio.denied.add("renderD128")
    snap = studio.snapshot()
    node = snap.render_node_for("8086:56a5")
    assert node.access == "denied"
    assert not any(c[0] == "vainfo" for c in studio.calls)
    assert node.va is not None and node.va.state == "denied"


def test_a_denied_video_node_is_listed_but_nothing_is_read_from_it(studio: FakeMachine) -> None:
    studio.denied.add("video0")
    snap = studio.snapshot()
    capture = next(n for n in snap.v4l2 if n.path == "/dev/video0")
    assert capture.access == "denied"
    assert capture.usb_id == "0fd9:0067"  # sysfs stays readable
    assert capture.kind == "unknown" and capture.formats == ()
    assert states(snap)["v4l2"] == "ok"


def test_absent_tools_are_absent_not_denied(machine: FakeMachine) -> None:
    for tool in ("lspci", "vainfo", "pactl", "v4l2-ctl", "ffmpeg"):
        machine.tools.pop(tool)
    snap = machine.snapshot()
    got = states(snap)
    assert got["pci"] == "absent" and got["pipewire"] == "absent" and got["ffmpeg"] == "absent"
    assert got["usb"] == "absent" and got["alsa"] == "absent" and got["machine"] == "ok"


def test_a_tool_that_times_out_costs_only_its_source(studio: FakeMachine) -> None:
    studio.tools["pactl"] = lambda argv: None
    snap = studio.snapshot()
    got = states(snap)
    assert got["pipewire"] == "timeout"
    assert got["usb"] == got["drm"] == got["alsa"] == "ok"
    assert snap.pipewire.cards == ()


def test_permission_denied_from_a_tool_is_denied(studio: FakeMachine) -> None:
    studio.tools["lspci"] = lambda argv: subprocess.CompletedProcess(argv, 1, "", "lspci: Permission denied")
    assert states(studio.snapshot())["pci"] == "denied"


def test_garbage_from_a_tool_is_an_error_on_that_source(studio: FakeMachine) -> None:
    studio.tools["lspci"] = "this is not lspci output\nat all\n"
    snap = studio.snapshot()
    assert states(snap)["pci"] == "error"
    assert states(snap)["usb"] == "ok"
    assert "lspci" in snap.status("pci").error


def test_a_broken_proc_file_is_an_error_on_alsa_only(studio: FakeMachine) -> None:
    stream = studio.proc / "asound" / "card3" / "stream0"
    stream.unlink()
    stream.mkdir()  # reading a directory fails with EISDIR
    snap = studio.snapshot()
    assert states(snap)["alsa"] == "error"
    assert states(snap)["usb"] == "ok"


def test_missing_optional_pipewire_helpers_are_notes(studio: FakeMachine) -> None:
    studio.tools.pop("pactl-json")  # no card profile json
    snap = studio.snapshot()
    status = snap.status("pipewire")
    assert status.ok and any("card profiles" in n for n in status.notes)
    assert snap.pipewire.cards[0].usb_id is None  # unknown without the json


def test_unplugging_the_wave_xlr_changes_only_usb_alsa_and_pipewire(studio: FakeMachine) -> None:
    before = studio.snapshot()
    studio.remove_usb("0fd9:007d")
    studio.remove_alsa(3)
    studio.set_pipewire([], [], [])
    after = studio.snapshot()
    assert "0fd9:007d" not in after.usb_ids()
    assert after != before
    assert set(changed_sources(before, after)) == {"usb", "alsa", "pipewire"}


def test_an_unchanged_machine_gives_an_equal_snapshot(studio: FakeMachine) -> None:
    assert studio.snapshot() == studio.snapshot()


def test_a_v4l2loopback_node_is_reported_as_loopback(studio: FakeMachine) -> None:
    snap = studio.snapshot()
    assert [n.loopback for n in snap.v4l2 if n.path in ("/dev/video42", "/dev/video43")] == [True, True]
    assert snap.loopback.loaded and snap.loopback.version == "0.15.3"
    assert snap.loopback.option("video_nr") == "42,43"


def test_a_dmi_uuid_stands_in_for_a_missing_machine_id(machine: FakeMachine) -> None:
    (machine.root / "etc" / "machine-id").unlink()
    uuid = machine.sys / "class" / "dmi" / "id" / "product_uuid"
    uuid.parent.mkdir(parents=True)
    uuid.write_text("0123456789ABCDEF0123456789ABCDEF\n")
    assert machine.snapshot().machine == parsers.machine_hash(MACHINE_ID)


def test_no_machine_identity_is_absent(machine: FakeMachine) -> None:
    (machine.root / "etc" / "machine-id").unlink()
    snap = machine.snapshot()
    assert snap.machine is None and states(snap)["machine"] == "absent"


def test_the_probe_never_raises_on_an_empty_machine(machine: FakeMachine) -> None:
    machine.tools.clear()
    snap = probe.snapshot(machine.host())
    assert len(snap.sources) == len(probe.SOURCES)


def test_a_non_capture_node_is_not_a_capture_card(studio: FakeMachine) -> None:
    metadata = next(n for n in studio.snapshot().v4l2 if n.path == "/dev/video1")
    assert metadata.kind == "metadata"
    assert CAPS_CAPTURE & 1
