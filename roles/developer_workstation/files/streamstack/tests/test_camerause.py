"""Camera-use detection: a fake /proc tree, StreamStack's and OBS's own handles, helpers, and the OBS outputs."""

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


def test_obs_itself_is_not_a_consumer_but_marks_obs_running(proc: FakeProc) -> None:
    proc.process(103, "obs", fds=(PROGRAM, RAW))
    found = scan(proc)
    assert found.consumers == [] and found.obs_running


def test_streamstack_processes_are_plumbing(proc: FakeProc) -> None:
    proc.process(104, "python3", argv=["/usr/bin/python3", "-m", "streamstack", "console"], fds=(RAW,))
    proc.process(105, "python3", argv=["/usr/bin/python3", "-I", "/usr/bin/streamstack-console"], fds=(RAW,))
    proc.process(106, "streamstack", argv=["streamstack", "canon-bridge"], fds=(PROGRAM,))
    assert scan(proc).consumers == []


def test_our_own_pid_is_never_a_consumer(proc: FakeProc) -> None:
    proc.process(107, "python3", argv=["python3", "x.py"], fds=(RAW,))
    assert scan(proc, own=107).consumers == []


def test_a_media_helper_started_by_streamstack_or_obs_is_plumbing(proc: FakeProc) -> None:
    proc.process(200, "streamstack", argv=["streamstack", "console"])
    proc.process(201, "ffmpeg", ppid=200, fds=(RAW,))
    proc.process(300, "obs")
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


def test_the_reason_names_the_apps_and_obs() -> None:
    usage = Usage((Consumer(1, "ms-teams", PROGRAM), Consumer(2, "zoom", RAW)), ("recording",), True)
    assert usage.active
    assert usage.reason == "Teams and Zoom opened the camera and OBS recording started"
    assert not Usage().active and Usage().reason == ""
    assert Usage((Consumer(1, "google-chrome", RAW),)).reason == "Chrome in a browser tab opened the camera"


class FakeObs:
    def __init__(self, state: dict[str, bool] | None) -> None:
        self.answer = state
        self.asked = 0

    def state(self) -> dict[str, bool] | None:
        self.asked += 1
        return self.answer


FOLLOW = {"apps": True, "virtualcam": True, "stream": True, "record": True, "ignore": [], "devices": []}


def watcher(proc: FakeProc, obs: FakeObs) -> CameraWatcher:
    return CameraWatcher(ProcScanner(proc.root, own_pid=999_999), obs)


def test_obs_outputs_count_only_while_obs_runs(proc: FakeProc) -> None:
    obs = FakeObs({"virtualcam": True, "stream": False, "record": True})
    assert not watcher(proc, obs).poll(FOLLOW, {PROGRAM}).active
    assert obs.asked == 0  # OBS is not running, so the websocket is not even tried
    proc.process(10, "obs")
    usage = watcher(proc, obs).poll(FOLLOW, {PROGRAM})
    assert usage.obs == ("virtual camera", "recording") and usage.obs_known and usage.active


def test_each_source_can_be_switched_off(proc: FakeProc) -> None:
    proc.process(10, "obs")
    proc.process(11, "firefox", fds=(PROGRAM,))
    obs = FakeObs({"virtualcam": True, "stream": True, "record": True})
    only_obs = watcher(proc, obs).poll({**FOLLOW, "apps": False}, {PROGRAM})
    assert only_obs.consumers == () and only_obs.obs
    only_apps = watcher(proc, obs).poll({**FOLLOW, "virtualcam": False, "stream": False, "record": False}, {PROGRAM})
    assert [c.pid for c in only_apps.consumers] == [11] and only_apps.obs == ()
    nothing = watcher(proc, obs).poll(
        {**FOLLOW, "apps": False, "virtualcam": False, "stream": False, "record": False}, {PROGRAM}
    )
    assert not nothing.active


def test_obs_that_cannot_be_asked_is_not_known(proc: FakeProc) -> None:
    proc.process(10, "obs")
    usage = watcher(proc, FakeObs(None)).poll(FOLLOW, {PROGRAM})
    assert not usage.active and not usage.obs_known


def test_extra_devices_from_the_settings_are_watched(proc: FakeProc) -> None:
    proc.process(12, "cheese", fds=("/dev/video7",))
    assert not watcher(proc, FakeObs(None)).poll(FOLLOW, {PROGRAM}).active
    assert watcher(proc, FakeObs(None)).poll({**FOLLOW, "devices": ["/dev/video7"]}, {PROGRAM}).active


def test_watched_devices_skip_an_external_raw_camera() -> None:
    cfg = {"STREAMSTACK_PROGRAM_DEVICE": PROGRAM, "STREAMSTACK_CAMERA_DEVICE": RAW, "STREAMSTACK_CAMERA_EXTERNAL": "0"}
    assert camerause.watched_devices(cfg) == {PROGRAM, RAW}
    assert camerause.watched_devices({**cfg, "STREAMSTACK_CAMERA_EXTERNAL": "1"}) == {PROGRAM}


class FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail, self.closed = fail, False

    def call(self, request: str) -> dict:
        if self.fail:
            raise OSError("socket closed")
        return {"outputActive": request == "GetStreamStatus"}

    def close(self) -> None:
        self.closed = True


def test_the_websocket_probe_backs_off_after_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    attempts = []

    def connect(timeout: float = 2) -> FakeClient:
        attempts.append(now[0])
        raise OSError("refused")

    monkeypatch.setattr("streamstack.obs.OBS", connect)
    probe = camerause.WebsocketObs(clock=lambda: now[0])
    assert probe.state() is None and probe.state() is None
    assert attempts == [0.0]
    now[0] = camerause.OBS_RETRY_SECONDS + 1
    assert probe.state() is None
    assert len(attempts) == 2


def test_the_websocket_probe_reads_the_three_outputs_and_drops_a_dead_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    monkeypatch.setattr("streamstack.obs.OBS", lambda timeout=2: client)
    probe = camerause.WebsocketObs(clock=lambda: 0.0)
    assert probe.state() == {"virtualcam": False, "stream": True, "record": False}
    client.fail = True
    assert probe.state() is None
    assert client.closed
