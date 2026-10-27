"""The virtual camera and microphone as apps see them: setup checks, access, and who has them open."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import replace
from pathlib import Path

import pytest

from streamstack import camerause, config, virtualdevices
from streamstack.virtualdevices import CameraReport, MicReport

ROOT = Path(__file__).resolve().parents[1]
CFG = {
    "STREAMSTACK_PROGRAM_DEVICE": "/dev/video43",
    "STREAMSTACK_PROGRAM_LABEL": "StreamStack Camera",
}


def params(tmp_path: Path, video_nr: str, caps: str) -> Path:
    (tmp_path / "video_nr").write_text(video_nr + "\n")
    (tmp_path / "exclusive_caps").write_text(caps + "\n")
    return tmp_path


def good(**changes: object) -> CameraReport:
    base = CameraReport(
        device="/dev/video43",
        expected_label="StreamStack Camera",
        name="StreamStack Camera",
        exclusive_caps=True,
        access=True,
        producer=True,
        mode="1920x1080 YUYV @ 30 fps",
        modes=[("YUYV", 1920, 1080, 30.0)],
    )
    return replace(base, **changes)


def states(checks: list[dict[str, str]]) -> dict[str, str]:
    return {c["id"]: c["state"] for c in checks}


# -- module configuration ---------------------------------------------------------------------------------------------


def test_exclusive_caps_is_read_at_the_position_of_the_node_number(tmp_path: Path) -> None:
    root = params(tmp_path, "42,43,-1,-1", "Y,N,N,N")
    assert virtualdevices.exclusive_caps("/dev/video42", root) is True
    assert virtualdevices.exclusive_caps("/dev/video43", root) is False


def test_exclusive_caps_is_unknown_without_the_module_or_for_a_foreign_node(tmp_path: Path) -> None:
    assert virtualdevices.exclusive_caps("/dev/video43", tmp_path / "absent") is None
    assert virtualdevices.exclusive_caps("/dev/video0", params(tmp_path, "42,43", "Y,Y")) is None


def test_the_shipped_modprobe_snippet_matches_the_settings_apps_depend_on() -> None:
    text = (ROOT / "packaging/modprobe.d/streamstack-v4l2loopback.conf").read_text()
    (line,) = [ln for ln in text.splitlines() if ln.startswith("options v4l2loopback")]
    opts = dict(part.split("=", 1) for part in shlex.split(line)[2:])
    numbers = opts["video_nr"].split(",")
    labels = opts["card_label"].split(",")
    caps = opts["exclusive_caps"].split(",")
    assert int(opts["devices"]) == len(numbers) == len(labels) == len(caps)
    program = config.DEFAULTS["STREAMSTACK_PROGRAM_DEVICE"]
    index = numbers.index(program.removeprefix("/dev/video"))
    assert labels[index] == config.DEFAULTS["STREAMSTACK_PROGRAM_LABEL"]
    assert set(caps) == {"1"}, "every StreamStack node needs exclusive_caps, or Chrome and Zoom skip it"


def test_the_module_is_loaded_at_boot_and_the_udev_rule_grants_the_video_group() -> None:
    load = (ROOT / "packaging/modules-load.d/streamstack-v4l2loopback.conf").read_text()
    assert "v4l2loopback" in [ln.strip() for ln in load.splitlines()]
    rule = (ROOT / "packaging/udev/61-streamstack-video.rules").read_text()
    assert re.search(r'ATTR\{name\}=="StreamStack Camera".*GROUP="video".*MODE="0660"', rule)


# -- camera checks ----------------------------------------------------------------------------------------------------


def test_a_well_formed_camera_passes() -> None:
    result = virtualdevices.camera_checks(good())
    assert states(result) == {"virtual-camera-format": "ok"}
    assert "1920x1080 YUYV @ 30 fps" in result[0]["title"]


def test_a_wrong_name_is_a_failure_with_the_fix() -> None:
    (item,) = virtualdevices.camera_checks(good(name="Dummy video device", producer=None, modes=[], mode=""))
    assert item["state"] == "fail" and "card_label" in item["detail"]


def test_exclusive_caps_off_is_a_failure() -> None:
    assert states(virtualdevices.camera_checks(good(exclusive_caps=False)))["virtual-camera-exclusive-caps"] == "fail"


def test_no_permission_is_reported_instead_of_guessing_about_the_picture() -> None:
    result = virtualdevices.camera_checks(good(access=False, producer=None, modes=[], mode=""))
    assert states(result) == {"virtual-camera-access": "fail"}
    assert "video group" in result[0]["hint"]


@pytest.mark.parametrize(
    ("modes", "word"),
    [
        ([("H264", 1920, 1080, 30.0)], "H264"),
        ([("YUYV", 640, 480, 30.0)], "480p"),
        ([("YUYV", 1920, 1080, 15.0)], "15 fps"),
    ],
)
def test_formats_calls_cannot_use_are_warned_about(modes: list, word: str) -> None:
    (item,) = virtualdevices.camera_checks(good(modes=modes))
    assert item["state"] == "warn" and word in item["title"]


def test_a_missing_node_adds_nothing() -> None:
    assert virtualdevices.camera_checks(CameraReport("/dev/video43", "StreamStack Camera")) == []


# -- microphone -------------------------------------------------------------------------------------------------------

SOURCE = {
    "name": "streamstack_mic",
    "description": "StreamStack Mic",
    "state": "SUSPENDED",
    "sample_specification": "float32le 1ch 48000Hz",
    "monitor_of_sink": None,
}


def test_the_mic_report_reads_what_pactl_shows() -> None:
    report = virtualdevices.mic_report([SOURCE], default="streamstack_mic")
    assert report == MicReport(True, "StreamStack Mic", False, True, "SUSPENDED", 1, 48000)
    assert states(virtualdevices.mic_checks(report)) == {"virtual-mic-listed": "ok"}


def test_the_mic_is_absent_when_the_chain_is_off() -> None:
    assert virtualdevices.mic_report([], default="") == MicReport()
    assert virtualdevices.mic_checks(MicReport()) == []


def test_a_monitor_source_or_a_wrong_description_fails() -> None:
    monitor = virtualdevices.mic_report([{**SOURCE, "monitor_of_sink": 55}], default="")
    assert states(virtualdevices.mic_checks(monitor)) == {"virtual-mic-monitor": "fail"}
    renamed = virtualdevices.mic_report([{**SOURCE, "description": "Filter Chain"}], default="")
    assert states(virtualdevices.mic_checks(renamed)) == {"virtual-mic-label": "fail"}


# -- who has them open ------------------------------------------------------------------------------------------------


def node(nid: int, **props: str) -> dict:
    return {"id": nid, "type": "PipeWire:Interface:Node", "info": {"props": props}}


def link(out: int, into: int) -> dict:
    return {
        "id": 900 + out * 10 + into,
        "type": "PipeWire:Interface:Link",
        "info": {"output-node-id": out, "input-node-id": into},
    }


GRAPH = [
    node(313, **{"node.name": "streamstack_mic", "media.class": "Audio/Source"}),
    node(70, **{"application.process.binary": "teams-for-linux", "application.process.id": "4242", "node.name": "x"}),
    node(71, **{"application.process.binary": "streamstack", "application.process.id": "99", "node.name": "meter"}),
    node(72, **{"node.name": "alsa_input.other"}),
    link(313, 70),
    link(313, 71),
    link(72, 70),
]


def test_microphone_readers_come_from_links_and_skip_streamstack_itself() -> None:
    seen = virtualdevices.pipewire_readers(GRAPH, "mic", lambda p: p.get("node.name") == "streamstack_mic")
    assert [(s.kind, s.app, s.pid, s.via) for s in seen] == [("mic", "Teams", 4242, "pipewire")]


def test_seen_by_apps_joins_v4l2_holders_and_pipewire_readers(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    (root / "500" / "fd").mkdir(parents=True)
    (root / "500" / "comm").write_text("zoom\n")
    (root / "500" / "cmdline").write_bytes(b"zoom\0")
    (root / "500" / "stat").write_text("500 (zoom) S 1 1 1 0 -1\n")
    os.symlink("/dev/video43", root / "500" / "fd" / "7")
    seen = virtualdevices.seen_by_apps(CFG, scanner=camerause.ProcScanner(root, own_pid=1), objects=lambda: GRAPH)
    assert {(s.kind, s.app) for s in seen} == {("camera", "Zoom"), ("mic", "Teams")}
    item = virtualdevices.seen_check(seen)
    assert item["detail"] == "camera: Zoom; microphone: Teams"


def test_nobody_using_the_devices_is_a_good_answer(tmp_path: Path) -> None:
    (tmp_path / "proc").mkdir()
    seen = virtualdevices.seen_by_apps(CFG, scanner=camerause.ProcScanner(tmp_path / "proc", own_pid=1), objects=list)
    assert seen == []
    assert virtualdevices.seen_check(seen)["state"] == "ok"


def test_the_console_snapshot_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object, **__: object) -> None:
        raise OSError("sysfs vanished")

    monkeypatch.setattr(virtualdevices, "camera_report", boom)
    assert virtualdevices.snapshot(CFG) == {"error": "sysfs vanished", "seen": []}
