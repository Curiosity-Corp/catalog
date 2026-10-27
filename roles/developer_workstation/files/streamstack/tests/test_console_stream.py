from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_stream_service import Env

from streamstack.console.errors import ActionError
from streamstack.console.rig import Rig
from streamstack.console.stream import DemoStream, StreamController, empty_state, plan_to_settings


@pytest.fixture
def rig() -> Iterator[Rig]:
    rig = Rig.demo()
    rig.start()
    yield rig
    rig.stop()


@pytest.fixture
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[StreamController, Env]]:
    from streamstack.stream import service

    monkeypatch.setattr(service, "AUDIO_HEADER_WAIT_S", 0.05)
    env = Env(tmp_path)
    controller = StreamController({}, service=env.svc)
    yield controller, env
    controller.stop()


def schema(value: object) -> object:
    """The shape of a state value: key sets and types, so live and demo can be compared."""
    if isinstance(value, dict):
        return {k: schema(v) for k, v in value.items()}
    if isinstance(value, list):
        return [schema(value[0])] if value else []
    return "number" if isinstance(value, int | float) and not isinstance(value, bool) else type(value).__name__


def test_snapshot_carries_a_stream_section(rig: Rig) -> None:
    stream = rig.snapshot()["stream"]
    assert stream["status"] == "idle" and stream["probe"]["codecs"]
    json.dumps(stream)  # the whole section must be JSON-ready


def test_demo_actions_cover_the_whole_surface(rig: Rig) -> None:
    assert rig.act("stream.start", {}) == "Streaming."
    state = rig.snapshot()["stream"]
    assert state["status"] == "live" and all(d["state"] == "live" for d in state["destinations"] if d["active"])
    assert state["bitrate_kbps"] > 0 and state["latency_ms"] > 0
    rig.act("stream.set", {"bitrate_kbps": 5000, "tier": "720p"})
    assert rig.snapshot()["stream"]["settings"]["bitrate_kbps"] == 5000
    rig.act("record.start", {"format": "mp4"})
    assert rig.snapshot()["stream"]["recording"]["active"]
    rig.act("stream.stop", {})
    assert rig.snapshot()["stream"]["status"] == "recording"
    rig.act("record.stop", {})
    assert rig.snapshot()["stream"]["status"] == "idle"
    rig.act("stream.probe", {})


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("stream.set", {}),
        ("stream.set", {"bitrate_kbps": 1}),
        ("stream.start", {"destinations": "twitch"}),
        ("stream.stop", {"destination": 5}),
        ("stream.stop", {"destination": "not-running"}),
        ("record.start", {"format": "avi"}),
        ("stream.nonsense", {}),
    ],
)
def test_bad_requests_are_400s_on_the_demo(rig: Rig, action: str, params: dict) -> None:
    with pytest.raises(ActionError) as err:
        rig.act(action, params)
    assert err.value.status == 400


def test_rig_without_stream_says_so() -> None:
    demo = Rig.demo()
    demo.stream = None
    with pytest.raises(ActionError, match="not part of this rig") as err:
        demo.act("record.start", {})
    assert err.value.status == 409
    assert demo.snapshot()["stream"]["status"] == "unavailable"
    assert empty_state()["destinations"] == []


def test_live_and_demo_states_share_one_schema(live: tuple[StreamController, Env]) -> None:
    controller, _env = live
    controller.service.warm_probe()
    controller.service.host()
    controller.act("stream.set", {"destinations": [{"id": "a", "kind": "srt", "url": "srt://h.test:9000"}]})
    demo = DemoStream()
    demo.act("stream.set", {"destinations": [{"id": "a", "kind": "srt", "url": "srt://h.test:9000"}]})
    live_state, demo_state = controller.state(), demo.state()
    assert set(live_state) == set(demo_state)
    assert schema(live_state["destinations"]) == schema(demo_state["destinations"])
    assert set(live_state["recording"]) >= set(DemoStream().state()["recording"]) - {"state", "last_error"}
    assert set(live_state["settings"]) == set(demo_state["settings"])


def test_live_controller_maps_service_errors_to_action_errors(live: tuple[StreamController, Env]) -> None:
    controller, _env = live
    with pytest.raises(ActionError, match="Add a destination") as err:
        controller.act("stream.start", {})
    assert err.value.status == 400
    with pytest.raises(ActionError) as err:
        controller.act("stream.set", {"fps": 2})
    assert err.value.status == 400
    with pytest.raises(ActionError):
        controller.act("stream.explode", {})
    with pytest.raises(ActionError, match="true or false"):
        controller.act("stream.probe", {"refresh": "yes"})
    assert controller.act("stream.probe", {"refresh": True}) == "Probed this host."
    assert controller.act("record.stop", {}) == "Not recording."


def test_live_toasts_reach_the_rig(live: tuple[StreamController, Env]) -> None:
    controller, env = live
    toasts: list[tuple[str, str]] = []
    controller.notify = lambda level, text: toasts.append((level, text))
    env.svc._event("warn", "hello")
    assert toasts == [("warn", "hello")]


def test_state_exposes_the_probe_with_reasons(live: tuple[StreamController, Env]) -> None:
    controller, env = live
    env.whip_reason = "aiortc is not installed (install the whip extra: pip install 'streamstack[whip]')"
    controller.act("stream.probe", {})
    probe = controller.state()["probe"]
    assert probe["codecs"] == ["av1", "hevc", "h264"]
    whip = next(t for t in probe["transports"] if t["id"] == "whip")
    assert whip["ok"] is False and "streamstack[whip]" in whip["reason"]


