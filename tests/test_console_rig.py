"""The real demo controllers composed by Rig.demo(): the contract between rig, hub and controllers."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from streamstack import store
from streamstack.console.errors import ActionError
from streamstack.console.rig import Rig

TOP_LEVEL = {
    "version", "demo", "studio", "video", "audio", "auto", "health", "system", "lighting", "interface", "framing",
    "virtual",
    "presets",
    "hardware",
    "stream",
    "storage",
    "deck",
    "slots",
    "hotkeys",
    "meetings",
    "diagnostics",
    "call",
    "network",
    "echo",
    "eyeline",
    "preflight",
    "safejoin",
}  # fmt: skip


@pytest.fixture
def rig() -> Iterator[Rig]:
    rig = Rig.demo()
    rig.start()
    yield rig
    rig.stop()


def test_snapshot_has_every_contract_section(rig: Rig) -> None:
    snapshot = rig.snapshot()
    assert set(snapshot) == TOP_LEVEL
    assert snapshot["demo"] is True
    assert snapshot["video"]["scenes"]
    assert snapshot["audio"]["chain"]["presets"]
    assert snapshot["health"]["checks"]


def test_meters_frame_shape(rig: Rig) -> None:
    frame = rig.meters()
    assert {"t", "mic", "voice"} <= set(frame)
    assert -90.0 <= frame["mic"]["peak_db"] <= 0.0
    for source in ("mic", "voice"):
        assert {"peak_db", "sample_db", "rms_db", "clip", "latched", "lufs_m", "lufs_s"} <= set(frame[source])
    assert "gr_db" in frame["voice"] and "gr_db" not in frame["mic"]


@pytest.mark.parametrize(
    ("action", "params", "check"),
    [
        ("audio.mute", {"on": True}, lambda s: s["audio"]["mic"]["muted"] is True),
        ("audio.gain", {"db": -6.0}, lambda s: s["audio"]["mic"]["gain_db"] == -6.0),
        ("audio.bypass", {"on": True}, lambda s: s["audio"]["chain"]["bypass"] is True),
        ("video.privacy", {"on": True}, lambda s: s["video"]["privacy"] is True),
        ("video.look", {"values": {"brightness": 0.1}}, lambda s: s["video"]["look"]["brightness"] == 0.1),
        ("auto.on", {}, lambda s: s["auto"]["running"] is True),
    ],
)
def test_actions_route_to_their_controller(rig: Rig, action: str, params: dict, check) -> None:
    rig.act(action, params)
    deadline = time.monotonic() + 3
    while not check(rig.snapshot()):
        assert time.monotonic() < deadline, f"{action} did not change the state"
        time.sleep(0.05)


def test_every_controller_can_raise_a_toast(rig: Rig) -> None:
    """Each controller's notify must reach the hub's toast hook, whatever it names its callback internally."""
    toasts: list[tuple[str, str]] = []
    rig.on_toast = lambda level, text: toasts.append((level, text))
    for controller in (rig.audio, rig.video, rig.studio):
        controller.notify("info", type(controller).__name__)
    assert [text for _level, text in toasts] == ["DemoAudio", "DemoVideo", "DemoStudio"]


def test_demo_mic_check_toasts_when_ready(rig: Rig) -> None:
    toasts: list[str] = []
    rig.on_toast = lambda _level, text: toasts.append(text)
    rig.act("audio.mic_check", {"seconds": 2})
    deadline = time.monotonic() + 15
    while rig.snapshot()["audio"]["mic_check"]["state"] != "done":
        assert time.monotonic() < deadline, "the demo mic check never finished"
        time.sleep(0.1)
    assert any("Mic check" in text for text in toasts)
    clip = rig.snapshot()["audio"]["mic_check"]["clips"][0]
    path = rig.clip_path(clip["id"])
    assert path is not None and path.read_bytes()[:4] == b"RIFF"


def test_unknown_actions_are_rejected(rig: Rig) -> None:
    for bad in ("nope", "camera.explode", "audio.", "audio.gain; rm -rf /"):
        with pytest.raises(ActionError) as caught:
            rig.act(bad, {})
        assert caught.value.status == 400


def test_demo_frames_are_jpegs(rig: Rig) -> None:
    frame = rig.frame("program", 640)
    assert frame is not None and frame[:2] == b"\xff\xd8"


def test_audio_health_checks_are_merged_into_the_health_list(rig: Rig) -> None:
    ids = [c["id"] for c in rig.snapshot()["health"]["checks"]]
    assert ids.count("mic-stall") == 1 and ids.index("mic-stall") == ids.index("microphone") + 1
    before = rig.snapshot()["health"]["summary"]

    rig.act("audio.demo_stall", {"on": True})
    health = rig.snapshot()["health"]
    stall = next(c for c in health["checks"] if c["id"] == "mic-stall")
    assert stall["state"] == "fail" and health["summary"] == "fail"
    assert [c["id"] for c in health["checks"]].count("mic-stall") == 1

    rig.act("audio.demo_stall", {"on": False})
    assert rig.snapshot()["health"]["summary"] == before


def test_health_is_untouched_when_the_audio_controller_has_no_checks(rig: Rig) -> None:
    class Plain:
        notify = None

    rig.audio = Plain()
    rig.lighting = None
    rig.video = Plain()
    rig.hardware = None
    health = {"updated": None, "summary": "ok", "checks": [{"id": "x", "state": "ok"}]}
    assert rig._health(health) is health


def test_snapshot_carries_the_blur_state_and_the_action_reaches_the_demo_controller(rig: Rig) -> None:
    blur_state = rig.snapshot()["video"]["blur"]
    assert blur_state["available"] is True and blur_state["on"] is False
    assert set(blur_state["limits"]) == {"strength", "rate"}
    rig.act("video.blur", {"on": True, "strength": 12})
    after = rig.snapshot()["video"]["blur"]
    assert after["on"] is True and after["strength"] == 12
    assert after["cost"]["state"] == "ok"


def test_blur_cost_over_budget_reaches_the_health_list(rig: Rig) -> None:
    rig.act("video.blur", {"on": True, "rate": 1})
    health = rig.snapshot()["health"]
    check = next(c for c in health["checks"] if c["id"] == "background-blur")
    assert check["state"] == "warn" and health["summary"] != "ok"
    assert [c["id"] for c in health["checks"]].count("background-blur") == 1


def test_demo_plugin_removal_hides_nothing_and_explains_the_install(rig: Rig) -> None:
    rig.act("video.blur.plugin", {"installed": False})
    snapshot = rig.snapshot()
    assert snapshot["video"]["blur"]["plugin"]["installed"] is False
    assert snapshot["video"]["blur"]["plugin"]["hint"]
    with pytest.raises(ActionError) as caught:
        rig.act("video.blur", {"on": True})
    assert caught.value.status == 409
    rig.act("video.blur.plugin", {"installed": True})
    assert rig.snapshot()["video"]["blur"]["plugin"]["installed"] is True


def test_a_repaired_file_is_in_the_snapshot_toasts_once_and_can_be_dismissed(rig: Rig, tmp_path) -> None:
    toasts: list[tuple[str, str]] = []
    rig.on_toast = lambda level, text: toasts.append((level, text))
    damaged = tmp_path / "looks.json"
    damaged.write_text("{")
    store.read_json(damaged, {})
    store.read_json(damaged, {})  # gone now: nothing more to report
    (warning,) = rig.snapshot()["storage"]["warnings"]
    assert warning["action"] == "defaults"
    assert [level for level, _ in toasts] == ["warn"]
    assert "set aside" in toasts[0][1] and "System page" in toasts[0][1]
    rig.act("storage.dismiss", {"id": warning["id"]})
    assert rig.snapshot()["storage"]["warnings"] == []


def test_storage_actions_validate_their_input(rig: Rig) -> None:
    with pytest.raises(ActionError):
        rig.act("storage.dismiss", {"id": 5})
    with pytest.raises(ActionError):
        rig.act("storage.explode", {})
