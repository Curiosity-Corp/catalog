"""Studio, auto, health and system state for the Console, the demo twin and the structured doctor checks."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from streamstack import camlink, cli, config, micwatch, system, virtualdevices
from streamstack.console import studio
from streamstack.console.errors import ActionError
from streamstack.console.studio import DemoStudio, StudioController

STATE_KEYS = {"studio", "auto", "health", "system", "virtual"}


def wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    pytest.fail("condition not reached in time")


# -- pure helpers ---------------------------------------------------------------


def test_cpu_percent_from_proc_stat_deltas() -> None:
    before = studio.cpu_times("cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 2 3 4 5\n")
    after = studio.cpu_times("cpu  200 0 200 750 150 0 0 0 0 0\n")
    assert before == (200, 1000)
    assert after == (400, 1300)
    assert studio.cpu_percent(before, after) == pytest.approx(66.7)
    assert studio.cpu_percent(after, after) == 0.0
    assert studio.cpu_times("garbage") is None
    assert studio.cpu_times("") is None
    assert studio.cpu_times("cpu 1 2") is None


def write_journal(lines: list[object]) -> Path:
    path = config.state_dir() / "auto.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n")
    return path


def test_journal_is_newest_first_typed_and_tolerant() -> None:
    path = write_journal(
        [
            {"time": "2026-10-22T10:00:00", "event": "mic-gain", "before_db": 0.0, "after_db": 1.5},
            "{ half a line",
            {"time": "2026-10-22T10:01:00", "event": "ai", "confidence": 0.8, "proposal": {"notes": "Lifted gamma."}},
            {"time": "2026-10-22T10:02:00", "event": "video-error", "error": "the Console went away"},
            {"time": "2026-10-22T10:03:00", "event": "exposure"},  # missing fields: describe() must not crash us
            {"time": "2026-10-22T10:04:00", "event": "ai-skipped", "reason": "the Console is not running"},
            [1, 2, 3],
        ]
    )
    entries = studio.read_journal(path)
    assert [e["event"] for e in entries] == ["ai-skipped", "exposure", "video-error", "ai", "mic-gain"]
    assert [e["kind"] for e in entries] == ["ai", "video", "error", "ai", "audio"]
    assert entries[4]["text"] == "+0.0 dB -> +1.5 dB"
    assert entries[2]["text"] == "the Console went away"
    note, when = studio.last_note(entries)
    assert (note, when) == ("Lifted gamma.", "2026-10-22T10:01:00")
    assert set(studio.public_entries(entries)[0]) == {"time", "event", "text", "kind"}


def test_journal_limit_and_missing_file(tmp_path: Path) -> None:
    path = write_journal([{"time": f"t{i}", "event": "ai-skipped", "reason": str(i)} for i in range(100)])
    entries = studio.read_journal(path)
    assert len(entries) == 30 and entries[0]["text"] == "99"
    assert studio.read_journal(tmp_path / "absent.jsonl") == []
    assert studio.last_note([]) == (None, None)


def test_journal_tail_drops_the_cut_off_first_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "JOURNAL_TAIL_BYTES", 200)
    path = write_journal([{"time": f"t{i}", "event": "ai-skipped", "reason": "x" * 20} for i in range(20)])
    entries = studio.read_journal(path)
    assert 1 <= len(entries) < 20
    assert all(e["text"] == "x" * 20 for e in entries)


# -- doctor as structured data ---------------------------------------------------------


@pytest.fixture
def healthy_rig(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config.load_settings()
    names = {cfg["STREAMSTACK_CAMERA_DEVICE"]: "Canon EOS 50D", cfg["STREAMSTACK_PROGRAM_DEVICE"]: "StreamStack Camera"}
    monkeypatch.setattr(cli, "video_name", lambda device: names.get(device, ""))
    monkeypatch.setattr(cli, "loopback_has_producer", lambda device: True)
    monkeypatch.setattr(cli, "current_format", lambda device: "1920x1080 YUYV @ 30 fps")
    monkeypatch.setattr(cli, "usb_products", lambda vendor: ["Canon EOS 50D"])
    monkeypatch.setattr(cli, "user_unit_active", lambda unit: True)
    monkeypatch.setattr(cli, "find_camlink", lambda: "/dev/video4")
    monkeypatch.setattr(
        cli.camlink,
        "probe",
        lambda cfg=None, sources=None: camlink.Probe(
            device="/dev/video4",
            offered=[("YUYV", 1920, 1080, 59.94)],
            chosen=("YUYV", 1920, 1080, 59.94),
            audio="Cam Link 4K Digital Stereo",
        ),
    )
    monkeypatch.setattr(
        cli, "audio_sources", lambda: {"alsa_input.wave": "Wave:3", system.VOICE_SOURCE: "StreamStack Mic"}
    )
    monkeypatch.setattr(system, "default_device", lambda kind: "")
    monkeypatch.setattr(virtualdevices, "can_open", lambda device: True)
    monkeypatch.setattr(
        virtualdevices,
        "camera_report",
        lambda device, label, **kw: virtualdevices.CameraReport(
            device, label, name=label, exclusive_caps=True, access=True
        ),
    )
    monkeypatch.setattr(virtualdevices, "mic_report", lambda *a, **kw: virtualdevices.MicReport())
    monkeypatch.setattr(virtualdevices, "seen_by_apps", lambda cfg, **kw: [])
    monkeypatch.setattr(cli.voice_graph, "available_engines", lambda: ["deepfilternet"])
    monkeypatch.setattr(cli, "have", lambda tool: True)
    monkeypatch.setattr(
        cli.framing_health,
        "health_check",
        lambda cfg: cli.check("video", "framing", "ok", "Auto-framing is not installed (optional)"),
    )
    monkeypatch.setattr(cli, "hardware_report", lambda found: {"rig": None, "checks": []})  # tests/test_hw_doctor.py
    monkeypatch.setattr(cli, "_watch_microphone", lambda mic: micwatch.StallProbe(False, "healthy", "scheduled"))


def test_run_checks_returns_structured_results(healthy_rig: None) -> None:
    checks = cli.run_checks()
    assert {c["group"] for c in checks} == {"video", "audio", "apps"}
    assert all(set(c) == {"id", "group", "state", "title", "detail", "hint"} for c in checks)
    assert all(c["state"] in ("ok", "warn", "fail") for c in checks)
    assert len({c["id"] for c in checks}) == len(checks)
    by_id = {c["id"]: c for c in checks}
    assert by_id["camera-node"]["title"] == "Canon EOS 50D (/dev/video42) is live"
    assert by_id["camera-node"]["detail"] == "1920x1080 YUYV @ 30 fps"
    assert by_id["microphone"]["detail"] == "Wave:3"
    assert cli.summarize(checks) == "ok"


def test_run_checks_flags_what_is_missing(healthy_rig: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "video_name", lambda device: "")
    monkeypatch.setattr(cli, "audio_sources", lambda: {})
    monkeypatch.setattr(cli.camlink, "probe", lambda cfg=None, sources=None: camlink.Probe())
    checks = {c["id"]: c for c in cli.run_checks()}
    assert checks["camera-node"]["state"] == "fail" and "missing" in checks["camera-node"]["title"]
    assert checks["voice-chain"]["state"] == "fail"
    assert checks["microphone"]["state"] == "warn" and checks["microphone"]["hint"]
    assert checks["camlink"]["state"] == "warn"
    assert cli.summarize(list(checks.values())) == "fail"
    assert cli.summarize([]) == "ok"
    assert cli.summarize([cli.check("apps", "x", "warn", "t")]) == "warn"


def test_doctor_prints_the_checks_as_before(
    healthy_rig: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Video\n")
    assert "\nAudio\n" in out and "\nApps\n" in out
    assert "[  ok] StreamStack Camera (/dev/video43) is live: 1920x1080 YUYV @ 30 fps" in out
    assert "[  ok] call microphone: Wave:3" in out
    assert "Pick these in Teams / Zoom" in out and "Microphone: StreamStack Mic" in out

    monkeypatch.setattr(cli, "audio_sources", lambda: {})
    monkeypatch.setattr(cli.voice_graph, "missing_required", lambda: [])
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] the voice chain is not running, so there is no processed voice" in out
    assert "       -> streamstack chain start (or open the Console, which starts it)" in out


def test_call_camera_matches_the_status_line(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config.load_settings()
    monkeypatch.setattr(cli, "loopback_has_producer", lambda device: device == cfg["STREAMSTACK_PROGRAM_DEVICE"])
    assert cli.call_camera(cfg, True) == "graded"
    monkeypatch.setattr(cli, "loopback_has_producer", lambda device: False)
    assert cli.call_camera(cfg, True) == "idle"
    assert cli.call_camera(cfg, False) == "off"


# -- StudioController -----------------------------------------------------------------


class Harness:
    """A controller whose probes, doctor and CLI are fakes the test steers."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.gate = threading.Event()
        self.gate.set()
        self.code = 0
        self.output = ""
        self.checks_runs = 0
        self.toasts: list[tuple[str, str]] = []
        self.facts = {"running": True, "camera_for_calls": "graded", "bridge": True, "voice_chain": True}

    def runner(self, args: list[str]) -> tuple[int, str]:
        self.commands.append(args)
        assert self.gate.wait(10)
        return self.code, self.output

    def probe(self, cfg: dict[str, str]) -> dict:
        return dict(self.facts)

    def checks(self, cfg: dict[str, str]) -> list[dict[str, str]]:
        self.checks_runs += 1
        return [cli.check("video", "camera-node", "warn", "no picture", hint="plug it in")]

    def controller(self) -> StudioController:
        ctl = StudioController(
            probe_seconds=0.05, health_seconds=60, runner=self.runner, probe=self.probe, checks=self.checks
        )
        ctl.notify = lambda level, text: self.toasts.append((level, text))
        return ctl


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setattr(system, "user_unit_active", lambda unit: unit == cli.AUTO_UNIT)
    return Harness()