def test_a_presets_plan_becomes_stream_settings() -> None:
    plan = {
        "platform": "twitch",
        "tier": "balanced",  # a bandwidth tier: never a resolution tier
        "video": {
            "width": 1920,
            "height": 1080,
            "fps": 60,
            "rate_control": "cbr",
            "keyframe_interval_s": 2,
            "codecs": [{"codec": "av1", "bitrate_kbps": 6000}, {"codec": "h264", "bitrate_kbps": 8000}],
        },
        "audio": {"bitrate_kbps": 160},
    }
    assert plan_to_settings(plan) == {
        "tier": "1080p",
        "fps": 60,
        "rate_control": "cbr",
        "keyint_s": 2.0,
        "codec": "auto",
        "bitrate_kbps": 6000,
        "audio_bitrate_kbps": 160,
    }


def test_a_plan_with_one_codec_pins_it_and_a_vertical_picture_uses_its_short_side() -> None:
    plan = {
        "video": {
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "rate_control": "cqp",
            "codecs": [{"codec": "hevc", "quality": 24}],
        },
        "audio": {"bitrate_kbps": 0},
    }
    assert plan_to_settings(plan) == {"tier": "1080p", "fps": 30, "rate_control": "cqp", "codec": "hevc", "quality": 24}


def test_a_plan_without_a_video_section_is_a_400() -> None:
    with pytest.raises(ActionError) as caught:
        plan_to_settings({"platform": "x"})
    assert caught.value.status == 400


def test_stream_set_accepts_the_presets_contract_live_and_demo(live: tuple[StreamController, Env], rig: Rig) -> None:
    controller, _env = live
    params = {
        "platform": "youtube",
        "tier": "high",
        "plan": {
            "video": {"width": 1280, "height": 720, "fps": 30, "codecs": [{"codec": "h264", "bitrate_kbps": 4500}]}
        },
    }
    controller.act("stream.set", params)
    assert controller.state()["settings"]["tier"] == "720p"
    assert controller.state()["settings"]["bitrate_kbps"] == 4500
    rig.act("stream.set", params)
    assert rig.snapshot()["stream"]["settings"]["codec"] == "h264"


def test_demo_keys_are_write_only(rig: Rig) -> None:
    dest = {"id": "yt", "kind": "rtmp", "name": "YouTube", "url": "rtmp://a.rtmp.youtube.test/live2"}
    rig.act("stream.set", {"destinations": [{**dest, "key": "abcd-1234-secret"}]})
    state = rig.snapshot()["stream"]
    assert state["destinations"][0]["key"] == "set" and "abcd-1234-secret" not in json.dumps(state)
    rig.act("stream.set", {"destinations": [{**dest, "name": "YouTube main"}]})  # an edit keeps the key
    assert rig.snapshot()["stream"]["destinations"][0]["key"] == "set"
    rig.act("stream.set", {"destinations": [{**dest, "key": ""}]})
    assert rig.snapshot()["stream"]["destinations"][0]["key"] == "none"


def test_demo_refuses_an_encoder_the_probe_says_is_broken(rig: Rig) -> None:
    with pytest.raises(ActionError) as err:
        rig.act("stream.set", {"encoder": "av1_nvenc"})
    assert err.value.status == 400
    rig.act("stream.set", {"encoder": "hevc_vaapi"})
    assert rig.snapshot()["stream"]["settings"]["encoder"] == "hevc_vaapi"


def test_demo_live_changes_follow_the_documented_classes(rig: Rig) -> None:
    assert rig.act("stream.set", {"bitrate_kbps": 4000}).startswith("Saved")
    rig.act("stream.start", {})
    assert "no reconnect" in rig.act("stream.set", {"audio_bitrate_kbps": 160})
    assert "stay connected" in rig.act("stream.set", {"bitrate_kbps": 5000})
    stream = rig.snapshot()["stream"]
    assert stream["encoders"][0]["swapping"] and stream["last_swap"]["ok"]
    assert "reconnect once" in rig.act("stream.set", {"tier": "720p"})
    assert rig.snapshot()["stream"]["destinations"][0]["restarts"] == 1
    assert rig.act("stream.set", {"tier": "720p"}) == "Nothing changed."
    rig.act("stream.stop", {})


def test_demo_shows_a_destination_that_keeps_reconnecting(rig: Rig) -> None:
    flaky = {"id": "flaky", "kind": "srt", "name": "Flaky", "url": "srt://flaky.demo.test:9000"}
    rig.act("stream.set", {"destinations": [flaky]})
    rig.act("stream.start", {})
    stream = rig.snapshot()["stream"]
    dest = stream["destinations"][0]
    assert dest["state"] == "backoff" and dest["health"] == "warn" and dest["restarts"] >= 3 and dest["last_error"]
    assert stream["status"] == "degraded"
    rig.act("stream.stop", {})


def test_demo_reports_cpu_and_gpu_only_while_encoding(rig: Rig) -> None:
    assert rig.snapshot()["stream"]["cpu_percent"] is None
    rig.act("stream.start", {})
    stream = rig.snapshot()["stream"]
    assert stream["cpu_percent"] > 0 and stream["encoders"][0]["gpu_percent"] > 0
    rig.act("stream.stop", {})
