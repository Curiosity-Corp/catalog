"""Automatic bandwidth downshift (#98): the ladder, the judgement of a link, and the hysteresis, all as pure logic."""

from __future__ import annotations

import itertools

import pytest

from streamstack.presets import catalog
from streamstack.presets.schema import PresetError
from streamstack.stream import adapt
from streamstack.stream.model import EncoderSettings


def steps_for(platform: str, tier: str) -> list[adapt.Step]:
    cat = catalog.load()
    return adapt.build_steps(cat.platform(platform), cat.tiers, tier)


# -- the ladder -------------------------------------------------------------------------------------------------


def test_step_zero_is_the_chosen_plan_and_every_later_step_sends_strictly_less() -> None:
    for platform in catalog.load().platforms:
        for tier in catalog.load().tiers:
            steps = steps_for(platform["id"], tier["id"])
            assert steps, (platform["id"], tier["id"])
            rates = [(s.width * s.height * s.fps, s.bitrate_kbps or 0) for s in steps]
            for before, after in itertools.pairwise(rates):
                assert after[0] <= before[0] and after < before, (platform["id"], tier["id"], steps)
            assert len({s.label for s in steps}) == len(steps), "a rung that looks like another is not a step"


def test_the_ladder_matches_what_the_presets_page_resolves_for_its_first_step() -> None:
    cat = catalog.load()
    from streamstack.presets.plan import resolve
    from streamstack.stream import planmap

    plan = resolve(cat.platform("youtube"), next(t for t in cat.tiers if t["id"] == "balanced"))
    first = steps_for("youtube", "balanced")[0]
    assert dict(first.patch) == planmap.plan_to_settings(plan)


def test_unknown_platform_or_tier_is_a_preset_error() -> None:
    cat = catalog.load()
    with pytest.raises(PresetError):
        adapt.build_steps(cat.platform("youtube"), cat.tiers, "no-such-tier")


def test_overlay_moves_only_the_picture_and_the_bitrate() -> None:
    steps = steps_for("youtube", "balanced")
    chosen = EncoderSettings(codec="hevc", encoder="libx265", tier="1080p", audio_bitrate_kbps=96, keyint_s=4.0)
    low = adapt.overlay(chosen, steps[-1])
    assert (low.codec, low.encoder, low.audio_bitrate_kbps, low.keyint_s) == ("hevc", "libx265", 96, 4.0)
    assert low.tier == "480p" and low.bitrate_kbps == steps[-1].bitrate_kbps and low.fps == steps[-1].fps


# -- what the link says -----------------------------------------------------------------------------------------


def sample(**kw: object) -> adapt.LinkSample:
    return adapt.LinkSample(dest="a", name="Twitch", **kw)  # type: ignore[arg-type]


def test_a_clean_link_is_ok_and_an_unconnected_one_is_not_judged() -> None:
    judge = adapt.Judge(adapt.AdaptConfig())
    assert judge.judge(sample(loss_percent=0.0, rtt_ms=40.0, queued_ms=50.0)).level == adapt.OK
    assert judge.judge(sample(state="backoff", loss_percent=50.0)).level == adapt.OK


@pytest.mark.parametrize(
    ("loss", "level"), [(0.5, adapt.OK), (1.5, adapt.WARN), (4.0, adapt.CONGESTED), (12.0, adapt.SEVERE)]
)
def test_packet_loss_thresholds(loss: float, level: str) -> None:
    assert adapt.Judge(adapt.AdaptConfig()).judge(sample(loss_percent=loss)).level == level


@pytest.mark.parametrize(
    ("queued_ms", "level"), [(100, adapt.OK), (500, adapt.WARN), (2000, adapt.CONGESTED), (6000, adapt.SEVERE)]
)
def test_a_growing_send_queue_is_the_universal_signal(queued_ms: float, level: str) -> None:
    assert adapt.Judge(adapt.AdaptConfig()).judge(sample(queued_ms=queued_ms)).level == level


