"""Pre-flight rules: each injected fault lowers exactly its own check and offers its one-press fix."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from streamstack import preflight
from streamstack.console.rig import Rig
from streamstack.preflight import Facts, MicProbe, NetworkFacts

GOOD_MIC = MicProbe(floor_db=-62.0, speech_db=-21.0, peak_db=-7.0, heard_speech=True, frames=300)
GOOD_NET = NetworkFacts(rtt_ms=28.0, jitter_ms=4.0, loss_pct=0.0, up_mbps=11.0, down_mbps=80.0)


@pytest.fixture(scope="module")
def demo_state() -> dict[str, Any]:
    rig = Rig.demo()
    rig.start()
    try:
        return rig.snapshot()
    finally:
        rig.stop()


@pytest.fixture
def state(demo_state: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(demo_state)


def facts(state: dict[str, Any], **over: Any) -> Facts:
    base: dict[str, Any] = {
        "mic": GOOD_MIC,
        "network": GOOD_NET,
        "face": True,
        "gpu_percent": 30.0,
        "disk_free_gb": 100.0,
    }
    return Facts(state=state, **{**base, **over})


def by_id(checks: list[preflight.Check]) -> dict[str, preflight.Check]:
    return {c.id: c for c in checks}


def test_a_healthy_demo_rig_scores_100_with_nothing_to_fix(state: dict[str, Any]) -> None:
    checks = preflight.run_checks(facts(state))
    assert [c.id for c in checks] == list(preflight.WEIGHTS)
    assert all(c.state in ("ok", "skip") for c in checks), [(c.id, c.state, c.detail) for c in checks]
    assert preflight.score(checks) == 100
    assert preflight.summary(checks)["verdict"] == "ready"
    assert all(c.fix is None for c in checks)
    assert by_id(checks)["meeting"].state == "skip"  # no call on: nothing to judge


def lowered(state: dict[str, Any], **over: Any) -> dict[str, preflight.Check]:
    return by_id(preflight.run_checks(facts(state, **over)))


def only(checks: dict[str, preflight.Check], check_id: str, state: str) -> preflight.Check:
    """`check_id` is in `state` and every other check is as healthy as it was."""
    others = {c.id: c.state for c in checks.values() if c.id != check_id}
    assert checks[check_id].state == state, checks[check_id]
    assert all(s in ("ok", "skip") for s in others.values()), others
    return checks[check_id]


def test_mic_stall_fails_the_mic_and_offers_revive(state: dict[str, Any]) -> None:
    state["audio"]["mic"]["stalled"] = True
    check = only(lowered(state), "mic", "fail")
    assert check.fix and check.fix.action == "audio.revive"


def test_muted_mic_warns_with_unmute(state: dict[str, Any]) -> None:
    state["audio"]["mic"]["muted"] = True
    check = only(lowered(state), "mic", "warn")
    assert check.fix and (check.fix.action, dict(check.fix.params)) == ("audio.mute", {"on": False})


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        (MicProbe(-34.0, -21.0, -7.0, True, 300), "fail"),  # a hot noise floor, and SNR 13 dB
        (MicProbe(-50.0, -21.0, -7.0, True, 300), "ok"),
        (MicProbe(-40.0, -21.0, -7.0, True, 300), "warn"),
        (MicProbe(-62.0, -50.0, -30.0, True, 300), "warn"),  # a quiet voice
        (MicProbe(-62.0, -21.0, -0.2, True, 300), "warn"),  # clipping
        (MicProbe(-62.0, -58.0, -50.0, False, 300), "warn"),  # nobody spoke
        (MicProbe(None, None, None, False, 0), "warn"),  # the meter delivered nothing
    ],
)
def test_the_spoken_prompt_judges_level_floor_snr_and_headroom(
    state: dict[str, Any], probe: MicProbe, expected: str
) -> None:
    check = lowered(state, mic=probe)["mic"]
    assert check.state == expected, check.detail


def test_no_probe_still_checks_the_node_and_says_the_level_was_not_tested(state: dict[str, Any]) -> None:
    check = lowered(state, mic=None)["mic"]
    assert check.state == "ok" and "spoken prompt" in check.detail


def test_loudness_target_is_compared_when_the_loudness_stage_is_on(state: dict[str, Any]) -> None:
    state["audio"]["chain"]["loudness"].update(on=True, level_lufs=-26.0, target_lufs=-16.0)
    check = only(lowered(state), "mic", "warn")
    assert "LUFS" in check.detail


def test_wrong_default_output_warns_and_offers_the_headphones(state: dict[str, Any]) -> None:
    out = state["audio"]["outputs"]
    hdmi = next(d for d in out["devices"] if d["kind"] == "hdmi")
    out["default"] = hdmi["name"]
    check = only(lowered(state), "outputs", "warn")
    headphones = next(d for d in out["devices"] if d["kind"] == "headphones")
    assert check.fix and check.fix.action == "audio.output_default" and check.fix.params == {"name": headphones["name"]}


def test_missing_default_output_fails(state: dict[str, Any]) -> None:
    state["audio"]["outputs"]["default"] = None
    check = only(lowered(state), "outputs", "fail")
    assert check.fix and check.fix.action == "audio.output_default"


@pytest.mark.parametrize(("volume", "muted"), [(3, False), (100, False), (55, True)])
def test_inaudible_or_blasting_default_output_warns(state: dict[str, Any], volume: int, muted: bool) -> None:
    default = next(d for d in state["audio"]["outputs"]["devices"] if d["default"])
    default.update(volume=volume, muted=muted)
    check = only(lowered(state), "outputs", "warn")
    assert check.fix is not None


def test_light_off_warns_with_turn_on(state: dict[str, Any]) -> None:
    assert len(state["lighting"]["lights"]) == 2
    state["lighting"]["lights"][0]["power"] = False
    check = only(lowered(state), "lights", "warn")
    assert check.fix and check.fix.action == "lighting.power" and check.fix.params["on"] is True


def test_light_outside_its_range_warns_and_a_lost_light_fails(state: dict[str, Any]) -> None:
    light = state["lighting"]["lights"][1]
    light.update(power=True, role="auto", brightness=80)  # not the demo's own values, which follow its schedule
    light["limits"].update(min_brightness=10, max_brightness=40)
    check = only(lowered(state), "lights", "warn")
    assert check.fix and check.fix.action == "lighting.brightness" and check.fix.params["percent"] == 40
    light["connected"] = False
    assert only(lowered(state), "lights", "fail").fix.action == "lighting.rescan"  # type: ignore[union-attr]


def test_manual_lights_are_left_alone(state: dict[str, Any]) -> None:
    state["lighting"]["lights"][0].update(power=False, role="manual")
    assert lowered(state)["lights"].state == "ok"


def test_a_rig_with_lights_that_found_none_warns_and_one_without_skips(state: dict[str, Any]) -> None:
    state["lighting"]["lights"] = []
    assert state["hardware"]["lights"] is True
    assert lowered(state)["lights"].state == "warn"
    state["hardware"]["lights"] = False
    assert lowered(state)["lights"].state == "skip"


@pytest.mark.parametrize(
    ("net", "expected"),
    [
        (NetworkFacts(42.0, 300.0, 0.4, 11.0, 80.0), "fail"),  # 300 ms jitter
        (NetworkFacts(42.0, 45.0, 0.4), "warn"),
        (NetworkFacts(400.0, 4.0, 0.0), "fail"),
        (NetworkFacts(40.0, 4.0, 2.0), "warn"),
        (NetworkFacts(40.0, 4.0, 0.0, up_mbps=1.5), "warn"),
        (GOOD_NET, "ok"),
    ],
)
def test_network_thresholds(state: dict[str, Any], net: NetworkFacts, expected: str) -> None:
    assert lowered(state, network=net)["network"].state == expected


def test_jitter_lowers_only_the_network_check(state: dict[str, Any]) -> None:
    only(lowered(state, network=NetworkFacts(42.0, 300.0, 0.4)), "network", "fail")


def test_no_network_measurement_is_skipped_not_scored(state: dict[str, Any]) -> None:
    checks = preflight.run_checks(facts(state, network=None))
    assert by_id(checks)["network"].state == "skip" and preflight.score(checks) == 100


def test_gpu_busy_lowers_only_headroom(state: dict[str, Any]) -> None:
    only(lowered(state, gpu_percent=97.0), "headroom", "warn")
    only(lowered(state, gpu_percent=99.5), "headroom", "fail")


def test_cpu_dropped_frames_and_disk_lower_headroom(state: dict[str, Any]) -> None:
    state["system"]["cpu_percent"] = 91.0
    only(lowered(state), "headroom", "warn")
    state["system"]["cpu_percent"] = 20.0
    state["stream"]["dropped_frames"] = 12
    only(lowered(state), "headroom", "warn")
    state["stream"]["dropped_frames"] = 0
    only(lowered(state, disk_free_gb=0.5), "headroom", "fail")


def test_no_face_lowers_only_the_camera(state: dict[str, Any]) -> None:
    only(lowered(state, face=False), "camera", "warn")
    only(lowered(state, exposure_clipped_pct=30.0), "camera", "warn")


def test_camera_off_privacy_and_engine_trouble(state: dict[str, Any]) -> None:
    state["video"]["privacy"] = True
    check = only(lowered(state), "camera", "warn")
    assert check.fix and check.fix.action == "video.privacy" and check.fix.params == {"on": False}
    state["video"]["privacy"] = False
    state["video"]["engine"].update(state="error", fps=0.0)
    assert only(lowered(state), "camera", "fail").fix.action == "studio.start"  # type: ignore[union-attr]
    state["video"]["engine"].update(state="running", fps=15.0)
    only(lowered(state), "camera", "warn")
    state["studio"]["running"] = False
    assert lowered(state)["camera"].state == "fail"


def test_a_virtual_camera_without_a_producer_fails(state: dict[str, Any]) -> None:
    state["virtual"]["camera"]["producer"] = False
    only(lowered(state), "camera", "fail")


def test_the_meeting_app_must_use_streamstack_devices(state: dict[str, Any]) -> None:
    state["call"] = {"active": True, "service": "Teams", "camera": True, "mic": "open"}
    state["virtual"]["seen"] = [{"kind": "camera", "app": "Teams"}, {"kind": "mic", "app": "Teams"}]
    assert lowered(state)["meeting"].state == "ok"
    state["virtual"]["seen"] = []
    check = lowered(state)["meeting"]
    assert check.state == "warn" and "StreamStack Camera" in check.detail and "StreamStack Mic" in check.detail
    state["call"]["active"] = False
    assert lowered(state)["meeting"].state == "skip"


def test_score_weights_and_verdicts() -> None:
    def check(cid: str, st: str) -> preflight.Check:
        return preflight.Check(cid, st, "")

    everything = [check(cid, "ok") for cid in preflight.WEIGHTS]
    assert preflight.score(everything) == 100
    failing_mic = [check(c, "fail" if c == "mic" else "ok") for c in preflight.WEIGHTS]
    assert preflight.score(failing_mic) == round(100 * (100 - preflight.WEIGHTS["mic"]) / 100)
    assert preflight.verdict(failing_mic, preflight.score(failing_mic)) == "not-ready"
    warn_lights = [check(c, "warn" if c == "lights" else "ok") for c in preflight.WEIGHTS]
    assert preflight.verdict(warn_lights, preflight.score(warn_lights)) == "check"
    assert preflight.score([check("mic", "skip")]) == 0
    assert sum(preflight.WEIGHTS.values()) == 100


def test_mic_probe_from_meter_frames() -> None:
    room = [(-62.0 + (i % 3), -50.0) for i in range(200)]
    voice = [(-24.0 + (i % 4), -9.0) for i in range(60)]
    probe = preflight.mic_probe(room + voice)
    assert probe.heard_speech and probe.floor_db is not None and probe.floor_db < -58
    assert probe.snr_db is not None and probe.snr_db > 30 and probe.peak_db == -9.0
    quiet = preflight.mic_probe(room)
    assert not quiet.heard_speech and quiet.snr_db is None
    assert preflight.mic_probe([]).frames == 0
