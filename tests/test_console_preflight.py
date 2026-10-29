"""The Console's pre-flight controller: a run end to end on fakes, timeouts, fixes, history, the demo faults."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from streamstack import preflight
from streamstack import store as filestore
from streamstack.console.errors import ActionError
from streamstack.console.preflight import (
    HISTORY_MAX,
    DemoPreflight,
    PreflightController,
    empty_state,
)
from streamstack.console.rig import Rig
from streamstack.preflight import NetworkFacts


def wait_done(ctl: PreflightController, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = ctl.state()
        if st["state"] != "running":
            return st
        time.sleep(0.01)
    raise AssertionError("pre-flight never finished")


@pytest.fixture(scope="module")
def demo_snapshot() -> dict[str, Any]:
    rig = Rig.demo()
    rig.start()
    try:
        return rig.snapshot()
    finally:
        rig.stop()


def controller(
    snapshot: dict[str, Any], tmp_path: Path | None = None, **kw: Any
) -> tuple[PreflightController, list[tuple[str, str]], list[tuple[str, dict]]]:
    toasts: list[tuple[str, str]] = []
    acts: list[tuple[str, dict]] = []
    ctl = PreflightController(
        history_path=tmp_path / "h.json" if tmp_path else None,
        notify=lambda level, text: toasts.append((level, text)),
        listen_s=0.05,
        network=lambda: NetworkFacts(30.0, 5.0, 0.0),
        face=lambda: True,
        disk=lambda: 100.0,
        **kw,
    )
    ctl.bind(lambda: snapshot, lambda action, params: acts.append((action, params)))
    return ctl, toasts, acts


def speak(ctl: PreflightController) -> threading.Thread:
    """Feed meter frames like the hub does once the prompt is listening: a quiet room, then a voice."""

    def feed() -> None:
        deadline = time.monotonic() + 5.0
        while not ctl._listening and time.monotonic() < deadline:
            time.sleep(0.001)
        for _ in range(200):
            ctl.on_meters({"mic": {"rms_db": -62.0, "peak_db": -50.0}})
        for _ in range(40):
            ctl.on_meters({"mic": {"rms_db": -20.0, "peak_db": -8.0}})

    thread = threading.Thread(target=feed)
    thread.start()
    return thread


def test_a_run_listens_judges_scores_and_tells_the_person(demo_snapshot: dict[str, Any], tmp_path: Path) -> None:
    ctl, toasts, _ = controller(demo_snapshot, tmp_path)
    assert ctl.state()["state"] == "idle" and ctl.state()["result"] is None
    ctl.act("preflight.run", {})
    ctl.listen_s = 0.4
    feeder = speak(ctl)
    st = wait_done(ctl)
    feeder.join()
    assert st["state"] == "done" and st["phase"] is None
    result = st["result"]
    assert [c["id"] for c in result["checks"]] == list(preflight.WEIGHTS)
    assert result["score"] == 100 and result["verdict"] == "ready"
    mic = next(c for c in result["checks"] if c["id"] == "mic")
    assert mic["measured"]["heard_speech"] is True and mic["measured"]["snr_db"] > 20
    assert toasts[-1][0] == "ok" and "100/100" in toasts[-1][1]
    assert st["history"][0]["score"] == 100
    json.dumps(st)


def test_no_voice_on_the_prompt_is_a_mic_warning_with_the_level_untested(demo_snapshot: dict[str, Any]) -> None:
    ctl, _, _ = controller(demo_snapshot)
    ctl.listen_s = 0.3
    ctl.act("preflight.run", {})
    deadline = time.monotonic() + 5.0
    while not ctl._listening and time.monotonic() < deadline:
        time.sleep(0.001)
    for _ in range(40):
        ctl.on_meters({"mic": {"rms_db": -62.0, "peak_db": -55.0}})
    result = wait_done(ctl)["result"]
    mic = next(c for c in result["checks"] if c["id"] == "mic")
    assert mic["state"] == "warn" and "No voice was heard" in mic["detail"]
    assert result["verdict"] == "check"


def test_listen_false_skips_the_prompt(demo_snapshot: dict[str, Any]) -> None:
    ctl, _, _ = controller(demo_snapshot)
    ctl.act("preflight.run", {"listen": False})
    result = wait_done(ctl)["result"]
    mic = next(c for c in result["checks"] if c["id"] == "mic")
    assert mic["state"] == "ok" and mic["measured"]["snr_db"] is None
    with pytest.raises(ActionError):
        ctl.act("preflight.run", {"listen": "yes"})


def test_only_one_run_at_a_time(demo_snapshot: dict[str, Any]) -> None:
    ctl, _, _ = controller(demo_snapshot)
    ctl.listen_s = 0.3
    ctl.act("preflight.run", {})
    with pytest.raises(ActionError) as err:
        ctl.act("preflight.run", {})
    assert err.value.status == 409
    wait_done(ctl)


def test_a_fact_source_that_hangs_is_left_out_and_the_run_still_ends(
    demo_snapshot: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("streamstack.console.preflight.SOURCE_TIMEOUT_S", 0.2)
    release = threading.Event()
    ctl, _, _ = controller(demo_snapshot)
    ctl._network = lambda: (release.wait(5.0), None)[1]  # a network probe that never answers in time
    started = time.monotonic()
    ctl.act("preflight.run", {"listen": False})
    result = wait_done(ctl)["result"]
    release.set()
    assert time.monotonic() - started < 3.0
    network = next(c for c in result["checks"] if c["id"] == "network")
    assert network["state"] == "skip"


def test_a_failing_source_or_check_never_leaves_the_run_hanging(
    demo_snapshot: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctl, _, _ = controller(demo_snapshot)
    ctl._face = lambda: (_ for _ in ()).throw(OSError("no detector"))
    monkeypatch.setattr(preflight, "CHECKS", (("camera", lambda facts: 1 / 0), *preflight.CHECKS[1:]))
    ctl.act("preflight.run", {"listen": False})
    result = wait_done(ctl)["result"]
    camera = result["checks"][0]
    assert camera["state"] == "warn" and "failed to run" in camera["detail"]
    assert len(result["checks"]) == 7


def test_a_broken_snapshot_ends_the_run_with_an_error_toast(demo_snapshot: dict[str, Any]) -> None:
    ctl, _, _ = controller(demo_snapshot)
    ctl.bind(lambda: (_ for _ in ()).throw(RuntimeError("boom")), lambda a, p: None)
    ctl.act("preflight.run", {"listen": False})
    st = wait_done(ctl)
    assert st["state"] == "done"  # the facts were left out; every check judged what it could
    assert any(c["state"] == "fail" for c in st["result"]["checks"]) or st["result"]["score"] < 100


def test_fix_runs_the_checks_own_action_then_rejudges(demo_snapshot: dict[str, Any]) -> None:
    snap = json.loads(json.dumps(demo_snapshot))
    snap["lighting"]["lights"][0]["power"] = False
    ctl, _, acts = controller(snap)
    ctl.act("preflight.run", {"listen": False})
    result = wait_done(ctl)["result"]
    lights = next(c for c in result["checks"] if c["id"] == "lights")
    assert lights["state"] == "warn" and lights["fix"]["action"] == "lighting.power"

    def fix(action: str, params: dict) -> None:
        acts.append((action, params))
        snap["lighting"]["lights"][0]["power"] = True

    ctl.bind(lambda: snap, fix)
    message = ctl.act("preflight.fix", {"check": "lights"})
    assert acts[-1][0] == "lighting.power" and acts[-1][1]["on"] is True and message
    st = ctl.state()
    assert next(c for c in st["result"]["checks"] if c["id"] == "lights")["state"] == "ok"
    assert st["result"]["score"] == 100 and st["history"][0]["score"] == 100


def test_fix_refuses_unknown_missing_and_unfixable(demo_snapshot: dict[str, Any]) -> None:
    ctl, _, acts = controller(demo_snapshot)
    with pytest.raises(ActionError) as err:
        ctl.act("preflight.fix", {"check": "lights"})
    assert err.value.status == 409  # nothing has run yet
    ctl.act("preflight.run", {"listen": False})
    wait_done(ctl)
    for bad, status in (({"check": "nope"}, 400), ({}, 400), ({"check": "camera"}, 409)):
        with pytest.raises(ActionError) as err:
            ctl.act("preflight.fix", bad)
        assert err.value.status == status
    assert acts == []  # nothing was changed


def test_a_fix_outside_the_allowed_actions_is_never_run(demo_snapshot: dict[str, Any]) -> None:
    ctl, _, acts = controller(demo_snapshot)
    ctl.act("preflight.run", {"listen": False})
    wait_done(ctl)
    ctl._result["checks"][0]["fix"] = {"action": "studio.stop", "params": {}, "label": "Stop"}  # type: ignore[index]
    with pytest.raises(ActionError):
        ctl.act("preflight.fix", {"check": "camera"})
    assert acts == []


def test_history_keeps_the_last_30_runs_and_survives_a_restart(demo_snapshot: dict[str, Any], tmp_path: Path) -> None:
    ctl, _, _ = controller(demo_snapshot, tmp_path)
    for _ in range(HISTORY_MAX + 3):
        ctl.act("preflight.run", {"listen": False})
        wait_done(ctl)
    assert len(ctl.state()["history"]) == HISTORY_MAX
    again, _, _ = controller(demo_snapshot, tmp_path)
    runs = again.state()["history"]
    assert len(runs) == HISTORY_MAX and set(runs[0]) == {"at", "score", "verdict", "checks"}
    (tmp_path / "h.json").write_text('{"runs": 3}')
    assert controller(demo_snapshot, tmp_path)[0].state()["history"] == []
    (tmp_path / "h.json").write_text('{"runs": [{"score": "x"}, 5, {"score": 80, "verdict": "check"}]}')
    assert [r["score"] for r in controller(demo_snapshot, tmp_path)[0].state()["history"]] == [80]


def test_a_run_reports_done_only_once_its_history_is_on_disk(
    demo_snapshot: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = filestore.write_json

    def slow_write(path: Path, data: Any) -> None:
        time.sleep(0.2)  # a busy disk: anything that sees "done" must still find the run saved (#166)
        write(path, data)

    monkeypatch.setattr(filestore, "write_json", slow_write)
    ctl, _, _ = controller(demo_snapshot, tmp_path)
    ctl.act("preflight.run", {"listen": False})
    wait_done(ctl)
    assert len(json.loads((tmp_path / "h.json").read_text())["runs"]) == 1


def test_a_slow_history_save_never_lands_after_a_newer_one(
    demo_snapshot: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctl, _, _ = controller(demo_snapshot, tmp_path)
    write, release = filestore.write_json, threading.Event()
    stalled: list[int] = []

    def stall_first(path: Path, data: Any) -> None:
        if not stalled:
            stalled.append(len(data["runs"]))
            release.wait(5)  # the older save is preempted mid-write (#166)
        write(path, data)

    monkeypatch.setattr(filestore, "write_json", stall_first)
    ctl._history = [{"at": "a", "score": 100, "verdict": "ready", "checks": {}}]
    older = threading.Thread(target=ctl._save_history)
    older.start()
    while not stalled:
        time.sleep(0.01)
    ctl._history = [*ctl._history, {"at": "b", "score": 90, "verdict": "ready", "checks": {}}]
    newer = threading.Thread(target=ctl._save_history)
    newer.start()
    newer.join(0.3)  # without the save lock the newer save finishes here, before the older one
    release.set()
    older.join(5)
    newer.join(5)
    assert [r["at"] for r in json.loads((tmp_path / "h.json").read_text())["runs"]] == ["a", "b"]


def test_unknown_action(demo_snapshot: dict[str, Any]) -> None:
    ctl, _, _ = controller(demo_snapshot)
    with pytest.raises(ActionError):
        ctl.act("preflight.nope", {})


def test_empty_state_shape_matches_a_live_one(demo_snapshot: dict[str, Any]) -> None:
    ctl, _, _ = controller(demo_snapshot)
    assert set(empty_state()) == set(ctl.state()) and empty_state()["available"] is False


# ---------------------------------------------------------------------------
# the demo twin: each injected fault lowers exactly its check
# ---------------------------------------------------------------------------


class KwRig:
    """The rig with the e2e helper's calling style: `act("audio.mute", on=True)`."""

    def __init__(self, rig: Rig) -> None:
        self.rig = rig

    def act(self, action: str, **params: Any) -> str | None:
        return self.rig.act(action, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.rig, name)