def test_a_round_trip_far_above_the_best_seen_is_congestion_but_a_high_steady_one_is_not() -> None:
    judge = adapt.Judge(adapt.AdaptConfig())
    assert judge.judge(sample(rtt_ms=300.0)).level == adapt.OK  # a long path, steady
    assert judge.judge(sample(rtt_ms=310.0)).level == adapt.OK
    verdict = judge.judge(sample(rtt_ms=1200.0))
    assert verdict.level == adapt.CONGESTED and "1200" in verdict.reason and "Twitch" in verdict.reason


def test_dropped_frames_and_reconnects_are_read_as_changes_between_samples() -> None:
    judge = adapt.Judge(adapt.AdaptConfig())
    assert judge.judge(sample(dropped=100, restarts=4)).level == adapt.OK  # history before we looked
    assert judge.judge(sample(dropped=110, restarts=4)).level == adapt.CONGESTED
    assert judge.judge(sample(dropped=300, restarts=4)).level == adapt.SEVERE
    assert judge.judge(sample(dropped=300, restarts=5)).level == adapt.CONGESTED
    assert judge.judge(sample(dropped=300, restarts=7)).level == adapt.SEVERE


def test_send_speed_is_judged_only_once_the_connection_has_settled() -> None:
    judge = adapt.Judge(adapt.AdaptConfig())
    assert judge.judge(sample(speed=0.2, connected_s=3.0)).level == adapt.OK
    assert judge.judge(sample(speed=0.8, connected_s=30.0)).level == adapt.CONGESTED
    assert judge.judge(sample(speed=0.4, connected_s=30.0)).level == adapt.SEVERE
    assert judge.judge(sample(speed=None, connected_s=30.0)).level == adapt.OK


def test_the_worst_verdict_wins_and_names_its_destination() -> None:
    bad = adapt.Judge(adapt.AdaptConfig()).judge(adapt.LinkSample(dest="b", name="Backup", loss_percent=20.0))
    good = adapt.Verdict(adapt.OK, "", "a")
    assert adapt.worst([good, bad]) is bad and adapt.worst([]).level == adapt.OK


def test_sample_from_status_reads_whip_and_srt_measurements_and_turns_bytes_into_time() -> None:
    status = {
        "state": "live",
        "queued_bytes": 125_000,
        "dropped_frames": 2,
        "restarts": 1,
        "speed": 1.0,
        "connected_s": 20.0,
        "whip": {"rtt_ms": 80, "loss_percent": 2.5},
        "srt": {"rtt_ms": 30, "loss_percent": 9.0},
    }
    whip = adapt.sample_from_status("w", "W", "whip", status, 4000.0)
    assert (whip.loss_percent, whip.rtt_ms) == (2.5, 80.0) and whip.queued_ms == pytest.approx(250.0)
    srt = adapt.sample_from_status("s", "S", "srt", status, 4000.0)
    assert (srt.loss_percent, srt.rtt_ms) == (9.0, 30.0)
    rtmp = adapt.sample_from_status("r", "R", "rtmp", status, 4000.0)
    assert rtmp.loss_percent is None and rtmp.rtt_ms is None and rtmp.dropped == 2


# -- the decision -----------------------------------------------------------------------------------------------

CFG = adapt.AdaptConfig()


def controller(count: int = 5) -> adapt.AdaptController:
    return adapt.AdaptController([adapt.Step(f"s{i}", {}, 0, 0, 30, 1000 - i) for i in range(count)], CFG)


def run(ctl: adapt.AdaptController, now: float, level: str, uplink: str | None = None) -> adapt.Shift | None:
    shift = ctl.evaluate(now, adapt.Verdict(level, f"{level} link"), uplink)
    if shift:
        ctl.commit(now, shift)
    return shift


def test_simulated_loss_steps_down_after_two_bad_rounds_then_recovers_after_a_calm_spell() -> None:
    ctl = controller()
    assert run(ctl, 0, adapt.CONGESTED) is None  # one bad round is not enough
    shift = run(ctl, 2, adapt.CONGESTED)
    assert shift and shift.direction == "down" and shift.to == 1 and ctl.index == 1
    for t in range(4, 30, 2):  # still congested, but inside the cooldown: nothing moves until it ends
        shift = run(ctl, t, adapt.CONGESTED)
        if shift:
            assert t - 2 >= CFG.cooldown_s
            break
    assert ctl.index == 2
    start = 40.0  # the loss stops
    moves = [t for t in range(0, 200, 2) if run(ctl, start + t, adapt.OK)]
    assert ctl.index == 0, "it must come all the way back"
    first = moves[0]
    assert first >= CFG.up_hold_s, "recovery waits for a long calm spell"
    assert all(b - a >= CFG.cooldown_s for a, b in itertools.pairwise(moves)), "one rung at a time"


