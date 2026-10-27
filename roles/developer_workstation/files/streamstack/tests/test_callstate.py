"""Call-state detection: pactl parsing, evidence from a fake /proc, the steady state machine, the monitor, and the
privacy promise (read only, never the camera, never a stream)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from streamstack import callstate
from streamstack.callstate import (
    AppEvidence,
    CallDetector,
    CallMonitor,
    CallState,
    CaptureStream,
    ExternalMute,
    Gathered,
    MicState,
    app_name,
    current_call,
    gather,
    parse_source_outputs,
    read_window_titles,
)

CFG = {
    "STREAMSTACK_PROGRAM_DEVICE": "/dev/video43",
    "STREAMSTACK_CAMERA_DEVICE": "/dev/video42",
    "STREAMSTACK_CAMERA_EXTERNAL": "",
}


def output(
    binary: str, name: str = "", *, pid: int = 4242, corked: bool = False, mute: bool = False, **props: str
) -> dict:
    return {
        "index": 1,
        "corked": corked,
        "mute": mute,
        "properties": {
            "application.process.binary": binary,
            "application.name": name or binary,
            "application.process.id": str(pid),
            **props,
        },
    }


def pactl(*outputs: dict[str, Any]) -> str:
    return json.dumps(list(outputs))


class Done:
    def __init__(self, stdout: str = "", code: int = 0) -> None:
        self.stdout, self.returncode = stdout, code


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_call_apps_are_named_and_other_clients_are_not() -> None:
    assert app_name("ms-teams").label == "Teams"
    assert app_name("msedge").label == "Edge" and app_name("msedge").browser
    assert app_name("zoom").label == "Zoom"
    assert app_name("chrome").label == "Chrome"
    for other in ("parec", "pacat", "obs", "ffmpeg", "easyeffects", "streamstack-console", "pw-record", ""):
        assert app_name(other) is None


def test_streamstack_never_counts_even_with_a_browser_like_name() -> None:
    assert app_name("streamstack-chrome-helper") is None


def test_parse_keeps_call_apps_only_and_reads_the_flags() -> None:
    text = pactl(
        output("pacat", "parec"),
        output("msedge", "Microsoft Edge input", pid=100),
        output("zoom", "ZOOM VoiceEngine", pid=200, corked=True),
        output("teams", pid=300, mute=True),
    )
    streams = parse_source_outputs(text)
    assert [(s.app, s.pid, s.corked, s.muted, s.live) for s in streams] == [
        ("Edge", 100, False, False, True),
        ("Zoom", 200, True, False, False),
        ("Teams", 300, False, True, False),
    ]


def test_screen_share_audio_captured_from_a_sink_is_not_a_microphone() -> None:
    monitor = output("chrome", **{"stream.capture.sink": "true"})
    assert parse_source_outputs(pactl(monitor)) == []


@pytest.mark.parametrize("text", ["", "not json", "{}", "null", "[1, 2, null]", '[{"properties": 3}]'])
def test_parse_survives_garbage(text: str) -> None:
    assert parse_source_outputs(text) == []


def test_read_capture_streams_reports_a_server_that_does_not_answer() -> None:
    assert callstate.read_capture_streams(lambda *a, **k: None) == ([], False)
    assert callstate.read_capture_streams(lambda *a, **k: Done("", 1)) == ([], False)
    streams, ok = callstate.read_capture_streams(lambda *a, **k: Done(pactl(output("zoom"))))
    assert ok and [s.app for s in streams] == ["Zoom"]


def test_only_a_read_only_pactl_listing_is_ever_run() -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str], timeout: float = 0) -> Done:
        seen.append(argv)
        return Done("[]")

    callstate.read_capture_streams(runner)
    assert seen == [["pactl", "-f", "json", "list", "source-outputs"]]


# ---------------------------------------------------------------------------
# evidence: /proc plus pactl
# ---------------------------------------------------------------------------


class FakeProc:
    def __init__(self, root: Path) -> None:
        self.root = root

    def process(self, pid: int, comm: str, *, ppid: int = 1, fds: tuple[str, ...] = ()) -> None:
        base = self.root / str(pid)
        (base / "fd").mkdir(parents=True)
        (base / "comm").write_text(comm + "\n")
        (base / "cmdline").write_bytes(comm.encode() + b"\0")
        (base / "stat").write_text(f"{pid} ({comm}) S {ppid} 1 1 0 -1\n")
        for number, target in enumerate(fds, start=3):
            os.symlink(target, base / "fd" / str(number))


@pytest.fixture
def proc(tmp_path: Path) -> FakeProc:
    root = tmp_path / "proc"
    root.mkdir()
    return FakeProc(root)


def scanner(proc: FakeProc) -> callstate.camerause.ProcScanner:
    return callstate.camerause.ProcScanner(proc.root, own_pid=999_999)


def runner_for(*outputs: dict[str, Any], titles: str = "", titles_ok: bool = True) -> Any:
    def run(argv: list[str], timeout: float = 0) -> Done | None:
        if argv[0] == "pactl":
            return Done(pactl(*outputs))
        if argv[0] == "wmctrl":
            return Done(titles) if titles_ok else None
        raise AssertionError(argv)

    return run


def test_a_call_app_with_the_camera_and_a_stream_is_one_app(proc: FakeProc) -> None:
    proc.process(100, "msedge", fds=("/dev/video43",))
    proc.process(101, "msedge", ppid=100)  # the browser's other processes do not multiply the app
    found = gather(CFG, scanner=scanner(proc), runner=runner_for(output("msedge", pid=101)))
    assert found.audio_ok and found.video_ok
    assert [(a.app, a.camera, len(a.streams)) for a in found.apps] == [("Edge", True, 1)]


def test_non_call_consumers_are_not_a_call(proc: FakeProc) -> None:
    proc.process(100, "cheese", fds=("/dev/video43",))
    proc.process(101, "obs", fds=("/dev/video43",))
    found = gather(CFG, scanner=scanner(proc), runner=runner_for(output("pacat", "parec"), output("ffmpeg")))
    assert found.apps == []


def test_streamstack_own_handles_are_not_a_call(proc: FakeProc) -> None:
    proc.process(100, "python3", fds=("/dev/video43",))
    (proc.root / "100" / "cmdline").write_bytes(b"python3\0-m\0streamstack\0console\0")
    found = gather(CFG, scanner=scanner(proc), runner=runner_for())
    assert found.apps == []


def test_a_zoom_stream_alone_is_evidence(proc: FakeProc) -> None:
    found = gather(CFG, scanner=scanner(proc), runner=runner_for(output("zoom", "ZOOM VoiceEngine")))
    assert [(a.app, a.camera) for a in found.apps] == [("Zoom", False)]


def test_window_titles_name_the_service_behind_a_browser(proc: FakeProc) -> None:
    proc.process(100, "chrome", fds=("/dev/video43",))
    titles = "0x04000003  0 100 desk Meet - abc-defg-hij - Google Chrome\n0x04000009  0 55 desk some other window\n"
    found = gather(CFG, scanner=scanner(proc), runner=runner_for(output("chrome"), titles=titles))
    detector = CallDetector(settle_s=0)
    snap = detector.update(found.apps, now=0.0)
    assert snap.app == "Chrome"
    # the title lookup reads the window owner's name from /proc, which the fake does not provide for pid 55
    assert read_window_titles(runner_for(titles=titles), proc.root) == {
        "Chrome": ("Meet - abc-defg-hij - Google Chrome",)
    }


@pytest.mark.parametrize(
    ("title", "service"),
    [
        ("Meet - abc-defg-hij - Google Chrome", "Google Meet"),
        ("meet.google.com is sharing - Microsoft Edge", "Google Meet"),
        ("Chat | Team | Microsoft Teams - Microsoft Edge", "Teams"),
        ("Zoom Meeting - Firefox", "Zoom"),
        ("Inbox - Mail - Chrome", "Chrome"),
    ],
)
def test_service_from_titles(title: str, service: str) -> None:
    assert AppEvidence("Chrome", True, True, titles=(title,)).service == service


def test_titles_are_not_looked_up_when_nothing_could_be_a_call(proc: FakeProc) -> None:
    calls: list[str] = []

    def run(argv: list[str], timeout: float = 0) -> Done:
        calls.append(argv[0])
        return Done("[]")

    gather(CFG, scanner=scanner(proc), runner=run)
    assert calls == ["pactl"]


def test_a_missing_wmctrl_or_pactl_is_not_an_error(proc: FakeProc) -> None:
    proc.process(100, "zoom", fds=("/dev/video43",))
    found = gather(CFG, scanner=scanner(proc), runner=lambda *a, **k: None)
    assert [a.app for a in found.apps] == ["Zoom"] and not found.audio_ok


def test_scanning_never_opens_a_video_device(proc: FakeProc, monkeypatch: pytest.MonkeyPatch) -> None:
    proc.process(100, "zoom", fds=("/dev/video43",))
    opened: list[str] = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda path, *a, **k: (opened.append(str(path)), real_open(path, *a, **k))[1])
    gather(CFG, scanner=scanner(proc), runner=runner_for())
    assert not [p for p in opened if p.startswith("/dev/video")]


# ---------------------------------------------------------------------------
# the state machine
# ---------------------------------------------------------------------------


def stream(*, corked: bool = False, muted: bool = False, app: str = "Teams") -> CaptureStream:
    return CaptureStream(app, app.lower(), 1, corked, muted)


def teams(*, camera: bool = True, streams: tuple[CaptureStream, ...] = (stream(),)) -> list[AppEvidence]:
    return [AppEvidence("Teams", False, camera, streams)]


def test_idle_joining_in_call_idle() -> None:
    d = CallDetector(settle_s=4.0, leave_s=3.0)
    assert d.update([], now=0.0).state is CallState.IDLE
    joining = d.update(teams(), now=1.0)
    assert joining.state is CallState.JOINING and joining.active and not joining.in_call
    assert joining.service == "Teams" and joining.since == 1.0
    assert d.update(teams(), now=4.9).state is CallState.JOINING
    in_call = d.update(teams(), now=5.0)
    assert in_call.state is CallState.IN_CALL and in_call.in_call and in_call.since == 5.0
    # steady: the same state keeps its start
    assert d.update(teams(), now=20.0).since == 5.0
    assert d.update([], now=21.0).state is CallState.IN_CALL  # a gap shorter than the leave hold
    assert d.update([], now=23.9).state is CallState.IN_CALL
    idle = d.update([], now=24.1)
    assert idle.state is CallState.IDLE and not idle.active and idle.service is None


def test_a_browser_reopening_its_stream_does_not_end_the_call() -> None:
    d = CallDetector(settle_s=1.0, leave_s=3.0)
    for t in range(0, 4):
        d.update(teams(), now=float(t))
    assert d.snapshot.in_call
    d.update([], now=4.0)
    assert d.update(teams(), now=5.5).state is CallState.IN_CALL
    d.update([], now=6.0)  # the hold restarts for the next gap
    assert d.update([], now=8.9).state is CallState.IN_CALL
    assert d.update([], now=9.1).state is CallState.IDLE


def test_camera_without_a_microphone_stream_stays_joining() -> None:
    d = CallDetector(settle_s=1.0)
    d.update(teams(streams=()), now=0.0)
    snap = d.update(teams(streams=()), now=60.0)
    assert snap.state is CallState.JOINING and snap.camera and snap.mic is MicState.MUTED


def test_a_stream_alone_becomes_a_call_without_the_camera() -> None:
    d = CallDetector(settle_s=2.0)
    d.update(teams(camera=False), now=0.0)
    snap = d.update(teams(camera=False), now=2.0)
    assert snap.in_call and not snap.camera


def test_mic_state_from_the_streams() -> None:
    d = CallDetector(settle_s=0.0)
    assert d.update(teams(), now=0.0).mic is MicState.OPEN
    assert d.update(teams(streams=(stream(corked=True),)), now=1.0).mic is MicState.MUTED
    assert d.update(teams(streams=(stream(muted=True),)), now=2.0).mic is MicState.MUTED
    assert d.update(teams(streams=(stream(muted=True), stream())), now=3.0).mic is MicState.OPEN
    assert d.update([], now=3.1).mic is MicState.OPEN  # held through the leave gap
    assert CallDetector().snapshot.mic is MicState.NONE


def test_an_adapter_report_wins_while_fresh_and_expires() -> None:
    d = CallDetector(settle_s=0.0)
    report = ExternalMute(True, "extension", at=0.0)
    assert d.update(teams(), report, now=1.0).mic is MicState.MUTED  # the stream is live but the app says muted
    assert d.update(teams(), ExternalMute(False, "extension", 0.0), now=2.0).mic is MicState.OPEN
    stale = d.update(teams(streams=(stream(corked=True),)), report, now=callstate.EXTERNAL_FRESH_S + 5)
    assert stale.mic is MicState.MUTED and stale.external is None  # inferred from the corked stream again


def test_service_prefers_the_app_with_the_microphone() -> None:
    d = CallDetector(settle_s=0.0)
    apps = [AppEvidence("Chrome", True, True, ()), AppEvidence("Zoom", False, False, (stream(app="Zoom"),))]
    snap = d.update(apps, now=0.0)
    assert snap.service == "Zoom" and snap.apps == ("Chrome", "Zoom")
    assert "Zoom" in snap.reason and "Chrome" in snap.reason


def test_reason_reads_as_a_sentence() -> None:
    snap = CallDetector(settle_s=0.0).update(teams(), now=0.0)
    assert snap.reason == "Teams has the camera and a microphone stream open"
    assert snap.public()["state"] == "in_call" and snap.public()["active"] is True
    meet = AppEvidence("Edge", True, True, (stream(app="Edge"),), ("Meet - x - Edge",))
    assert CallDetector(settle_s=0.0).update([meet], now=0.0).reason.startswith("Google Meet in Edge has")


def test_current_call_is_a_one_shot_without_hysteresis(proc: FakeProc) -> None:
    assert current_call(CFG, scanner=scanner(proc), runner=runner_for()).state is CallState.IDLE
    proc.process(100, "zoom", fds=("/dev/video43",))
    snap = current_call(CFG, scanner=scanner(proc), runner=runner_for(output("zoom", pid=100)))
    assert snap.active and snap.service == "Zoom"


# ---------------------------------------------------------------------------
# the monitor
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_monitor_notifies_changes_once_and_survives_bad_listeners() -> None:
    clock = Clock()
    present: list[AppEvidence] = []
    monitor = CallMonitor(
        CFG,
        source=lambda: Gathered(list(present), True, True),
        detector=CallDetector(settle_s=2.0, leave_s=1.0, clock=clock),
        clock=clock,
    )
    heard: list[tuple[str, str]] = []

    def boom(before: Any, after: Any) -> None:
        raise RuntimeError("listener bug")

    monitor.subscribe(boom)
    unsubscribe = monitor.subscribe(lambda b, a: heard.append((b.state.value, a.state.value)))
    for t, apps in ((0, []), (1, teams()), (2, teams()), (3, teams()), (4, teams()), (5, []), (7, [])):
        clock.now = float(t)
        present[:] = apps
        monitor.poll_once()
    assert heard == [("idle", "joining"), ("joining", "in_call"), ("in_call", "idle")]
    unsubscribe()
    clock.now = 9.0
    present[:] = teams()
    monitor.poll_once()
    assert len(heard) == 3


def test_a_failing_probe_keeps_the_last_snapshot() -> None:
    clock = Clock()
    boom = [False]

    def source() -> Gathered:
        if boom[0]:
            raise OSError("pactl exploded")
        return Gathered(teams(), True, True)

    monitor = CallMonitor(CFG, source=source, detector=CallDetector(settle_s=0.0, clock=clock), clock=clock)
    monitor.poll_once()
    boom[0] = True
    assert monitor.poll_once().active


def test_monitor_health_and_adapter_reports() -> None:
    clock = Clock()
    monitor = CallMonitor(
        CFG,
        source=lambda: Gathered(teams(), False, True),
        detector=CallDetector(settle_s=0.0, clock=clock),
        clock=clock,
    )
    monitor.poll_once()
    assert not monitor.health().audio and monitor.health().video
    monitor.report_mute(True, "extension")
    assert monitor.external() == ExternalMute(True, "extension", 0.0)
    assert monitor.poll_once().mic is MicState.MUTED
    monitor.report_mute(None, "")
    assert monitor.external() is None


def test_the_monitor_thread_starts_and_stops() -> None:
    monitor = CallMonitor(CFG, source=lambda: Gathered(teams(), True, True), poll_s=0.01)
    monitor.start()
    monitor.start()  # idempotent
    deadline = time.monotonic() + 3
    while not monitor.snapshot().active and time.monotonic() < deadline:
        time.sleep(0.01)
    assert monitor.snapshot().active
    monitor.stop()
    assert monitor._thread is None
