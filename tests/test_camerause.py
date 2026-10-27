"""Camera-use detection: a fake /proc tree, StreamStack's own handles, helpers, and the watcher's settings."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from streamstack import camerause
from streamstack.camerause import CameraWatcher, Consumer, ProcScanner, Usage

PROGRAM, RAW = "/dev/video43", "/dev/video10"


class FakeProc:
    """/proc in a temp directory: processes with a name, an argv, a parent and open file descriptors."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def process(
        self, pid: int, comm: str, *, argv: list[str] | None = None, ppid: int = 1, fds: tuple[str, ...] = ()
    ) -> None:
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


def scan(proc: FakeProc, *devices: str, ignore: tuple[str, ...] = (), own: int = 999_999) -> camerause.Scan:
    return ProcScanner(proc.root, own_pid=own).scan(set(devices or (PROGRAM, RAW)), ignore)


def test_an_app_holding_the_program_camera_is_a_consumer(proc: FakeProc) -> None:
    proc.process(100, "firefox", fds=("/dev/null", PROGRAM))
    found = scan(proc)
    assert [(c.pid, c.comm, c.device) for c in found.consumers] == [(100, "firefox", PROGRAM)]
    assert found.consumers[0].app == "Firefox in a browser tab"


def test_the_raw_camera_counts_too(proc: FakeProc) -> None:
    proc.process(101, "zoom", fds=(RAW,))
    assert [c.app for c in scan(proc).consumers] == ["Zoom"]


def test_processes_without_the_camera_are_not_listed(proc: FakeProc) -> None:
    proc.process(102, "bash", fds=("/dev/pts/0", "/dev/video0"))
    assert scan(proc).consumers == []


def test_streamstack_processes_are_plumbing(proc: FakeProc) -> None:
    proc.process(104, "python3", argv=["/usr/bin/python3", "-m", "streamstack", "console"], fds=(RAW,))
    proc.process(105, "python3", argv=["/usr/bin/python3", "-I", "/usr/bin/streamstack-console"], fds=(RAW,))
    proc.process(106, "streamstack", argv=["streamstack", "canon-bridge"], fds=(PROGRAM,))
    assert scan(proc).consumers == []


def test_our_own_pid_is_never_a_consumer(proc: FakeProc) -> None:
    proc.process(107, "python3", argv=["python3", "x.py"], fds=(RAW,))
    assert scan(proc, own=107).consumers == []


def test_a_media_helper_started_by_streamstack_is_plumbing(proc: FakeProc) -> None:
    proc.process(200, "streamstack", argv=["streamstack", "console"])
    proc.process(201, "ffmpeg", ppid=200, fds=(RAW,))
    proc.process(300, "streamstack", argv=["streamstack", "canon-bridge"])
    proc.process(301, "sh", ppid=300)
    proc.process(302, "ffmpeg", ppid=301, fds=(PROGRAM,))
    assert scan(proc).consumers == []


def test_a_media_helper_from_a_terminal_is_a_consumer(proc: FakeProc) -> None:
    proc.process(400, "bash")
    proc.process(401, "ffmpeg", ppid=400, fds=(RAW,))
    assert [c.comm for c in scan(proc).consumers] == ["ffmpeg"]


def test_ignored_apps_do_not_count_by_process_or_friendly_name(proc: FakeProc) -> None:
    proc.process(500, "slack", fds=(RAW,))
    proc.process(501, "ms-teams", fds=(RAW,))
    assert [c.comm for c in scan(proc, ignore=("slack",)).consumers] == ["ms-teams"]
    assert [c.comm for c in scan(proc, ignore=("Teams",)).consumers] == ["slack"]


def test_vanished_and_unreadable_processes_are_skipped(proc: FakeProc) -> None:
    proc.process(600, "firefox", fds=(PROGRAM,))
    (proc.root / "601").mkdir()  # no fd directory, as for another user's process
    (proc.root / "602").mkdir()
    (proc.root / "602" / "fd").write_text("not a directory")
    assert [c.pid for c in scan(proc).consumers] == [600]


def test_a_missing_proc_is_survived(tmp_path: Path) -> None:
    assert ProcScanner(tmp_path / "nope", own_pid=1).scan({PROGRAM}).consumers == []