def test_a_severe_link_steps_down_at_once_and_twice_when_it_stays_severe() -> None:
    ctl = controller()
    shift = run(ctl, 0, adapt.SEVERE)
    assert shift and shift.to == 1
    assert run(ctl, 2, adapt.SEVERE) is None  # min gap not yet over
    shift = run(ctl, 6, adapt.SEVERE)
    assert shift and shift.direction == "down"


def test_it_never_steps_below_the_last_rung_or_above_the_first() -> None:
    ctl = controller(2)
    assert run(ctl, 0, adapt.SEVERE) and ctl.index == 1
    for t in range(10, 200, 5):
        assert run(ctl, float(t), adapt.SEVERE) is None
    assert ctl.index == 1
    top = controller(2)
    assert run(top, 0, adapt.OK) is None and run(top, 500, adapt.OK) is None and top.index == 0


def test_a_warning_holds_the_current_step_and_restarts_the_calm_spell() -> None:
    ctl = controller()
    run(ctl, 0, adapt.SEVERE)
    for t in range(10, 28, 2):
        run(ctl, float(t), adapt.OK)
    assert run(ctl, 28.0, adapt.WARN) is None
    assert run(ctl, 40.0, adapt.OK) is None and ctl.index == 1  # the 30 s count began again at 40
    assert run(ctl, 72.0, adapt.OK) and ctl.index == 0


def test_a_recovery_that_fails_quickly_doubles_the_wait_before_the_next_try() -> None:
    ctl = controller()
    run(ctl, 0, adapt.SEVERE)
    up = next(t for t in range(2, 100, 2) if run(ctl, float(t), adapt.OK))
    assert ctl.index == 0 and ctl.hold_s == CFG.up_hold_s
    assert run(ctl, up + 6.0, adapt.SEVERE)  # it did not hold
    assert ctl.hold_s == CFG.up_hold_s * 2
    assert run(ctl, up + 7.0, adapt.SEVERE) is None and ctl.index == 1  # inside the minimum gap


def test_the_hold_factor_is_forgotten_after_a_long_calm_at_the_top() -> None:
    ctl = controller()
    run(ctl, 0, adapt.SEVERE)
    for t in range(2, 100, 2):
        run(ctl, float(t), adapt.OK)
    run(ctl, 106.0, adapt.SEVERE)
    assert ctl.hold_s > CFG.up_hold_s
    ctl.reset()
    assert ctl.hold_s == CFG.up_hold_s and ctl.index == 0


def test_a_distrusted_uplink_blocks_recovery_but_never_causes_a_step_down() -> None:
    ctl = controller()
    run(ctl, 0, adapt.SEVERE)
    for t in range(10, 300, 5):
        assert run(ctl, float(t), adapt.OK, uplink="poor") is None
    assert ctl.index == 1
    steady = controller()
    for t in range(0, 100, 2):
        assert run(steady, float(t), adapt.OK, uplink="offline") is None
    assert steady.index == 0
    assert run(ctl, 400.0, adapt.OK, uplink="good") is None  # the calm count starts now
    assert run(ctl, 440.0, adapt.OK, uplink="good") and ctl.index == 0


def test_snapshot_describes_the_ladder_the_history_and_the_wait() -> None:
    ctl = controller(3)
    run(ctl, 0, adapt.SEVERE)
    snap = ctl.snapshot(5.0)
    assert snap["step"] == 1 and snap["degraded"] is True and snap["current"]["label"] == "s1"
    assert snap["cooldown_s"] == pytest.approx(CFG.cooldown_s - 5.0)
    assert [h["direction"] for h in snap["history"]] == ["down"] and snap["history"][0]["to"] == "s1"
    assert snap["recover_in_s"] == CFG.up_hold_s and len(snap["steps"]) == 3
