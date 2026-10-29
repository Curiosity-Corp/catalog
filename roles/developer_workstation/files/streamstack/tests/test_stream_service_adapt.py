"""Automatic downshift in the stream service (#98): simulated loss steps the picture down live, calm brings it back."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_stream_service import Env, FakeChild, start_with_audio

from streamstack.presets import catalog
from streamstack.stream import adapt, service
from streamstack.stream.service import StreamError

DEST = {"id": "a", "kind": "srt", "url": "srt://h.test:9000"}
LADDER = {"platform": "youtube", "tier": "balanced"}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(service, "AUDIO_HEADER_WAIT_S", 0.05)
    return Env(tmp_path)


def steps() -> list[adapt.Step]:
    cat = catalog.load()
    return adapt.build_steps(cat.platform(LADDER["platform"]), cat.tiers, LADDER["tier"])


class Rig:
    """A running stream whose uplink the test controls: the sink reports whatever loss and queue `link` says."""

    def __init__(self, env: Env, extra: dict[str, Any] | None = None) -> None:
        self.env = env
        self.link: dict[str, Any] = {"loss_percent": 0.0, "queued_bytes": 0}
        top = steps()[0]
        env.svc.set({"destinations": [DEST], **dict(top.patch), "ladder": LADDER, **(extra or {})})
        start_with_audio(env, env.svc.start_stream)
        self.video = env.last("video-hevc")
        env.feed(self.video, 0, 1000, key_at={0, 600})
        env.audio(0, 1000)
        self.ms = 1000
        sink = env.svc._active["a"].sink
        self.base_status = sink.status

        def status() -> dict[str, Any]:
            return {
                **self.base_status(),
                "state": "live",
                "connected_s": 120.0,
                "queued_bytes": self.link["queued_bytes"],
                "srt": {"loss_percent": self.link["loss_percent"], "rtt_ms": 40},
            }

        sink.status = status  # type: ignore[method-assign]

    def tick(self, seconds: float = 2.5) -> None:
        self.env.wall.now += seconds
        self.env.svc.tick()

    def finish_swap(self) -> None:
        """Complete the swap in flight: the new encoder keys at its planned cut and the old one is retired."""
        env = self.env
        new = env.last("video-hevc")
        cut = float(new.argv[new.argv.index("-force_key_frames") + 1].split("t,")[1].split("+")[0]) * 1000
        key = round(cut)
        env.feed(new, key - 330, key + 400, key_at={key})
        env.feed(self.video, self.ms, key + 400)
        env.audio(self.ms, key + 400)
        self.ms, self.video = key + 400, new
        env.svc.tick()

    def swapping(self) -> bool:
        return any(run.pending is not None for run in self.env.svc._runs.values())

    def bitrate(self) -> str:
        return next(a for a in self.env.last("video-hevc").argv if a.endswith("k") and a[:-1].isdigit())


def bitrate_arg(child: FakeChild) -> str:
    return next(a for a in child.argv if a.endswith("k") and a[:-1].isdigit())


def test_simulated_loss_steps_the_picture_down_live_and_calm_brings_it_back(env: Env) -> None:
    ladder = steps()
    rig = Rig(env)
    assert rig.bitrate() == f"{ladder[0].bitrate_kbps}k"
    state = env.svc.state()
    assert state["adapt"]["step"] == 0 and state["adapt"]["degraded"] is False and state["adapt"]["ladder"] == LADDER

    rig.link["loss_percent"] = 12.0  # the uplink starts losing packets
    rig.tick()
    state = env.svc.state()
    assert state["adapt"]["step"] == 1 and state["adapt"]["degraded"] is True
    assert "12% packet loss" in state["adapt"]["history"][0]["reason"]
    assert state["adapt"]["verdict"]["level"] == adapt.SEVERE
    assert rig.bitrate() == f"{ladder[1].bitrate_kbps}k" and rig.swapping()
    assert state["settings"]["bitrate_kbps"] == ladder[0].bitrate_kbps, "the person's choice is untouched"
    assert state["effective"]["bitrate_kbps"] == ladder[1].bitrate_kbps
    assert state["output"]["height"] == ladder[1].height and state["output"]["fps"] == ladder[1].fps
    assert any(level == "warn" and "struggling" in text for level, text in env.events)
    saved = json.loads((env.svc._state_path or Path()).read_text())
    assert saved["settings"]["bitrate_kbps"] == ladder[0].bitrate_kbps and saved["ladder"] == LADDER

    rig.finish_swap()
    rig.link["loss_percent"] = 0.0  # the loss stops
    for _ in range(80):
        rig.tick()
        if rig.swapping():
            rig.finish_swap()
        if env.svc.state()["adapt"]["step"] == 0:
            break
    state = env.svc.state()
    assert state["adapt"]["step"] == 0 and state["effective"] == state["settings"]
    assert rig.bitrate() == f"{ladder[0].bitrate_kbps}k"
    assert [h["direction"] for h in state["adapt"]["history"]][-1] == "up"
    assert any(level == "ok" and "steady" in text for level, text in env.events)
    env.svc.shutdown()


def test_a_send_queue_that_keeps_growing_steps_down_without_any_loss_measurement(env: Env) -> None:
    rig = Rig(env)
    kbps = float(env.svc.state()["encoders"][0]["bitrate_kbps"]) + 128
    rig.link["queued_bytes"] = int(3.0 * kbps * 1000 / 8 * 2)  # a few seconds of video waiting
    rig.tick()
    rig.tick()
    assert env.svc.state()["adapt"]["step"] >= 1
    assert "waiting to be sent" in env.svc.state()["adapt"]["history"][0]["reason"]
    env.svc.shutdown()


def test_a_swap_still_in_flight_delays_the_shift_and_it_is_retried(env: Env) -> None:
    rig = Rig(env)
    rig.link["loss_percent"] = 12.0
    rig.tick()
    assert env.svc.state()["adapt"]["step"] == 1 and rig.swapping()
    # without finishing the swap, a second step down cannot be applied: it waits, nothing is committed twice
    rig.tick()
    rig.tick()  # the minimum gap is over and the link is still severe, but the first swap has not landed
    assert env.svc.state()["adapt"]["step"] == 1
    assert any("waits" in text for _level, text in env.events)
    rig.finish_swap()
    for _ in range(8):
        rig.tick()
        if rig.swapping():
            rig.finish_swap()
    assert env.svc.state()["adapt"]["step"] >= 2
    env.svc.shutdown()


def test_stopping_the_stream_returns_to_the_chosen_plan_and_a_new_stream_starts_there(env: Env) -> None:
    rig = Rig(env)
    rig.link["loss_percent"] = 12.0
    rig.tick()
    rig.finish_swap()
    assert env.svc.state()["adapt"]["step"] == 1
    env.svc.stop_stream()
    env.svc.tick()
    state = env.svc.state()
    assert state["adapt"]["step"] == 0 and state["effective"] == state["settings"]
    env.svc.shutdown()


def test_turning_adapt_off_returns_to_the_plan_and_stops_judging(env: Env) -> None:
    rig = Rig(env)
    rig.link["loss_percent"] = 12.0
    rig.tick()
    rig.finish_swap()
    env.svc.set({"adapt": False})
    rig.finish_swap()
    state = env.svc.state()
    assert state["adapt"]["enabled"] is False and state["adapt"]["step"] == 0
    for _ in range(10):
        rig.tick()
    assert env.svc.state()["adapt"]["step"] == 0
    with pytest.raises(StreamError, match="true or false"):
        env.svc.set({"adapt": "yes"})
    env.svc.shutdown()


def test_changing_the_picture_by_hand_forgets_the_ladder_but_audio_changes_keep_it(env: Env) -> None:
    env.svc.set({"destinations": [DEST], **dict(steps()[0].patch), "ladder": LADDER})
    env.svc.set({"audio_bitrate_kbps": 96})
    assert env.svc.state()["adapt"]["ladder"] == LADDER
    env.svc.set({"bitrate_kbps": 1234})
    adapt_state = env.svc.state()["adapt"]
    assert adapt_state["ladder"] is None and adapt_state["available"] is False
    assert any("forgotten" in text for _level, text in env.events)
    env.svc.set({"ladder": LADDER})
    assert env.svc.state()["adapt"]["ladder"] == LADDER
    env.svc.set({"ladder": None})
    assert env.svc.state()["adapt"]["ladder"] is None


@pytest.mark.parametrize(
    "ladder",
    [
        {"platform": "nowhere", "tier": "balanced"},
        {"platform": "youtube", "tier": "nope"},
        {"platform": "youtube"},
        "x",
    ],
)
def test_a_bad_ladder_is_a_400_and_changes_nothing(env: Env, ladder: object) -> None:
    before = env.svc.state()["settings"]
    with pytest.raises(StreamError) as err:
        env.svc.set({"bitrate_kbps": 4000, "ladder": ladder})
    assert err.value.status == 400
    assert env.svc.state()["settings"] == before


def test_the_ladder_survives_a_restart_and_a_vanished_plan_is_dropped_with_a_note(env: Env, tmp_path: Path) -> None:
    env.svc.set({"destinations": [DEST], **dict(steps()[0].patch), "ladder": LADDER, "adapt": False})
    path = tmp_path / "stream.json"
    again = Env(tmp_path)
    assert again.svc.state()["adapt"]["ladder"] == LADDER and again.svc.state()["adapt"]["enabled"] is False
    saved = json.loads(path.read_text())
    saved["ladder"] = {"platform": "gone", "tier": "balanced"}
    path.write_text(json.dumps(saved))
    third = Env(tmp_path)
    assert third.svc.state()["adapt"]["ladder"] is None
    assert third.svc.settings.bitrate_kbps == steps()[0].bitrate_kbps, "the settings themselves are kept"
    assert any("downshift is off" in text for text in (e["text"] for e in third.svc.state()["events"]))


def test_an_uplink_rating_that_fails_never_stops_the_supervisor(env: Env) -> None:
    def broken() -> str | None:
        raise RuntimeError("monitor down")

    env.svc._uplink = broken
    rig = Rig(env)
    rig.link["loss_percent"] = 12.0
    rig.tick()
    assert env.svc.state()["adapt"]["step"] == 1
    assert any("could not be read" in text for text in (e["text"] for e in env.svc.state()["events"]))
    env.svc.shutdown()