def test_a_symlinked_device_name_is_matched_by_its_real_path(proc: FakeProc, tmp_path: Path) -> None:
    real = tmp_path / "video-real"
    real.write_text("")
    link = tmp_path / "video-link"
    link.symlink_to(real)
    proc.process(700, "cheese", fds=(str(real),))
    assert [c.pid for c in scan(proc, str(link)).consumers] == [700]


def test_scanning_never_opens_a_device(proc: FakeProc, monkeypatch: pytest.MonkeyPatch) -> None:
    proc.process(800, "firefox", fds=(PROGRAM,))
    opened: list[str] = []
    real_open = open
    monkeypatch.setattr("builtins.open", lambda file, *a, **k: (opened.append(str(file)), real_open(file, *a, **k))[1])
    monkeypatch.setattr(os, "open", lambda path, *a, **k: pytest.fail(f"os.open({path})"))
    scan(proc)
    assert not [path for path in opened if path.startswith("/dev/video")]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["streamstack", "lights", "run"], True),
        (["/usr/bin/python3", "/usr/bin/streamstack-auto", "run"], True),
        (["python3", "-m", "streamstack", "console"], True),
        (["ffmpeg", "-i", "/dev/video0"], False),
        (["firefox", "--streamstack"], False),
        ([], False),
    ],
)
def test_is_streamstack_command(argv: list[str], expected: bool) -> None:
    assert camerause.is_streamstack_command(argv) is expected


def test_the_reason_names_the_apps() -> None:
    usage = Usage((Consumer(1, "ms-teams", PROGRAM), Consumer(2, "zoom", RAW)))
    assert usage.active
    assert usage.reason == "Teams and Zoom opened the camera"
    assert not Usage().active and Usage().reason == ""
    assert Usage((Consumer(1, "google-chrome", RAW),)).reason == "Chrome in a browser tab opened the camera"


FOLLOW = {"apps": True, "ignore": [], "devices": []}


def watcher(proc: FakeProc) -> CameraWatcher:
    return CameraWatcher(ProcScanner(proc.root, own_pid=999_999))


def test_the_watcher_reports_the_apps_holding_the_camera(proc: FakeProc) -> None:
    assert not watcher(proc).poll(FOLLOW, {PROGRAM}).active
    proc.process(11, "firefox", fds=(PROGRAM,))
    usage = watcher(proc).poll(FOLLOW, {PROGRAM})
    assert [c.pid for c in usage.consumers] == [11] and usage.active


def test_apps_can_be_switched_off(proc: FakeProc) -> None:
    proc.process(11, "firefox", fds=(PROGRAM,))
    assert not watcher(proc).poll({**FOLLOW, "apps": False}, {PROGRAM}).active


def test_the_ignore_list_is_passed_to_the_scan(proc: FakeProc) -> None:
    proc.process(11, "slack", fds=(PROGRAM,))
    assert not watcher(proc).poll({**FOLLOW, "ignore": ["Slack"]}, {PROGRAM}).active


def test_extra_devices_from_the_settings_are_watched(proc: FakeProc) -> None:
    proc.process(12, "cheese", fds=("/dev/video7",))
    assert not watcher(proc).poll(FOLLOW, {PROGRAM}).active
    assert watcher(proc).poll({**FOLLOW, "devices": ["/dev/video7"]}, {PROGRAM}).active


def test_closing_the_watcher_is_harmless(proc: FakeProc) -> None:
    watch = watcher(proc)
    watch.close()
    assert not watch.poll(FOLLOW, {PROGRAM}).active


def test_watched_devices_skip_an_external_raw_camera() -> None:
    cfg = {"STREAMSTACK_PROGRAM_DEVICE": PROGRAM, "STREAMSTACK_CAMERA_DEVICE": RAW, "STREAMSTACK_CAMERA_EXTERNAL": "0"}
    assert camerause.watched_devices(cfg) == {PROGRAM, RAW}
    assert camerause.watched_devices({**cfg, "STREAMSTACK_CAMERA_EXTERNAL": "1"}) == {PROGRAM}
