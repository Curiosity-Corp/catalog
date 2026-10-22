"""Console video: OBS state, looks, privacy, frames, reconnects and the demo rig, all against a fake OBS."""

from __future__ import annotations

import base64
import json
import os
import queue
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from streamstack import auto, config
from streamstack.console import video
from streamstack.console.errors import ActionError
from streamstack.obs import COLOR_FILTER, LUT_FILTER, OBS, SHARPEN_FILTER

JPEG = b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"


def wait_until(check: Callable[[], object], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached in time")


class Rig:
    """The fake OBS behind every connection: scenes, filters, outputs, an event queue and a call log."""

    def __init__(self) -> None:
        self.scenes = {
            "Canon": ["Canon EOS 50D"],
            "Cam Link": ["Cam Link 4K"],
            "Side by Side": ["Canon EOS 50D", "Cam Link 4K", "Logo"],
            "Be Right Back": ["Card"],
        }
        self.program = "Canon"
        self.filters: dict[str, dict[str, list]] = {
            name: {
                COLOR_FILTER: [True, {"brightness": 0.0, "contrast": 0.0, "gamma": 0.0, "saturation": 0.0}],
                LUT_FILTER: [False, {"clut_amount": 0.35}],
                SHARPEN_FILTER: [False, {"sharpness": 0.08}],
            }
            for name in ("Canon EOS 50D", "Cam Link 4K")
        }
        self.vcam = False
        self.recording = False
        self.streaming = False
        self.stream_key = "live_key"
        self.calls: list[tuple[str, dict]] = []
        self.events: queue.Queue[dict] = queue.Queue()
        self.lock = threading.Lock()
        self.connections = 0
        self.down: Exception | None = None
        self.shot_widths: list[int] = []

    def connector(self, events: int = 0) -> OBS:
        if self.down:
            raise self.down
        self.connections += 1
        return FakeOBS(self, events)


class FakeOBS(OBS):
    def __init__(self, rig: Rig, events: int) -> None:  # no socket
        self.rig = rig
        self.events = events
        self.lock = threading.RLock()
        self.closed = False
        self.broken: Exception | None = None

    def close(self) -> None:
        self.closed = True

    def wait_event(self, timeout: float) -> dict | None:
        if self.broken:
            raise self.broken
        try:
            return self.rig.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def call(self, request: str, **data) -> dict:
        rig = self.rig
        if self.broken:
            raise self.broken
        with rig.lock:
            rig.calls.append((request, data))
            if request == "GetSceneList":
                return {
                    "currentProgramSceneName": rig.program,
                    "scenes": [{"sceneName": n, "sceneIndex": len(rig.scenes) - i} for i, n in enumerate(rig.scenes)],
                }
            if request == "SetCurrentProgramScene":
                rig.program = data["sceneName"]
                return {}
            if request == "GetSceneItemList":
                return {"sceneItems": [{"sourceName": n} for n in rig.scenes[data["sceneName"]]]}
            if request == "GetSourceFilter":
                found = rig.filters.get(data["sourceName"], {}).get(data["filterName"])
                if found is None:
                    raise RuntimeError("GetSourceFilter: no such filter")
                return {"filterEnabled": found[0], "filterSettings": dict(found[1])}
            if request == "SetSourceFilterSettings":
                rig.filters[data["sourceName"]][data["filterName"]][1].update(data["filterSettings"])
                return {}
            if request == "SetSourceFilterEnabled":
                rig.filters[data["sourceName"]][data["filterName"]][0] = data["filterEnabled"]
                return {}
            if request == "GetVirtualCamStatus":
                return {"outputActive": rig.vcam}
            if request == "StartVirtualCam":
                rig.vcam = True
                return {}
            if request == "StopVirtualCam":
                rig.vcam = False
                return {}
            if request == "GetRecordStatus":
                return {"outputActive": rig.recording, "outputPaused": False, "outputTimecode": "00:01:02.345"}
            if request in ("StartRecord", "StopRecord"):
                rig.recording = request == "StartRecord"
                return {}
            if request == "GetStreamStatus":
                return {"outputActive": rig.streaming, "outputTimecode": "00:00:09.100"}
            if request == "GetStreamServiceSettings":
                return {"streamServiceSettings": {"key": rig.stream_key}}
            if request in ("StartStream", "StopStream"):
                rig.streaming = request == "StartStream"
                return {}
            if request == "GetSourceScreenshot":
                rig.shot_widths.append(data["imageWidth"])
                return {"imageData": "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()}
        raise RuntimeError(f"{request}: unsupported")


class Toasts:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def __call__(self, level: str, text: str) -> None:
        self.items.append((level, text))

    def texts(self) -> list[str]:
        return [t for _, t in self.items]


@pytest.fixture
def rig() -> Rig:
    return Rig()


@pytest.fixture
def toasts() -> Toasts:
    return Toasts()


@pytest.fixture
def ctl(rig: Rig, toasts: Toasts, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(video, "BACKOFF", (0.01,))
    monkeypatch.setattr(video, "POLL_SECONDS", 0.05)
    controller = video.VideoController(dict(config.DEFAULTS), toasts, connector=rig.connector)
    controller.start()
    wait_until(lambda: controller.state()["obs"] == "connected" and controller.state()["scenes"])
    yield controller
    controller.stop()


def written(rig: Rig, request: str) -> list[dict]:
    return [d for r, d in rig.calls if r == request]


# -- state ------------------------------------------------------------------


def test_state_assembly(ctl, rig):
    state = ctl.state()
    assert state["obs"] == "connected" and state["obs_error"] is None
    assert [s["name"] for s in state["scenes"]] == ["Canon", "Cam Link", "Side by Side", "Be Right Back"]
    assert state["program_scene"] == "Canon"
    assert state["scenes"][0]["active"] is True
    assert state["privacy"] is False and state["privacy_scene"] == "Be Right Back"
    assert state["sources"] == ["Canon EOS 50D", "Cam Link 4K"]  # de-duplicated, "Logo"/"Card" have no filter
    assert state["selected_source"] == "Canon EOS 50D"
    assert state["look"]["warmth"] == 0.0 and state["look"]["sharpen_enabled"] is False
    assert state["limits"]["brightness"] == [-0.5, 0.5]
    assert [lk["name"] for lk in state["looks"][:2]] == ["Natural", "Warm Studio"]
    assert state["recording"] == {"active": False, "paused": False, "timecode": None}
    assert set(state) >= {"virtualcam", "streaming", "program_scene", "sources", "look", "limits", "looks"}


def test_state_is_a_copy(ctl):
    first = ctl.state()
    first["scenes"].clear()
    assert ctl.state()["scenes"]


def test_selected_source_follows_program_scene(ctl, rig):
    rig.program = "Cam Link"
    rig.events.put({"eventType": "CurrentProgramSceneChanged"})
    wait_until(lambda: ctl.state()["selected_source"] == "Cam Link 4K")


def test_events_update_outputs_quickly(ctl, rig):
    rig.recording = True
    rig.events.put({"eventType": "RecordStateChanged"})
    wait_until(lambda: ctl.state()["recording"]["active"])
    assert ctl.state()["recording"]["timecode"] == "00:01:02"


def test_polling_fallback_without_events(ctl, rig):
    rig.vcam = True  # no event queued: the 1 s poll (50 ms here) still notices
    wait_until(lambda: ctl.state()["virtualcam"])


def test_privacy_scene_fallback_names():
    assert video.pick_privacy_scene(["A", "Be Right Back"]) == "Be Right Back"
    assert video.pick_privacy_scene(["A", "Stream BRB"]) == "Stream BRB"
    assert video.pick_privacy_scene(["A", "Privacy screen"]) == "Privacy screen"
    assert video.pick_privacy_scene(["A", "B"]) is None


def test_no_privacy_scene_is_409(ctl, rig):
    del rig.scenes["Be Right Back"]
    rig.events.put({"eventType": "SceneRemoved"})
    wait_until(lambda: ctl.state()["privacy_scene"] is None)
    with pytest.raises(ActionError) as err:
        ctl.act("video.privacy", {"on": True})
    assert err.value.status == 409


# -- scenes and privacy -----------------------------------------------------


def test_scene_switch_and_validation(ctl, rig):
    ctl.act("video.scene", {"name": "Cam Link"})
    assert rig.program == "Cam Link" and ctl.state()["program_scene"] == "Cam Link"
    with pytest.raises(ActionError) as err:
        ctl.act("video.scene", {"name": "Nope"})
    assert err.value.status == 400
    with pytest.raises(ActionError):
        ctl.act("video.scene", {})
    with pytest.raises(ActionError):
        ctl.act("video.bogus", {})


def test_privacy_remembers_and_restores(ctl, rig, toasts):
    ctl.act("video.scene", {"name": "Side by Side"})
    ctl.act("video.privacy", {"on": True})
    assert rig.program == "Be Right Back" and ctl.state()["privacy"] is True
    ctl.act("video.privacy", {"on": False})
    assert rig.program == "Side by Side" and ctl.state()["privacy"] is False
    assert any("Privacy" in t for t in toasts.texts())


def test_privacy_off_after_manual_switch_only_clears_flag(ctl, rig):
    ctl.act("video.privacy", {"on": True})
    rig.program = "Cam Link"  # the user clicked a scene in OBS meanwhile
    rig.events.put({"eventType": "CurrentProgramSceneChanged"})
    wait_until(lambda: ctl.state()["privacy"] is False)
    switches = len(written(rig, "SetCurrentProgramScene"))
    ctl.act("video.privacy", {"on": False})
    assert rig.program == "Cam Link"
    assert len(written(rig, "SetCurrentProgramScene")) == switches


def test_privacy_needs_a_bool(ctl):
    with pytest.raises(ActionError) as err:
        ctl.act("video.privacy", {"on": "yes"})
    assert err.value.status == 400


# -- the look ---------------------------------------------------------------


def test_look_validation():
    assert video.validate_look({"brightness": 0.1, "sharpen_enabled": True}) == {
        "brightness": 0.1,
        "sharpen_enabled": True,
    }
    for bad in (
        None,
        {},
        {"brightness": 0.9},
        {"contrast": "high"},
        {"contrast": True},
        {"contrast": float("nan")},
        {"sharpen_enabled": 1},
        {"lut_amount": 1.5},
        {"colour": 0.1},
    ):
        with pytest.raises(ActionError) as err:
            video.validate_look(bad)
        assert err.value.status == 400


def test_look_writes_warmth_as_color_multiply(ctl, rig):
    ctl.act("video.look", {"values": {"warmth": 0.5, "brightness": 0.1}})
    wait_until(lambda: rig.filters["Canon EOS 50D"][COLOR_FILTER][1].get("color_multiply"))
    settings = rig.filters["Canon EOS 50D"][COLOR_FILTER][1]
    assert settings["color_multiply"] == auto.warmth_to_multiply(0.5)
    assert settings["brightness"] == 0.1 and "warmth" not in settings
    assert ctl.state()["look"]["warmth"] == 0.5  # optimistic state is immediate


def test_look_flags_and_amounts(ctl, rig):
    ctl.act(
        "video.look",
        {"values": {"lut_enabled": True, "lut_amount": 0.4, "sharpen_enabled": True, "sharpen_amount": 0.3}},
    )
    wait_until(lambda: rig.filters["Canon EOS 50D"][SHARPEN_FILTER][0])
    wait_until(lambda: rig.filters["Canon EOS 50D"][LUT_FILTER][0])
    assert rig.filters["Canon EOS 50D"][SHARPEN_FILTER][1]["sharpness"] == 0.3
    assert rig.filters["Canon EOS 50D"][LUT_FILTER][1]["clut_amount"] == 0.4


def test_look_on_another_source_and_unknown_source(ctl, rig):
    ctl.act("video.look", {"values": {"gamma": 0.2}, "source": "Cam Link 4K"})
    wait_until(lambda: rig.filters["Cam Link 4K"][COLOR_FILTER][1]["gamma"] == 0.2)
    with pytest.raises(ActionError) as err:
        ctl.act("video.look", {"values": {"gamma": 0.2}, "source": "Logo"})
    assert err.value.status == 400


def test_look_bursts_coalesce_latest_wins(ctl, rig):
    gate = threading.Event()
    real = FakeOBS.call

    def slow(self, request, **data):
        if request == "SetSourceFilterSettings":
            gate.wait(2)
        return real(self, request, **data)

    FakeOBS.call = slow  # type: ignore[method-assign]
    try:
        for step in range(60):
            ctl.act("video.look", {"values": {"contrast": step / 100}})
        gate.set()
        wait_until(lambda: rig.filters["Canon EOS 50D"][COLOR_FILTER][1].get("contrast") == 0.59)
    finally:
        FakeOBS.call = real  # type: ignore[method-assign]
    # one write was stuck in flight, the other 59 merged into at most one more
    assert len(written(rig, "SetSourceFilterSettings")) <= 3


def test_state_look_not_clobbered_by_stale_poll(ctl, rig):
    ctl.act("video.look", {"values": {"brightness": 0.2}})
    assert ctl.state()["look"]["brightness"] == 0.2
    wait_until(lambda: rig.filters["Canon EOS 50D"][COLOR_FILTER][1]["brightness"] == 0.2)
    time.sleep(0.2)
    assert ctl.state()["look"]["brightness"] == 0.2


def test_look_source_switch_reads_that_source(ctl, rig):
    rig.filters["Cam Link 4K"][COLOR_FILTER][1]["saturation"] = 0.3
    ctl.act("video.source", {"name": "Cam Link 4K"})
    assert ctl.state()["selected_source"] == "Cam Link 4K" and ctl.state()["look"]["saturation"] == 0.3
    with pytest.raises(ActionError):
        ctl.act("video.source", {"name": "Logo"})


def test_look_reset_ramps_to_neutral(ctl, rig, monkeypatch):
    monkeypatch.setattr(video, "RESET_SECONDS", 0.1)
    settings = rig.filters["Canon EOS 50D"][COLOR_FILTER][1]
    settings.update(contrast=0.4, saturation=-0.3, color_multiply=auto.warmth_to_multiply(0.4))
    rig.filters["Canon EOS 50D"][LUT_FILTER][0] = True
    ctl.act("video.look.reset", {})
    wait_until(lambda: not rig.filters["Canon EOS 50D"][LUT_FILTER][0])
    assert settings["contrast"] == 0.0 and settings["saturation"] == 0.0
    assert auto.multiply_to_warmth(settings["color_multiply"]) == 0.0


def test_newer_look_cancels_a_ramp(ctl, rig, monkeypatch):
    monkeypatch.setattr(video, "APPLY_SECONDS", 3.0)
    ctl.act("video.looks.apply", {"name": "Punchy"})
    wait_until(lambda: written(rig, "SetSourceFilterSettings"))
    ctl.act("video.look", {"values": {"contrast": -0.5}})
    wait_until(lambda: rig.filters["Canon EOS 50D"][COLOR_FILTER][1]["contrast"] == -0.5)
    time.sleep(0.4)
    assert rig.filters["Canon EOS 50D"][COLOR_FILTER][1]["contrast"] == -0.5  # the ramp did not resume
    assert not rig.filters["Canon EOS 50D"][SHARPEN_FILTER][0]  # and never reached its final switches


def test_apply_look_sets_everything(ctl, rig, monkeypatch):
    monkeypatch.setattr(video, "APPLY_SECONDS", 0.1)
    ctl.act("video.looks.apply", {"name": "Cinematic"})
    wait_until(lambda: rig.filters["Canon EOS 50D"][LUT_FILTER][0])
    assert rig.filters["Canon EOS 50D"][LUT_FILTER][1]["clut_amount"] == 0.4
    assert rig.filters["Canon EOS 50D"][COLOR_FILTER][1]["contrast"] == 0.12
    with pytest.raises(ActionError) as err:
        ctl.act("video.looks.apply", {"name": "Missing"})
    assert err.value.status == 400


# -- the library ------------------------------------------------------------


def test_builtin_looks_are_complete_and_in_range():
    assert list(video.BUILTIN_LOOKS) == ["Natural", "Warm Studio", "Cool Daylight", "Cinematic", "Punchy", "Soft Skin"]
    for look in video.BUILTIN_LOOKS.values():
        assert set(look) == set(video.NEUTRAL)
        assert video.validate_look(look) == look
    assert video.BUILTIN_LOOKS["Natural"] == video.NEUTRAL
    assert video.BUILTIN_LOOKS["Cinematic"]["lut_enabled"] is True


def test_save_and_delete_looks(ctl, rig, isolated_home):
    ctl.act("video.look", {"values": {"contrast": 0.3, "warmth": 0.2}})
    ctl.act("video.looks.save", {"name": "Evening"})
    path = isolated_home / ".config/streamstack/looks.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["Evening"]["contrast"] == 0.3
    assert not list(path.parent.glob("*.tmp"))
    listing = ctl.state()["looks"]
    assert listing[-1]["name"] == "Evening" and listing[-1]["builtin"] is False
    ctl.act("video.looks.delete", {"name": "Evening"})
    assert "Evening" not in [lk["name"] for lk in ctl.state()["looks"]]


def test_looks_name_rules(ctl):
    for bad, status in (("Natural", 400), ("natural", 400), ("x" * 41, 400), ("   ", 400), ("bad\nname", 400)):
        with pytest.raises(ActionError) as err:
            ctl.act("video.looks.save", {"name": bad})
        assert err.value.status == status
    ctl.act("video.looks.save", {"name": "x" * 40})
    for bad in ("Natural", "Ghost"):
        with pytest.raises(ActionError) as err:
            ctl.act("video.looks.delete", {"name": bad})
        assert err.value.status == 400


def test_corrupt_looks_file_is_ignored(isolated_home):
    path = isolated_home / ".config/streamstack/looks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"Good": {"contrast": 0.1}, "Bad": {"contrast": 9}, "Worse": 3}))
    assert [lk["name"] for lk in video.LookLibrary().listing()][-1] == "Good"
    path.write_text("{not json")
    assert len(video.LookLibrary().listing()) == len(video.BUILTIN_LOOKS)


# -- outputs ----------------------------------------------------------------


def test_outputs_toggle(ctl, rig, toasts):
    ctl.act("video.virtualcam", {"on": True})
    ctl.act("video.record", {"on": True})
    ctl.act("video.stream", {"on": True})
    assert rig.vcam and rig.recording and rig.streaming
    assert ctl.state()["recording"]["active"] and ctl.state()["streaming"]["active"]
    assert "Recording started" in toasts.texts()
    assert ctl.act("video.record", {"on": True}) == "Recording is already on."
    ctl.act("video.record", {"on": False})
    ctl.act("video.stream", {"on": False})
    ctl.act("video.virtualcam", {"on": False})
    assert not (rig.vcam or rig.recording or rig.streaming)


def test_stream_without_key_is_409(ctl, rig):
    rig.stream_key = ""
    with pytest.raises(ActionError) as err:
        ctl.act("video.stream", {"on": True})
    assert err.value.status == 409 and "stream key" in err.value.message
    assert not written(rig, "StartStream")


def test_obs_refusal_becomes_409(ctl, rig, monkeypatch):
    real = FakeOBS.call

    def refuse(self, request, **data):
        if request == "StartRecord":
            raise RuntimeError("StartRecord: output busy")
        return real(self, request, **data)

    monkeypatch.setattr(FakeOBS, "call", refuse)
    with pytest.raises(ActionError) as err:
        ctl.act("video.record", {"on": True})
    assert err.value.status == 409 and "output busy" in err.value.message


# -- connection state -------------------------------------------------------


def test_actions_without_obs_are_409(rig):
    controller = video.VideoController(dict(config.DEFAULTS), connector=rig.connector)
    for action, params in (
        ("video.scene", {"name": "Canon"}),
        ("video.privacy", {"on": True}),
        ("video.look", {"values": {"gamma": 0.1}}),
        ("video.record", {"on": True}),
    ):
        with pytest.raises(ActionError) as err:
            controller.act(action, params)
        assert err.value.status == 409
    state = controller.state()
    assert state["obs"] == "offline" and state["scenes"] == [] and state["look"]["gamma"] == 0.0
    assert controller.frame("program", 640) is None


def test_reconnect_state_transitions(rig, toasts, monkeypatch):
    monkeypatch.setattr(video, "BACKOFF", (0.01,))
    monkeypatch.setattr(video, "POLL_SECONDS", 0.05)
    rig.down = video.ObsUnavailable("offline")
    controller = video.VideoController(dict(config.DEFAULTS), toasts, connector=rig.connector)
    controller.start()
    try:
        wait_until(lambda: controller.state()["obs"] == "offline")
        rig.down = video.ObsUnavailable("no-websocket", "turn the websocket on")
        wait_until(lambda: controller.state()["obs"] == "no-websocket")
        assert controller.state()["obs_error"] == "turn the websocket on"
        rig.down = video.ObsUnavailable("error", "login refused")
        wait_until(lambda: controller.state()["obs"] == "error")
        rig.down = None
        wait_until(lambda: controller.state()["obs"] == "connected" and controller.state()["scenes"])
        assert controller.state()["obs_error"] is None
    finally:
        controller.stop()
    assert "OBS connected" in toasts.texts()


def test_socket_loss_then_recovery(rig, toasts, monkeypatch):
    monkeypatch.setattr(video, "BACKOFF", (0.01,))
    monkeypatch.setattr(video, "POLL_SECONDS", 0.05)
    created: list[FakeOBS] = []

    def connector(events: int = 0) -> OBS:
        conn = rig.connector(events)
        created.append(conn)  # type: ignore[arg-type]
        return conn

    controller = video.VideoController(dict(config.DEFAULTS), toasts, connector=connector)
    controller.start()
    try:
        wait_until(lambda: controller.state()["obs"] == "connected" and controller.state()["scenes"])
        rig.down = video.ObsUnavailable("offline")
        created[0].broken = ConnectionResetError("reset")
        wait_until(lambda: controller.state()["obs"] == "offline")
        assert controller.state()["scenes"] == []
        assert "OBS disconnected" in toasts.texts()
        rig.down = None
        wait_until(lambda: controller.state()["obs"] == "connected" and controller.state()["scenes"])
    finally:
        controller.stop()


def test_control_connection_reconnects_once(ctl, rig):
    ctl.act("video.scene", {"name": "Cam Link"})
    opened = rig.connections
    ctl._conns["control"].broken = BrokenPipeError("pipe")  # type: ignore[attr-defined]
    ctl.act("video.scene", {"name": "Canon"})  # first try hits the dead socket, the retry opens a new one
    assert rig.program == "Canon" and rig.connections == opened + 1


def test_default_connector_classification(monkeypatch, isolated_home):
    monkeypatch.setattr(video.system, "running", lambda name: False)
    with pytest.raises(video.ObsUnavailable) as err:
        video.connect_obs()
    assert err.value.state == "offline"
    monkeypatch.setattr(video.system, "running", lambda name: True)
    with pytest.raises(video.ObsUnavailable) as err:
        video.connect_obs()  # no websocket config at all
    assert err.value.state == "no-websocket" and "streamstack studio" in err.value.message
    conf = isolated_home / ".config/obs-studio/plugin_config/obs-websocket/config.json"
    conf.parent.mkdir(parents=True)
    conf.write_text(json.dumps({"server_enabled": False}))
    with pytest.raises(video.ObsUnavailable) as err:
        video.connect_obs()
    assert err.value.state == "no-websocket"


# -- frames -----------------------------------------------------------------


def test_frame_from_program_and_source(ctl, rig):
    assert ctl.frame("program", 640) == JPEG
    assert rig.shot_widths == [640]
    shots = [d for r, d in rig.calls if r == "GetSourceScreenshot"]
    assert shots[0]["sourceName"] == "Canon"
    assert ctl.frame("Cam Link 4K", 100) == JPEG and rig.shot_widths[-1] == 160  # clamped up
    assert ctl.frame("Cam Link 4K", 99999) == JPEG and rig.shot_widths[-1] == 1280
    assert ctl.frame("No Such Source", 640) is None


def test_frame_cache_shared_between_viewers(ctl, rig, monkeypatch):
    monkeypatch.setattr(video, "FRAME_TTL", 5.0)
    results = []
    threads = [threading.Thread(target=lambda: results.append(ctl.frame("program", 640))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [JPEG] * 6 and len(rig.shot_widths) == 1
    ctl.frame("program", 320)
    assert len(rig.shot_widths) == 2  # a different width is a different cache entry


def test_frame_cache_expires(ctl, rig, monkeypatch):
    monkeypatch.setattr(video, "FRAME_TTL", 0.01)
    ctl.frame("program", 640)
    time.sleep(0.03)
    ctl.frame("program", 640)
    assert len(rig.shot_widths) == 2


def test_frame_failure_returns_none(ctl, rig, monkeypatch):
    real = FakeOBS.call

    def fail(self, request, **data):
        if request == "GetSourceScreenshot":
            raise RuntimeError("no render")
        return real(self, request, **data)

    monkeypatch.setattr(FakeOBS, "call", fail)
    assert ctl.frame("program", 640) is None


def test_width_clamp():
    assert [video.clamp_width(w) for w in (0, 100, 160, 640, 1280, 5000, "800", None, "x")] == [
        160,
        160,
        160,
        640,
        1280,
        1280,
        800,
        640,
        640,
    ]


def test_jpeg_splitter_handles_chunks_and_garbage():
    one, two = b"\xff\xd8\xff\xdbAAAA\xff\xd9", b"\xff\xd8\xff\xdbBB\xff\xd9"
    stream = b"junk" + one + two + b"\xff\xd8\xff\xdbpartial"
    splitter = video.JpegSplitter()
    frames: list[bytes] = []
    for i in range(0, len(stream), 3):  # arbitrary read sizes
        frames += splitter.feed(stream[i : i + 3])
    assert frames == [one, two]
    assert splitter.feed(b"\xff\xd9") == [b"\xff\xd8\xff\xdbpartial\xff\xd9"]
    assert splitter.feed(b"") == []


def test_jpeg_splitter_drops_runaway_stream(monkeypatch):
    monkeypatch.setattr(video.JpegSplitter, "MAX_BUFFER", 100)
    splitter = video.JpegSplitter()
    assert splitter.feed(b"\xff\xd8\xff" + b"x" * 500) == []
    assert len(splitter.buffer) == 0


def test_ffmpeg_argv():
    single = video.ffmpeg_camera_argv("/dev/video42", 640)
    assert single[:2] == ["ffmpeg", "-nostdin"] and "-frames:v" in single and "scale=640:-2" in single
    stream = video.ffmpeg_camera_argv("/dev/video42", 640, 5)
    assert "-frames:v" not in stream and "fps=5,scale=640:-2" in stream and stream[-1] == "-"


class FakeProc:
    def __init__(self, data: bytes) -> None:
        self.stdout = type("S", (), {})()
        self._chunks = [data[i : i + 7] for i in range(0, len(data), 7)]
        self.stdout.read = lambda n: self._chunks.pop(0) if self._chunks else b""
        self.killed = False

    def poll(self):
        return None if not self.killed and self._chunks else 0

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None) -> int:
        return 0


def test_camera_stream_shares_one_process():
    started: list[list[str]] = []

    def popen(argv, **kwargs):
        started.append(argv)
        return FakeProc(JPEG * 3)

    stream = video.CameraStream("/dev/video42", popen=popen)
    assert stream.latest(640) == JPEG
    assert len(started) == 1 and "fps=5" in started[0][started[0].index("-vf") + 1]
    stream.stop()


def test_camera_stream_missing_ffmpeg():
    def popen(argv, **kwargs):
        raise FileNotFoundError("ffmpeg")

    assert video.CameraStream("/dev/video42", popen=popen).latest(640) is None


def test_camera_frame_uses_one_shot_then_shared_stream(ctl, monkeypatch):
    grabs: list[int] = []
    monkeypatch.setattr(video.VideoController, "_grab_once", staticmethod(lambda d, w: grabs.append(w) or JPEG))
    shared: list[int] = []
    monkeypatch.setattr(ctl._camera, "latest", lambda w, wait=2.0: shared.append(w) or JPEG)
    monkeypatch.setattr(video, "FRAME_TTL", 0.0)
    assert ctl.frame("camera", 640) == JPEG
    assert grabs == [640] and not shared
    assert ctl.frame("camera", 640) == JPEG  # a second request soon after: someone is watching
    assert shared == [640]


def test_camera_falls_back_to_obs_view(ctl, rig, monkeypatch):
    monkeypatch.setattr(video.VideoController, "_grab_once", staticmethod(lambda d, w: None))
    assert ctl.frame("camera", 640) == JPEG
    assert [d["sourceName"] for r, d in rig.calls if r == "GetSourceScreenshot"] == ["Canon EOS 50D"]


# -- the demo rig -----------------------------------------------------------


@pytest.fixture
def demo(toasts) -> video.DemoVideo:
    return video.DemoVideo(toasts)


def test_demo_state_shape_matches_real(demo, ctl):
    real, fake = ctl.state(), demo.state()
    assert set(fake) == set(real)
    assert set(fake["look"]) == set(real["look"]) and fake["limits"] == real["limits"]
    assert fake["obs"] == "connected" and len(fake["scenes"]) == 4 and len(fake["sources"]) == 2


def test_demo_privacy_scene_and_frames(demo):
    cards = {demo.frame("program", 640)}
    assert all(c and c.startswith(b"\xff\xd8") for c in cards)
    demo.act("video.scene", {"name": "Cam Link"})
    cards.add(demo.frame("program", 640))
    demo.act("video.privacy", {"on": True})
    assert demo.state()["privacy"] is True and demo.state()["program_scene"] == "Be Right Back"
    cards.add(demo.frame("program", 640))
    demo.act("video.privacy", {"on": False})
    assert demo.state()["program_scene"] == "Cam Link" and demo.state()["privacy"] is False
    assert len(cards) == 3  # a different card per scene
    assert demo.frame("camera", 640) != demo.frame("Cam Link 4K", 640)
    assert demo.frame("nope", 640) is None


def test_demo_look_responds_and_validates(demo):
    demo.act("video.look", {"values": {"warmth": 0.4, "lut_enabled": True}})
    assert demo.state()["look"]["warmth"] == 0.4 and demo.state()["look"]["lut_enabled"] is True
    demo.act("video.source", {"name": "Cam Link 4K"})
    assert demo.state()["look"]["warmth"] == 0.0  # per-source looks
    with pytest.raises(ActionError):
        demo.act("video.look", {"values": {"warmth": 5}})
    demo.act("video.looks.apply", {"name": "Punchy"})
    assert demo.state()["look"]["sharpen_enabled"] is True
    demo.act("video.look.reset", {})
    assert demo.state()["look"] == video.NEUTRAL


def test_demo_library_and_outputs(demo, toasts):
    demo.act("video.looks.save", {"name": "Mine"})
    assert demo.state()["looks"][-1]["name"] == "Mine"
    with pytest.raises(ActionError):
        demo.act("video.looks.save", {"name": "Punchy"})
    demo.act("video.looks.delete", {"name": "Mine"})
    demo.act("video.record", {"on": True})
    assert demo.state()["recording"]["active"] and demo.state()["recording"]["timecode"] == "00:00:00"
    demo.act("video.stream", {"on": True})
    demo.act("video.virtualcam", {"on": False})
    assert demo.state()["virtualcam"] is False and demo.state()["streaming"]["active"]
    demo.act("video.record", {"on": False})
    assert demo.state()["recording"] == {"active": False, "paused": False, "timecode": None}
    assert "Recording started" in toasts.texts()
    with pytest.raises(ActionError):
        demo.act("video.unknown", {})


def test_demo_cards_ship_and_are_small():
    folder = config.data_dir() / "console/demo"
    files = sorted(folder.glob("*.jpg"))
    assert len(files) >= 3
    for file in files:
        data = file.read_bytes()
        assert data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9") and len(data) < 150_000
    assert {name for name in video.DEMO_CARDS.values()} <= {f.name for f in files}


def test_looks_file_atomic_write_leaves_no_partial(isolated_home, monkeypatch):
    library = video.LookLibrary()
    library.save("One", dict(video.NEUTRAL))
    before = library.file().read_text()

    def explode(*args, **kwargs):
        raise OSError(28, "No space left")

    monkeypatch.setattr(os, "fsync", explode)
    with pytest.raises(ActionError) as err:
        library.save("Two", dict(video.NEUTRAL))
    assert err.value.status == 409
    assert library.file().read_text() == before  # the old file is untouched


def test_data_files_are_packaged(tmp_path: Path):
    assert (config.data_dir() / "console/demo/card-brb.jpg").is_file()