def test_state_is_complete_before_any_probe_finished(harness: Harness) -> None:
    state = harness.controller().state()
    assert set(state) == STATE_KEYS
    assert state["studio"]["running"] is None and state["studio"]["busy"] is False
    assert state["studio"]["program_label"] == "StreamStack Camera"
    assert state["auto"]["journal"] == [] and state["auto"]["backend"] == "claude-cli"
    assert state["health"] == {"updated": None, "summary": None, "checks": []}
    assert state["system"]["cores"] >= 1 and state["system"]["cpu_percent"] is None


def test_background_probes_fill_the_cache(harness: Harness) -> None:
    write_journal([{"time": "2026-10-22T10:01:00", "event": "ai", "proposal": {"notes": "Looks good."}}])
    ctl = harness.controller()
    ctl.start()
    try:
        wait_for(lambda: ctl.state()["health"]["summary"] == "warn" and ctl.state()["auto"]["running"] is True)
        state = ctl.state()
        assert state["studio"]["running"] is True and state["studio"]["camera_for_calls"] == "graded"
        assert state["health"]["checks"][0]["id"] == "camera-node" and state["health"]["updated"]
        assert state["auto"]["last_note"] == "Looks good." and state["auto"]["journal"][0]["kind"] == "ai"
        wait_for(lambda: ctl.state()["system"]["cpu_percent"] is not None)
        assert 0 <= ctl.state()["system"]["cpu_percent"] <= 100
        started = time.monotonic()
        ctl.state()
        assert time.monotonic() - started < 0.05
    finally:
        ctl.stop()