@pytest.fixture
def demo_rig() -> Any:
    rig = Rig.demo()
    rig.start()
    try:
        yield KwRig(rig)
    finally:
        rig.stop()


def run_demo(rig: Any) -> dict[str, Any]:
    rig.act("preflight.run")
    return wait_done(rig.preflight)["result"]


def test_healthy_demo_rig_scores_100(demo_rig: Any) -> None:
    result = run_demo(demo_rig)
    assert result["score"] == 100 and result["verdict"] == "ready", result["text"]


@pytest.mark.parametrize(
    ("setup", "check_id", "fix_action"),
    [
        (lambda r: r.act("audio.demo_stall", on=True), "mic", "audio.revive"),
        (lambda r: r.act("preflight.demo_fault", fault="noise_floor"), "mic", None),
        (lambda r: r.act("preflight.demo_fault", fault="no_voice"), "mic", None),
        (lambda r: r.act("preflight.demo_fault", fault="clipping"), "mic", "audio.clip_clear"),
        (lambda r: r.act("preflight.demo_fault", fault="no_face"), "camera", None),
        (lambda r: r.act("preflight.demo_fault", fault="jitter"), "network", None),
        (lambda r: r.act("preflight.demo_fault", fault="gpu_busy"), "headroom", None),
        (
            lambda r: r.act("audio.output_default", name="alsa_output.demo-monitor.hdmi-stereo"),
            "outputs",
            "audio.output_default",
        ),
        (lambda r: r.act("lighting.power", on=False), "lights", "lighting.power"),
    ],
)
def test_each_injected_fault_lowers_exactly_its_check_and_offers_its_fix(
    demo_rig: Any, setup: Any, check_id: str, fix_action: str | None
) -> None:
    setup(demo_rig)
    result = run_demo(demo_rig)
    states = {c["id"]: c for c in result["checks"]}
    assert states[check_id]["state"] in ("warn", "fail"), states[check_id]
    assert [i for i, c in states.items() if c["state"] in ("warn", "fail")] == [check_id]
    assert result["score"] < 100
    assert (states[check_id]["fix"] or {}).get("action") == fix_action


