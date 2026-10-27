"""The v4l2loopback contract (#86): the module per kernel, the caps and format Chromium needs, one producer per node."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from streamstack import config, virtualdevices
from streamstack.hardware import contention, contract, kmod

# -- the module across kernel updates ---------------------------------------------------------------------------


def kernel(root: Path, release: str, *, module: bool, headers: bool = True) -> None:
    base = root / release
    (base / "kernel").mkdir(parents=True)
    lines = ["kernel/drivers/media/v4l2-core/videodev.ko.zst:"]
    if module:
        lines.append("ubuntu/dkms/v4l2loopback/v4l2loopback.ko.zst: kernel/drivers/media/v4l2-core/videodev.ko.zst")
    (base / "modules.dep").write_text("\n".join(lines) + "\n")
    if headers:
        (base / "build").mkdir()
        (base / "build" / "Makefile").write_text("")


def facts(tmp_path: Path, running: str, loaded: str | None = "0.15.4", dkms: str | None = ""):
    loaded_file = tmp_path / "version"
    if loaded is not None:
        loaded_file.write_text(loaded + "\n")
    return kmod.gather(root=tmp_path / "modules", running=running, loaded_file=loaded_file, run=lambda argv: dkms)


@pytest.fixture
def modules(tmp_path: Path) -> Path:
    (tmp_path / "modules").mkdir()
    return tmp_path / "modules"


def by_id(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {r["id"]: r for r in rows}


def test_release_order_is_numeric_not_textual() -> None:
    releases = ["7.0.0-100-generic", "7.0.0-38-generic", "6.17.0-9-generic"]
    assert sorted(releases, key=kmod.release_key) == ["6.17.0-9-generic", "7.0.0-38-generic", "7.0.0-100-generic"]


def test_a_running_kernel_with_the_module_is_ok(tmp_path: Path, modules: Path) -> None:
    kernel(modules, "7.0.0-38-generic", module=True)
    rows = by_id(kmod.checks(facts(tmp_path, "7.0.0-38-generic")))
    assert rows["v4l2loopback-kernel"]["state"] == "ok"
    assert rows["v4l2loopback-version"]["state"] == "ok"


def test_a_running_kernel_without_the_module_fails_with_the_fix(tmp_path: Path, modules: Path) -> None:
    kernel(modules, "7.0.0-38-generic", module=False)
    row = by_id(kmod.checks(facts(tmp_path, "7.0.0-38-generic", loaded=None)))["v4l2loopback-kernel"]
    assert row["state"] == "fail" and "no v4l2loopback module" in row["title"]
    assert "dkms autoinstall -k 7.0.0-38-generic" in row["hint"] and "modprobe v4l2loopback" in row["hint"]


def test_missing_headers_change_the_fix(tmp_path: Path, modules: Path) -> None:
    kernel(modules, "7.0.0-38-generic", module=False, headers=False)
    row = by_id(kmod.checks(facts(tmp_path, "7.0.0-38-generic", loaded=None)))["v4l2loopback-kernel"]
    assert "linux-headers-7.0.0-38-generic" in row["hint"]


def test_a_newer_installed_kernel_without_the_module_warns_before_the_reboot(tmp_path: Path, modules: Path) -> None:
    kernel(modules, "7.0.0-38-generic", module=True)
    kernel(modules, "7.0.0-41-generic", module=False)
    rows = by_id(kmod.checks(facts(tmp_path, "7.0.0-38-generic")))
    pending = rows["v4l2loopback-pending"]
    assert pending["state"] == "warn" and "7.0.0-41-generic" in pending["title"]
    assert "dkms autoinstall -k 7.0.0-41-generic" in pending["hint"]


def test_a_newer_kernel_with_the_module_is_fine_and_an_older_one_is_ignored(tmp_path: Path, modules: Path) -> None:
    kernel(modules, "7.0.0-30-generic", module=False)  # old: never booted again
    kernel(modules, "7.0.0-38-generic", module=True)
    kernel(modules, "7.0.0-41-generic", module=True)
    rows = by_id(kmod.checks(facts(tmp_path, "7.0.0-38-generic")))
    assert rows["v4l2loopback-pending"]["state"] == "ok"


def test_the_distribution_version_is_a_note_that_names_the_upstream_fix(tmp_path: Path, modules: Path) -> None:
    kernel(modules, "7.0.0-38-generic", module=True)
    row = by_id(kmod.checks(facts(tmp_path, "7.0.0-38-generic", loaded="0.15.3")))["v4l2loopback-version"]
    assert row["state"] == "ok" and "0.15.4" in row["title"] and "concurrency" in row["title"]


def test_a_version_older_than_the_distribution_has_is_a_warning_with_the_fix(tmp_path: Path, modules: Path) -> None:
    kernel(modules, "7.0.0-38-generic", module=True)
    row = by_id(kmod.checks(facts(tmp_path, "7.0.0-38-generic", loaded="0.15.1")))["v4l2loopback-version"]
    assert row["state"] == "warn" and "update v4l2loopback-dkms" in row["hint"]


def test_a_module_updated_on_disk_but_not_reloaded_is_flagged(tmp_path: Path, modules: Path) -> None:
    kernel(modules, "7.0.0-38-generic", module=True)
    status = "v4l2loopback/0.16.0, 7.0.0-38-generic, x86_64: installed\n"
    row = by_id(kmod.checks(facts(tmp_path, "7.0.0-38-generic", loaded="0.15.4", dkms=status)))["v4l2loopback-reload"]
    assert row["state"] == "warn" and "0.16.0" in row["title"] and "no call is running" in row["hint"]


def test_dkms_status_parses_both_forms() -> None:
    text = (
        "v4l2loopback/0.15.3, 7.0.0-38-generic, x86_64: installed\n"
        "v4l2loopback, 0.15.3, 7.0.0-30-generic, x86_64: built\n"
        "other/1.0, 7.0.0-38-generic, x86_64: installed\n"
    )
    assert [(e.version, e.release, e.state) for e in kmod.parse_dkms_status(text)] == [
        ("0.15.3", "7.0.0-38-generic", "installed"),
        ("0.15.3", "7.0.0-30-generic", "built"),
    ]


def test_an_unreadable_tree_says_nothing_instead_of_failing(tmp_path: Path) -> None:
    rows = kmod.checks(facts(tmp_path, "7.0.0-38-generic", loaded=None))
    assert rows == []


def test_the_kernel_hook_is_shipped_and_valid_shell() -> None:
    hook = Path(__file__).parent.parent / "packaging" / "kernel" / "postinst.d" / "zz-streamstack-v4l2loopback"
    assert os.access(hook, os.X_OK)
    text = hook.read_text()
    assert text.startswith("#!/bin/sh") and "dkms autoinstall" in text and text.rstrip().endswith("exit 0")


# -- the caps and format contract ---------------------------------------------------------------------------------

YUYV_1080 = [("YUYV", 1920, 1080, 30.0)]


def test_the_engines_output_conforms() -> None:
    assert contract.violations(YUYV_1080) == []
    assert contract.violations([("NV12", 1280, 720, 30.0)]) == []


@pytest.mark.parametrize(
    ("modes", "needle"),
    [
        ([("MJPG", 1920, 1080, 30.0)], "offers MJPG"),
        ([("YUYV", 1920, 1080, 30.0), ("YUYV", 1280, 720, 30.0)], "more than one size"),
        ([("YUYV", 1921, 1080, 30.0)], "odd side"),
        ([("YUYV", 320, 180, 30.0)], "smaller than 640x360"),
        ([("YUYV", 7680, 4320, 30.0)], "larger than 3840x2160"),
        ([("YUYV", 1920, 1080, 15.0)], "15 fps is outside"),
        ([("YUYV", 1920, 1080, 120.0)], "120 fps is outside"),
    ],
)
def test_each_way_to_break_the_contract_is_named(modes: list, needle: str) -> None:
    assert any(needle in v for v in contract.violations(modes))


def test_the_contract_row_fails_without_exclusive_caps_and_passes_when_it_conforms() -> None:
    bad = contract.check("/dev/video43", "StreamStack Camera", False, YUYV_1080)
    assert bad["state"] == "fail" and "exclusive_caps is off" in bad["title"] and bad["id"] == "v4l2loopback-contract"
    ok = contract.check("/dev/video43", "StreamStack Camera", True, YUYV_1080)
    assert ok["state"] == "ok" and "1920x1080 YUYV 30 fps" in ok["detail"]


def test_the_contract_is_only_judged_on_a_fed_readable_node() -> None:
    report = virtualdevices.CameraReport("/dev/video43", "StreamStack Camera", name="StreamStack Camera", access=True)
    assert contract.report_check(report) == []  # nothing is feeding it: no format to judge
    report.producer, report.modes, report.exclusive_caps = True, YUYV_1080, True
    assert contract.report_check(report)[0]["state"] == "ok"


# -- one producer per node ----------------------------------------------------------------------------------------

PROGRAM, CAMERA, LINK = "/dev/video43", "/dev/video42", "/dev/video0"


class FakeProc:
    def __init__(self, root: Path) -> None:
        self.root = root

    def process(self, pid: int, comm: str, argv: list[str] | None = None, ppid: int = 1, fds: tuple[str, ...] = ()):
        base = self.root / str(pid)
        (base / "fd").mkdir(parents=True)
        (base / "comm").write_text(comm + "\n")
        (base / "cmdline").write_bytes(b"\0".join(a.encode() for a in (argv or [comm])) + b"\0")
        (base / "stat").write_text(f"{pid} ({comm}) S {ppid} 1 1 0 -1\n")
        for number, target in enumerate(fds, start=3):
            os.symlink(target, base / "fd" / str(number))


@pytest.fixture
def proc(tmp_path: Path) -> FakeProc:
    root = tmp_path / "proc"
    root.mkdir()
    return FakeProc(root)


def rows_for(proc: FakeProc, capture: str | None = LINK) -> dict[str, dict[str, str]]:
    return by_id(contention.guard_checks({**config.DEFAULTS}, capture, proc=proc.root))


def test_only_the_engine_and_browsers_on_the_program_node_is_fine(proc: FakeProc) -> None:
    proc.process(10, "python3", ["python3", "-m", "streamstack", "console"], fds=(PROGRAM,))
    proc.process(11, "ffmpeg", ["ffmpeg", "-f", "v4l2", PROGRAM], ppid=10, fds=(PROGRAM,))  # the engine child
    proc.process(12, "msedge", fds=(PROGRAM,))
    assert rows_for(proc)["video-holders-program"]["state"] == "ok"


def test_a_leftover_obs_virtual_camera_is_a_competing_producer_and_is_named(proc: FakeProc) -> None:
    proc.process(10, "python3", ["python3", "-m", "streamstack", "console"], fds=(PROGRAM,))
    proc.process(200, "obs", ["obs", "--startvirtualcam"], fds=(PROGRAM,))
    row = rows_for(proc)["video-holders-program"]
    assert row["state"] == "fail" and "obs (pid 200)" in row["title"]
    assert "two producers" in row["title"] and "Stop Virtual Camera" in row["hint"]


def test_an_ffmpeg_that_writes_the_program_node_is_a_producer_but_one_that_reads_it_is_not(proc: FakeProc) -> None:
    proc.process(300, "ffmpeg", ["ffmpeg", "-f", "lavfi", "-i", "testsrc", "-f", "v4l2", PROGRAM], fds=(PROGRAM,))
    assert rows_for(proc)["video-holders-program"]["state"] == "fail"


def test_a_reader_ffmpeg_is_not_a_competing_producer(proc: FakeProc) -> None:
    proc.process(301, "ffmpeg", ["ffmpeg", "-f", "v4l2", "-i", PROGRAM, "-f", "null", "-"], fds=(PROGRAM,))
    assert rows_for(proc)["video-holders-program"]["state"] == "ok"


def test_two_feeders_on_the_camera_node_are_reported(proc: FakeProc) -> None:
    proc.process(400, "ffmpeg", ["ffmpeg", "-i", "pipe:0", "-f", "v4l2", CAMERA], fds=(CAMERA,))
    proc.process(401, "ffmpeg", ["ffmpeg", "-i", "x.mp4", "-f", "v4l2", CAMERA], fds=(CAMERA,))
    row = rows_for(proc)["video-holders-camera"]
    assert row["state"] == "fail" and "2 programs feed /dev/video42" in row["title"]


def test_one_feeder_on_the_camera_node_says_nothing(proc: FakeProc) -> None:
    proc.process(400, "ffmpeg", ["ffmpeg", "-i", "pipe:0", "-f", "v4l2", CAMERA], fds=(CAMERA,))
    assert "video-holders-camera" not in rows_for(proc)


def test_obs_holding_the_cam_link_is_the_blocked_camera_report(proc: FakeProc) -> None:
    proc.process(200, "obs", fds=(LINK,))
    row = rows_for(proc)["video-holders-capture"]
    assert (
        row["state"] == "warn" and "obs (pid 200) holds the Cam Link" in row["title"] and "one reader" in row["detail"]
    )
    assert "Stop Virtual Camera" in row["hint"] or "StreamStack Camera" in row["hint"]


def test_streamstack_holding_the_cam_link_is_not_a_rival(proc: FakeProc) -> None:
    proc.process(10, "python3", ["python3", "-m", "streamstack", "console"])
    proc.process(11, "ffmpeg", ["ffmpeg", "-f", "v4l2", "-i", LINK], ppid=10, fds=(LINK,))
    assert "video-holders-capture" not in rows_for(proc)


def test_holders_never_open_a_node(proc: FakeProc, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    real = os.open

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    proc.process(10, "firefox", fds=(PROGRAM,))
    rows_for(proc)
    assert not [p for p in opened if p.startswith("/dev/video")]