def test_a_crashing_check_becomes_a_failed_card(harness: Harness) -> None:
    def broken(cfg: dict[str, str]) -> list[dict[str, str]]:
        raise RuntimeError("v4l2-ctl exploded")

    ctl = StudioController(checks=broken, probe=harness.probe, runner=harness.runner)
    ctl._refresh_health()
    health = ctl.state()["health"]
    assert health["summary"] == "fail" and "v4l2-ctl exploded" in health["checks"][0]["detail"]


def test_studio_start_returns_at_once_and_reports_by_toast(harness: Harness) -> None:
    harness.gate.clear()
    ctl = harness.controller()
    started = time.monotonic()
    assert ctl.act("studio.start", {}) == "Starting the studio..."
    assert time.monotonic() - started < 0.5  # the HTTP thread is not held for the studio start
    wait_for(lambda: harness.commands == [["studio"]])
    assert ctl.state()["studio"]["busy"] is True
    with pytest.raises(ActionError) as busy:
        ctl.act("studio.stop", {})
    assert busy.value.status == 409
    harness.gate.set()
    wait_for(lambda: not ctl.state()["studio"]["busy"] and harness.toasts)
    assert harness.toasts == [("ok", "Studio started")]


def test_failures_toast_the_cli_output(harness: Harness) -> None:
    harness.code, harness.output = 1, "Starting the studio...\nffmpeg: command not found"
    ctl = harness.controller()
    ctl.act("studio.stop", {})
    wait_for(lambda: harness.toasts)
    assert harness.toasts == [("error", "Starting the studio... ffmpeg: command not found")]
    assert harness.commands == [["stop"]] and not ctl.state()["studio"]["busy"]


