"""The Console's echo check: refusals, the call gate, a mid-check call, results kept, the demo twin."""

from __future__ import annotations

import threading
import time
import wave
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from streamstack import echocheck
from streamstack.console import echocheck as ec
from streamstack.console.echocheck import DemoEcho, EchoController
from streamstack.console.errors import ActionError
from streamstack.console.rig import Rig

HDMI = "alsa_output.demo-monitor.hdmi-stereo"
HEADSET = "bluez_output.demo-headset.1"


def wait(rig: Rig, timeout: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = rig.snapshot()["echo"]
        if state["state"] != "running":
            return state
        time.sleep(0.05)
    raise AssertionError("the echo check did not finish")


def finished(controller: EchoController, timeout: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = controller.state()
        if state["state"] != "running":
            return state
        time.sleep(0.05)
    raise AssertionError("the echo check did not finish")


def saved(controller: EchoController, store: Path, sink: str, timeout: float = 60.0) -> dict[str, Any]:
    """The result for `sink` as a fresh controller reads it back from disk. The save follows the result (#168), so it
    gets a deadline of its own rather than what is left of the run's (#173), long enough for a starved runner's fsync
    (up to 41 s, infra#639). A miss says what the disk and the controller hold, so CI alone can tell the cause."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remembered = EchoController(store_path=store).last_result(sink) if store.exists() else None
        if remembered is not None:
            return remembered
        time.sleep(0.05)
    on_disk = store.read_text() if store.exists() else "(no file)"
    beside = sorted(p.name for p in store.parent.iterdir())
    raise AssertionError(
        f"the result for {sink} was never saved to {store}: on disk {on_disk!r}, beside it {beside}, "
        f"in memory {sorted(controller.state()['results'])}, still to save {controller._writer.dirty}"
    )


@pytest.fixture
def toasts() -> list[tuple[str, str]]:
    return []


@pytest.fixture
def rig(toasts: list[tuple[str, str]]) -> Iterator[Rig]:
    rig = Rig.demo()
    rig.start()
    rig.on_toast = lambda level, text: toasts.append((level, text))
    yield rig
    rig.stop()


def test_speakers_bleed_and_headphones_do_not(rig: Rig) -> None:
    rig.act("echo.check", {"name": HDMI})
    state = wait(rig)
    assert state["state"] == "done"
    assert state["result"]["level"] == "strong"
    assert state["result"]["coupling_db"] == pytest.approx(ec.DEMO_BLEED_DB, abs=3.0)
    assert "Studio Monitor" in state["result"]["text"]
    rig.act("echo.check", {"name": HEADSET})
    state = wait(rig)
    assert state["result"]["level"] == "none"
    assert set(state["results"]) == {HDMI, HEADSET}


def test_it_tests_the_default_output_and_changes_nothing(rig: Rig) -> None:
    before = rig.snapshot()["audio"]["outputs"]
    rig.act("echo.check", {})
    state = wait(rig)
    after = rig.snapshot()["audio"]["outputs"]
    assert state["sink"] == before["default"] == after["default"]
    assert [(d["name"], d["volume"], d["muted"]) for d in before["devices"]] == [
        (d["name"], d["volume"], d["muted"]) for d in after["devices"]
    ]


def test_scripted_feedback_loop(rig: Rig, toasts: list[tuple[str, str]]) -> None:
    rig.act("echo.demo", {"coupling_db": -12, "loop_gap_ms": 150})
    rig.act("echo.check", {"name": HDMI})
    state = wait(rig)
    assert state["result"]["level"] == "feedback"
    assert state["result"]["loop"] is True
    assert toasts[-1][0] == "error"


def test_never_during_a_call(rig: Rig) -> None:
    rig.act("call.demo", {"app": "Teams"})
    deadline = time.monotonic() + 5
    while not rig.call.active() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert rig.snapshot()["echo"]["blocked"]
    with pytest.raises(ActionError) as err:
        rig.act("echo.check", {})
    assert err.value.status == 409
    assert "call" in str(err.value)
    assert rig.snapshot()["echo"]["state"] == "idle"


def test_a_call_that_starts_mid_check_stops_it(rig: Rig) -> None:
    rig.act("echo.check", {})
    rig.act("call.demo", {"app": "Zoom"})
    state = wait(rig)
    assert state["state"] == "error"
    assert "call" in state["error"].lower()
    assert state["result"] is None


@pytest.mark.parametrize(
    ("setup", "fragment"),
    [
        (("audio.output_mute", {"name": HDMI, "on": True}), "muted"),
        (("audio.mute", {"on": True}), "microphone is muted"),
        (("audio.demo_stall", {"on": True}), "stalled"),
    ],
)
def test_refusals_say_why(rig: Rig, setup: tuple[str, dict[str, Any]], fragment: str) -> None:
    rig.act(*setup)
    with pytest.raises(ActionError) as err:
        rig.act("echo.check", {"name": HDMI})
    assert err.value.status == 409
    assert fragment in str(err.value)


def test_unknown_output_and_options(rig: Rig) -> None:
    with pytest.raises(ActionError) as err:
        rig.act("echo.check", {"name": "no-such-sink"})
    assert err.value.status == 409
    with pytest.raises(ActionError) as err:
        rig.act("echo.check", {"loud": True})
    assert err.value.status == 400
    with pytest.raises(ActionError) as err:
        rig.act("echo.nope", {})
    assert err.value.status == 400


def test_a_second_check_while_running_is_refused(rig: Rig) -> None:
    rig.act("echo.check", {"name": HDMI})
    with pytest.raises(ActionError) as err:
        rig.act("echo.check", {"name": HDMI})
    assert err.value.status == 409
    wait(rig)


def test_results_are_remembered(tmp_path: Path) -> None:
    store = tmp_path / "echo.json"
    first = DemoEcho(store_path=store)
    first.bind(lambda: Rig.demo().snapshot())
    first.act("echo.check", {"name": HDMI})
    finished(first)
    remembered = saved(first, store, HDMI)
    assert remembered["level"] == "strong"
    again = EchoController(store_path=store)
    assert again.last_result() == remembered


def test_a_damaged_store_is_ignored(tmp_path: Path) -> None:
    store = tmp_path / "echo.json"
    store.write_text('{"results": {"x": {"at": 3}, "y": "no"}}', encoding="utf-8")
    assert EchoController(store_path=store).state()["results"] == {}


class FakeCall:
    def __init__(self) -> None:
        self.on = False
        self.listeners: list[Callable[[Any, Any], None]] = []

    def active(self) -> bool:
        return self.on

    def snapshot(self) -> Any:
        return type("S", (), {"service": "Teams"})()

    def subscribe(self, listener: Callable[[Any, Any], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


def test_silence_is_unmeasured_not_no_bleed(tmp_path: Path) -> None:
    class Silent(EchoController):
        def _capture(self, sink: dict[str, Any], source: str, stop: threading.Event) -> tuple[list[float], int, float]:
            return [0.0] * 9600, 8000, 0.3

        def _need_tools(self) -> None:
            return None

    controller = Silent(call=FakeCall())
    rig = Rig.demo()
    controller.bind(rig.snapshot)
    controller.act("echo.check", {"name": HDMI})
    deadline = time.monotonic() + 5
    while controller.state()["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    result = controller.state()["result"]
    assert result["level"] == "unmeasured"
    assert "silence" in result["text"]


def test_sweep_file_and_wav_reader_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "sweep.wav"
    ec.write_sweep(path)
    samples, rate = ec.read_wav(path)
    assert rate == ec.PLAY_RATE
    assert len(samples) == int(ec.PLAY_RATE * echocheck.SWEEP_S)
    assert max(abs(v) for v in samples) == pytest.approx(10 ** (echocheck.PLAY_LEVEL_DB / 20), abs=0.01)


def test_wav_reader_takes_the_first_channel_and_rejects_junk(tmp_path: Path) -> None:
    stereo = tmp_path / "stereo.wav"
    with wave.open(str(stereo), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x40\x00\xc0" * 10)  # left +0.5, right -0.5
    samples, rate = ec.read_wav(stereo)
    assert rate == 16000
    assert samples == [0.5] * 10
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not a wav")
    with pytest.raises(ec.CaptureError):
        ec.read_wav(junk)


def test_demo_reset_clears_everything(rig: Rig) -> None:
    rig.act("echo.demo", {"coupling_db": -20})
    rig.act("echo.check", {"name": HDMI})
    wait(rig)
    rig.act("echo.demo_reset", {})
    state = rig.snapshot()["echo"]
    assert state["state"] == "idle"
    assert state["results"] == {}
    assert state["demo"] is True


def test_a_rig_without_the_check_reports_it_unavailable() -> None:
    assert ec.empty_state()["available"] is False


# -- the link context (#156): the network monitor and the SRT stats ------------------------------


def _state(**parts: Any) -> dict[str, Any]:
    return {"network": {"available": True, "enabled": True, "rating": "good", "cause": ""}, **parts}


def _dest(**over: Any) -> dict[str, Any]:
    return {"id": "d1", "name": "Backup SRT", "kind": "srt", "active": True, "state": "live", **over}


def test_a_clean_link_says_nothing() -> None:
    assert ec.link_context(_state(stream={"destinations": [_dest(srt={"probe_loss_percent": 0, "rtt_ms": 20})]})) == {
        "level": "ok",
        "reasons": [],
    }
    assert ec.link_context({})["level"] == "ok"


@pytest.mark.parametrize("rating", ["degraded", "poor", "offline"])
def test_a_bad_network_rating_is_named(rating: str) -> None:
    state = _state()
    state["network"].update(rating=rating, cause="weak Wi-Fi")
    context = ec.link_context(state)
    assert context["level"] == "caveat"
    assert context["reasons"] == [f"the network is {rating} (weak Wi-Fi)"]


def test_a_monitor_that_is_off_is_not_judged() -> None:
    state = _state()
    state["network"].update(rating="poor", enabled=False)
    assert ec.link_context(state)["level"] == "ok"


def test_stalling_streams_and_srt_stats_are_named_but_recordings_and_idle_destinations_are_not() -> None:
    destinations = [
        _dest(id="a", name="Twitch", kind="rtmp", state="backoff"),
        _dest(id="b", srt={"probe_loss_percent": 25.0, "rtt_ms": 30}),
        _dest(id="c", name="Slow SRT", srt={"probe_loss_percent": 0, "rtt_ms": 520}),
        _dest(id="d", name="Disk", kind="record", state="failed"),
        _dest(id="e", name="Off", active=False, state="failed"),
    ]
    reasons = ec.link_context(_state(stream={"destinations": destinations}))["reasons"]
    assert reasons == [
        "the stream to Twitch is stalling (backoff)",
        "the SRT path to Backup SRT loses 25 % of its probes",
        "the SRT path to Slow SRT has a 520 ms round trip",
    ]


def test_a_clear_verdict_on_a_bad_link_says_so_and_warns(tmp_path: Path) -> None:
    rig = Rig.demo()
    toasts: list[tuple[str, str]] = []
    controller = DemoEcho(lambda level, text: toasts.append((level, text)), store_path=tmp_path / "e.json")

    def snapshot() -> dict[str, Any]:
        return {**rig.snapshot(), "network": {"available": True, "enabled": True, "rating": "poor", "cause": "x"}}

    controller.bind(snapshot)
    controller.act("echo.check", {"name": HEADSET})
    result = finished(controller)["result"]
    assert result["level"] == "none"
    assert result["link"]["level"] == "caveat"
    assert "may come from it" in result["text"]
    assert toasts[-1][0] == "warn"
    assert saved(controller, tmp_path / "e.json", HEADSET)["link"]["level"] == "caveat"


def test_a_clean_link_leaves_the_text_alone(rig: Rig) -> None:
    rig.act("echo.check", {"name": HEADSET})
    result = wait(rig)["result"]
    assert result["link"] == {"level": "ok", "reasons": []}
    assert "link" not in result["text"]


def test_a_bleed_on_a_bad_link_is_not_blamed_only_on_the_link() -> None:
    text = ec.with_caveat("Bleed at -34 dB.", "strong", {"reasons": ["the network is degraded"]})
    assert text.startswith("Bleed at -34 dB.")
    assert "as well as from the bleed" in text
    assert ec.with_caveat("x", "unmeasured", {"reasons": ["y"]}) == "x"