def test_the_one_press_fix_clears_the_fault(demo_rig: Any) -> None:
    demo_rig.act("lighting.power", on=False)
    assert run_demo(demo_rig)["score"] < 100
    demo_rig.act("preflight.fix", check="lights")
    st = demo_rig.preflight.state()
    assert st["result"]["score"] == 100
    assert all(light["power"] for light in demo_rig.snapshot()["lighting"]["lights"])


def test_demo_faults_validate_and_reset(demo_rig: Any) -> None:
    with pytest.raises(ActionError):
        demo_rig.act("preflight.demo_fault", fault="meteor")
    demo_rig.act("preflight.demo_fault", fault="jitter")
    assert demo_rig.preflight.state()["faults"] == ["jitter"]
    demo_rig.act("preflight.demo_fault", fault="jitter", on=False)
    demo_rig.act("preflight.demo_fault", fault="no_face")
    run_demo(demo_rig)
    demo_rig.act("preflight.demo_reset")
    st = demo_rig.preflight.state()
    assert st["faults"] == [] and st["result"] is None and st["history"] == [] and st["demo"] is True
    assert isinstance(demo_rig.preflight, DemoPreflight)


def test_the_rig_without_preflight_reports_an_empty_object_and_refuses_the_actions() -> None:
    rig = Rig.demo()
    plain = Rig(rig.audio, rig.video, rig.studio)
    assert plain.snapshot()["preflight"] == empty_state()
    with pytest.raises(ActionError) as err:
        plain.act("preflight.run", {})
    assert err.value.status == 409