def test_a_runner_that_raises_still_clears_busy(harness: Harness, capsys: pytest.CaptureFixture) -> None:
    def explode(args: list[str]) -> tuple[int, str]:
        raise OSError("no fork")

    ctl = harness.controller()
    ctl._runner = explode
    ctl.act("auto.on", {})
    wait_for(lambda: harness.toasts)
    assert harness.toasts[0][0] == "error" and "no fork" in harness.toasts[0][1]
    assert not ctl.state()["auto"]["busy"]
    assert "Traceback" in capsys.readouterr().err


def test_auto_actions_map_to_the_cli(harness: Harness) -> None:
    ctl = harness.controller()
    for action, expected in (
        ("auto.on", [["auto", "on"]]),
        ("auto.off", [["auto", "off"]]),
        ("auto.once", [["auto", "once"]]),
    ):
        harness.commands.clear()
        harness.toasts.clear()
        ctl.act(action, {})
        wait_for(lambda: harness.toasts)
        assert harness.commands == expected


def test_auto_reset_turns_auto_off_first_when_it_runs(harness: Harness) -> None:
    ctl = harness.controller()
    ctl._auto["running"] = True
    ctl.act("auto.reset", {})
    wait_for(lambda: harness.toasts)
    assert harness.commands == [["auto", "off"], ["auto", "reset"]]
    harness.commands.clear()
    harness.toasts.clear()
    ctl._auto["running"] = False
    ctl.act("auto.reset", {})
    wait_for(lambda: harness.toasts)
    assert harness.commands == [["auto", "reset"]]


def test_health_refresh_reruns_the_checks(harness: Harness) -> None:
    ctl = harness.controller()
    ctl.start()
    try:
        wait_for(lambda: harness.checks_runs == 1)
        assert ctl.act("health.refresh", {}) == "Checking..."
        wait_for(lambda: harness.checks_runs == 2)
    finally:
        ctl.stop()


def test_open_app_validates_and_launches(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr(system, "detach", launched.append)
    monkeypatch.setattr(system, "have", lambda tool: tool != "qv4l2")
    ctl = harness.controller()
    for params in ({}, {"name": 3}, {"name": "firefox"}, {"name": "firefox; reboot"}):
        with pytest.raises(ActionError) as bad:
            ctl.act("open.app", params)
        assert bad.value.status == 400
    assert ctl.act("open.app", {"name": "qpwgraph"}) == "Opening qpwgraph..."
    assert launched == [["qpwgraph"]]
    with pytest.raises(ActionError) as missing:
        ctl.act("open.app", {"name": "qv4l2"})
    assert missing.value.status == 409
    with pytest.raises(ActionError):
        ctl.act("studio.explode", {})


def test_run_cli_runs_the_package_in_a_child() -> None:
    code, output = studio.run_cli(["--version"])
    assert code == 0 and output.startswith("streamstack ")
    assert studio.run_cli(["no-such-command"])[0] != 0


# -- DemoStudio -----------------------------------------------------------------------


def test_demo_state_is_believable() -> None:
    demo = DemoStudio()
    state = demo.state()
    assert set(state) == STATE_KEYS
    assert state["studio"]["running"] is True and state["studio"]["camera_for_calls"] == "graded"
    assert state["health"]["summary"] == "warn"
    assert {c["state"] for c in state["health"]["checks"]} == {"ok", "warn"}
    journal = state["auto"]["journal"]
    assert len(journal) >= 4 and {e["kind"] for e in journal} >= {"audio", "ai", "video"}
    assert state["auto"]["last_note"] and state["auto"]["can_undo"] is True
    assert 0 < state["system"]["cpu_percent"] < 100


def test_demo_cpu_wanders() -> None:
    demo = DemoStudio()
    first = demo.state()["system"]["cpu_percent"]
    demo._t0 -= 3.0
    assert demo.state()["system"]["cpu_percent"] != first


def test_demo_studio_stop_and_start_flip_state() -> None:
    demo = DemoStudio(delay=0)
    toasts: list[tuple[str, str]] = []
    demo.notify = lambda level, text: toasts.append((level, text))
    assert demo.act("studio.stop", {}) == "Stopping the studio..."
    state = demo.state()
    assert state["studio"]["running"] is False and state["studio"]["camera_for_calls"] == "raw"
    assert next(c for c in state["health"]["checks"] if c["id"] == "program-node")["state"] == "warn"
    demo.act("studio.start", {})
    assert demo.state()["studio"]["running"] is True
    assert toasts == [("ok", "Studio stopped"), ("ok", "Studio started")]


def test_demo_busy_while_a_slow_action_runs() -> None:
    demo = DemoStudio(delay=1.5)  # long enough that a loaded CI runner cannot finish it between two calls
    demo.act("studio.stop", {})
    assert demo.state()["studio"]["busy"] is True and demo.state()["studio"]["running"] is True
    with pytest.raises(ActionError) as busy:
        demo.act("studio.start", {})
    assert busy.value.status == 409
    wait_for(lambda: demo.state()["studio"]["running"] is False)
    assert demo.state()["studio"]["busy"] is False


def test_demo_auto_actions() -> None:
    demo = DemoStudio(delay=0)
    demo.act("auto.on", {})
    state = demo.state()["auto"]
    assert state["running"] is True and state["journal"][0]["text"] == "auto mode started"
    demo.act("auto.once", {})
    assert demo.state()["auto"]["last_note"].startswith("(0.9)")
    demo.act("auto.reset", {})
    assert demo.state()["auto"]["can_undo"] is False
    demo.act("auto.off", {})
    assert demo.state()["auto"]["running"] is False


def test_demo_health_refresh_and_open_app() -> None:
    demo = DemoStudio(delay=0)
    demo._checked = "2000-01-01T00:00:00"
    assert demo.act("health.refresh", {}) == "Checking..."
    assert demo.state()["health"]["updated"] != "2000-01-01T00:00:00"
    assert "nothing is launched" in (demo.act("open.app", {"name": "qpwgraph"}) or "")
    with pytest.raises(ActionError) as bad:
        demo.act("open.app", {"name": "rm"})
    assert bad.value.status == 400
    with pytest.raises(ActionError):
        demo.act("nope.nope", {})


def test_doctor_shows_the_stall_check_right_after_the_microphone(
    healthy_rig: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [c["id"] for c in cli.run_checks()]
    assert ids.index("mic-stall") == ids.index("microphone") + 1
    assert {c["id"]: c for c in cli.run_checks()}["mic-stall"]["state"] == "ok"

    monkeypatch.setattr(cli, "_watch_microphone", lambda mic: micwatch.StallProbe(True, "stalled", "not scheduled"))
    checks = {c["id"]: c for c in cli.run_checks()}
    assert checks["mic-stall"]["state"] == "fail"
    assert "nobody can hear you" in checks["mic-stall"]["title"]
    assert cli.summarize(list(checks.values())) == "fail"

    monkeypatch.setattr(cli, "_watch_microphone", lambda mic: micwatch.StallProbe(None))
    assert "mic-stall" not in {c["id"] for c in cli.run_checks()}  # unknown: say nothing rather than guess


def test_console_health_leaves_the_stall_probe_to_the_audio_controller(
    healthy_rig: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(mic: str) -> micwatch.StallProbe:
        raise AssertionError("the 30 s health refresh must not sample the microphone")

    monkeypatch.setattr(cli, "_watch_microphone", boom)
    assert "mic-stall" not in {c["id"] for c in studio.console_checks(config.load_settings())}


def test_demo_studio_state_is_a_pure_function_of_its_clock() -> None:
    """The root cause of a flaky 'state is pushed only on change': the demo CPU figure follows the wall
    clock and moves by 0.1 every few milliseconds, so two snapshots a moment apart could differ."""
    now = [0.0]
    demo = DemoStudio(delay=0, clock=lambda: now[0])
    first = demo.state()
    assert demo.state() == first  # same instant, same snapshot, however long the test takes to run
    now[0] = 0.02
    assert demo.state()["system"]["cpu_percent"] != first["system"]["cpu_percent"]
