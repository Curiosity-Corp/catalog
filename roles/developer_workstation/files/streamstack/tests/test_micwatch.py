"""Microphone watchdog: ALSA PCM stall signal (procfs fixtures), debounce, revive escalation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from streamstack import micwatch
from streamstack.micwatch import StallDetector

FIXTURES = Path(__file__).parent / "fixtures"
MIC = "alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR_DS23M2A00049-00.mono-fallback"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Mapping the pactl source to its ALSA PCM
# ---------------------------------------------------------------------------


def source_json(
    *,
    state: str = "RUNNING",
    mute: bool = False,
    api: str = "alsa",
    card: str | None = "1",
    device: str = "0",
    **extra: str,
) -> str:
    props: dict[str, str] = {"device.api": api, "alsa.device": device, "alsa.subdevice": "0", **extra}
    if card is not None:
        props["alsa.card"] = card
    return json.dumps([{"name": MIC, "state": state, "mute": mute, "properties": props}])


def test_source_maps_to_its_alsa_pcm() -> None:
    info = micwatch.parse_source(source_json(), MIC)
    assert info is not None and info.state == "RUNNING" and not info.muted
    assert info.pcm == micwatch.AlsaPcm(1, 0, 0)
    assert info.pcm is not None and str(info.pcm.path) == "/proc/asound/card1/pcm0c/sub0/status"


def test_source_falls_back_to_the_api_card_property() -> None:
    info = micwatch.parse_source(source_json(card=None, **{"api.alsa.pcm.card": "3"}), MIC)
    assert info is not None and info.pcm == micwatch.AlsaPcm(3, 0, 0)


@pytest.mark.parametrize("api", ["bluez5", "", "jack"])
def test_non_alsa_sources_have_no_pcm(api: str) -> None:
    info = micwatch.parse_source(source_json(api=api), MIC)
    assert info is not None and info.pcm is None


def test_source_lookup_survives_garbage_and_absence() -> None:
    assert micwatch.parse_source("junk", MIC) is None
    assert micwatch.parse_source("{}", MIC) is None
    assert micwatch.parse_source(source_json(), "other") is None


# ---------------------------------------------------------------------------
# /proc/asound status
# ---------------------------------------------------------------------------


def test_status_file_of_a_healthy_capture_pcm() -> None:
    sample = micwatch.parse_pcm_status(fixture("pcm_status_running.txt"), 5.0)
    assert sample == micwatch.PcmSample("RUNNING", 2677872, 2676736, 5.0)


def test_status_file_of_a_closed_pcm() -> None:
    sample = micwatch.parse_pcm_status(fixture("pcm_status_closed.txt"))
    assert sample is not None and sample.state == "closed" and sample.hw_ptr is None


@pytest.mark.parametrize("text", ["", "\n", "-----\n"])
def test_status_file_garbage_is_none(text: str) -> None:
    assert micwatch.parse_pcm_status(text) is None


def test_read_pcm_status_reports_unreadable_as_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(micwatch, "PROC_ASOUND", tmp_path)
    pcm = micwatch.AlsaPcm(1, 0, 0)
    assert micwatch.read_pcm_status(pcm) is None
    pcm.path.parent.mkdir(parents=True)
    pcm.path.write_text(fixture("pcm_status_running.txt"))
    sample = micwatch.read_pcm_status(pcm, lambda: 9.0)
    assert sample is not None and sample.state == "RUNNING" and sample.at == 9.0


# ---------------------------------------------------------------------------
# The verdict over a window
# ---------------------------------------------------------------------------


class Pcm:
    """A scripted /proc status: each read is the next (state, hw, appl) step; `rate` frames per second advance."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.state = "RUNNING"
        self.hw = 1_342_896
        self.appl = 1_341_900
        self.hw_rate = 48_000
        self.appl_rate = 48_000
        self.reads = 0
        self.last = clock.now

    def __call__(self, pcm: micwatch.AlsaPcm) -> micwatch.PcmSample | None:
        dt = self.clock.now - self.last
        self.last = self.clock.now
        self.hw += int(self.hw_rate * dt)
        self.appl += int(self.appl_rate * dt)
        self.reads += 1
        if self.state == "closed":
            return micwatch.PcmSample("closed")
        return micwatch.PcmSample(self.state, self.hw, self.appl)


def run_monitor(pcm: Pcm, clock: Clock, seconds: int, **kw: Any) -> list[micwatch.StallProbe]:
    monitor = micwatch.StallMonitor(clock, pcm)
    listing = kw.pop("listing", source_json())
    out = []
    for _ in range(seconds + 1):
        out.append(monitor.probe(MIC, listing, **kw))
        clock.advance(1.0)
    return out


def test_a_healthy_microphone_is_never_stalled() -> None:
    """The case the coordinator measured: hw_ptr and appl_ptr both advance ~48000/s, appl trailing by ~1k."""
    clock = Clock()
    verdicts = run_monitor(Pcm(clock), clock, 12)
    assert not any(v.stalled for v in verdicts)
    assert verdicts[-1].stalled is False and verdicts[-1].state == "healthy"


def test_no_verdict_before_a_full_window() -> None:
    clock = Clock()
    verdicts = run_monitor(Pcm(clock), clock, 4)
    assert [v.stalled for v in verdicts] == [None] * 5
    assert verdicts[0].state == "watching"


def test_a_pcm_that_is_not_running_is_stalled_after_the_window() -> None:
    clock = Clock()
    pcm = Pcm(clock)
    pcm.state = "closed"
    verdicts = run_monitor(pcm, clock, 6)
    assert [v.stalled for v in verdicts[:5]] == [None] * 5
    assert verdicts[5].stalled is True and "closed" in verdicts[5].detail


@pytest.mark.parametrize("state", ["XRUN", "SETUP", "PREPARED"])
def test_other_non_running_states_are_stalls_too(state: str) -> None:
    clock = Clock()
    pcm = Pcm(clock)
    pcm.state = state
    assert run_monitor(pcm, clock, 6)[-1].stalled is True


def test_hw_ptr_not_advancing_is_a_stall() -> None:
    clock = Clock()
    pcm = Pcm(clock)
    pcm.hw_rate = 0
    verdict = run_monitor(pcm, clock, 6)[-1]
    assert verdict.stalled is True and "hw_ptr" in verdict.detail


def test_appl_ptr_not_advancing_while_hw_does_is_pipewire_not_reading() -> None:
    clock = Clock()
    pcm = Pcm(clock)
    pcm.appl_rate = 0
    verdict = run_monitor(pcm, clock, 6)[-1]
    assert verdict.stalled is True and "PipeWire is not reading" in verdict.detail


def test_a_stall_that_starts_mid_stream_is_found_and_a_recovery_clears_it() -> None:
    clock = Clock()
    pcm = Pcm(clock)
    monitor = micwatch.StallMonitor(clock, pcm)
    for _ in range(8):
        assert monitor.probe(MIC, source_json()).stalled is not True
        clock.advance(1)
    pcm.hw_rate = pcm.appl_rate = 0
    seen = []
    for _ in range(8):
        seen.append(monitor.probe(MIC, source_json()).stalled)
        clock.advance(1)
    assert seen[-1] is True
    pcm.hw_rate = pcm.appl_rate = 48_000
    for _ in range(2):
        clock.advance(1)
        verdict = monitor.probe(MIC, source_json())
    assert verdict.stalled is False


def test_corroboration_by_our_own_meter_is_mentioned_but_never_a_trigger() -> None:
    clock = Clock()
    healthy = run_monitor(Pcm(clock), clock, 8, silent=True)
    assert not any(v.stalled for v in healthy)  # parec heard nothing, but the PCM moves: not stalled
    clock = Clock()
    pcm = Pcm(clock)
    pcm.hw_rate = 0
    stalled = run_monitor(pcm, clock, 6, silent=True)[-1]
    assert stalled.stalled is True and "meter" in stalled.detail


def test_counter_wraparound_counts_as_movement() -> None:
    samples = [micwatch.PcmSample("RUNNING", 9000, 8000, 0.0), micwatch.PcmSample("RUNNING", 200, 100, 5.0)]
    assert micwatch.judge(samples).stalled is False


def test_missing_pointers_are_unknown_not_a_stall() -> None:
    samples = [micwatch.PcmSample("RUNNING", None, None, 0.0), micwatch.PcmSample("RUNNING", None, None, 5.0)]
    assert micwatch.judge(samples).stalled is None


# ---------------------------------------------------------------------------
# What is never judged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("listing", "state"),
    [
        (source_json(api="bluez5"), "unsupported"),
        (source_json(mute=True), "muted"),
        (source_json(state="SUSPENDED"), "idle"),
        (source_json(state="IDLE"), "idle"),
        ("junk", "absent"),
    ],
)
def test_unjudgeable_sources_never_read_the_pcm(listing: str, state: str) -> None:
    clock = Clock()
    pcm = Pcm(clock)
    pcm.state = "closed"  # would be a stall if it were looked at
    verdicts = run_monitor(pcm, clock, 8, listing=listing)
    assert pcm.reads == 0
    assert all(v.state == state and v.stalled is not True for v in verdicts)


def test_unreadable_status_is_unknown() -> None:
    monitor = micwatch.StallMonitor(Clock(), lambda pcm: None)
    verdict = monitor.probe(MIC, source_json())
    assert verdict.stalled is None and verdict.state == "unreadable"


def test_a_mute_or_suspend_forgets_the_old_samples() -> None:
    clock = Clock()
    pcm = Pcm(clock)
    pcm.hw_rate = 0
    monitor = micwatch.StallMonitor(clock, pcm)
    for _ in range(6):
        monitor.probe(MIC, source_json())
        clock.advance(1)
    assert monitor.probe(MIC, source_json(mute=True)).stalled is False
    assert monitor.probe(MIC, source_json()).stalled is None  # starts a fresh window, not an instant verdict


# ---------------------------------------------------------------------------
# Re-verification after a revive step
# ---------------------------------------------------------------------------


class Listing:
    def __call__(self, argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str] | None:
        return subprocess.CompletedProcess(argv, 0, source_json(), "")


def test_verify_says_healthy_as_soon_as_both_pointers_move() -> None:
    clock = Clock()
    monitor = micwatch.StallMonitor(clock, Pcm(clock))

    def sleep(seconds: float) -> None:
        clock.advance(seconds)

    assert monitor.verify(MIC, Listing(), sleep) is False
    assert clock.now < 102  # it did not wait out the whole timeout


def test_verify_says_stalled_when_nothing_moves() -> None:
    clock = Clock()
    pcm = Pcm(clock)
    pcm.hw_rate = 0
    monitor = micwatch.StallMonitor(clock, pcm)
    assert monitor.verify(MIC, Listing(), clock.advance) is True


def test_verify_cannot_judge_what_it_cannot_map_or_read() -> None:
    clock = Clock()
    assert micwatch.StallMonitor(clock, lambda p: None).verify(MIC, Listing(), clock.advance) is None
    assert micwatch.StallMonitor(clock, Pcm(clock)).verify(MIC, lambda argv, timeout=10: None, clock.advance) is None


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------


def test_detector_needs_the_condition_to_hold_before_flagging() -> None:
    clock = Clock()
    detector = StallDetector(clock, wall=lambda: 1_700_000_000.0, confirm=5.0, clear=2.0)
    assert detector.update(True) is None
    clock.advance(4.9)
    assert detector.update(True) is None
    assert not detector.stalled
    clock.advance(0.2)
    assert detector.update(True) == "started"
    assert detector.stalled
    assert detector.since is not None and detector.since.startswith("2023-11-1")
    clock.advance(10)
    assert detector.update(True) is None  # flagged once, not on every tick


def test_detector_does_not_flap_on_short_glitches() -> None:
    clock = Clock()
    detector = StallDetector(clock, confirm=5.0, clear=2.0)
    for _ in range(20):  # four seconds bad, one second fine, over and over: never five bad seconds in a row
        for _ in range(4):
            detector.update(True)
            clock.advance(1)
        detector.update(False)
        clock.advance(1)
    assert not detector.stalled


def test_detector_clears_only_after_a_stable_recovery() -> None:
    clock = Clock()
    detector = StallDetector(clock, confirm=1.0, clear=2.0)
    detector.update(True)
    clock.advance(1.5)
    assert detector.update(True) == "started"
    assert detector.update(False) is None
    clock.advance(1.0)
    assert detector.update(True) is None  # a blip of the old condition restarts the clearing
    clock.advance(0.5)
    assert detector.update(False) is None
    clock.advance(1.9)
    assert detector.update(False) is None
    clock.advance(0.2)
    assert detector.update(False) == "cleared"
    assert not detector.stalled and detector.since is None


def test_detector_reset_forgets_everything() -> None:
    clock = Clock()
    detector = StallDetector(clock, confirm=0.0)
    assert detector.update(True) == "started"
    detector.reset()
    assert not detector.stalled and detector.since is None
    assert detector.update(False) is None


# ---------------------------------------------------------------------------
# The health check
# ---------------------------------------------------------------------------


def test_check_item_has_the_doctor_shape() -> None:
    keys = {"id", "group", "state", "title", "detail", "hint"}
    bad = micwatch.check_item(True)
    assert bad is not None and set(bad) == keys
    assert (bad["id"], bad["group"], bad["state"]) == ("mic-stall", "audio", "fail")
    assert "nobody can hear you" in bad["title"] and "mic-revive" in bad["hint"]
    good = micwatch.check_item(False, "scheduled")
    assert good is not None and set(good) == keys and good["state"] == "ok"
    assert micwatch.check_item(None) is None


# ---------------------------------------------------------------------------
# Revive escalation
# ---------------------------------------------------------------------------

SOURCES = json.dumps([{"name": MIC, "properties": {"device.name": "alsa_card.usb-Wave", "alsa.card": "1"}}])
CARDS = json.dumps(
    [
        {"name": "alsa_card.pci-0000", "active_profile": "off", "properties": {"alsa.card": "0"}},
        {
            "name": "alsa_card.usb-Wave",
            "active_profile": "output:analog-stereo+input:mono-fallback",
            "properties": {"alsa.card": "1"},
        },
    ]
)


class Pactl:
    """A fake pactl: records commands, can refuse some, and lists the source again after the profile cycle."""

    def __init__(self, refuse: tuple[str, ...] = (), cards: bool = True) -> None:
        self.commands: list[list[str]] = []
        self.refuse = refuse
        self.cards = cards

    def __call__(self, argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str] | None:
        self.commands.append(argv)
        assert timeout <= 10  # every helper has a deadline
        args = argv[1:]
        code = 1 if any(token in args for token in self.refuse) else 0
        out = ""
        if args[:4] == ["-f", "json", "list", "sources"]:
            out = SOURCES
        elif args[:4] == ["-f", "json", "list", "cards"]:
            out = CARDS if self.cards else "[]"
        elif args == ["get-default-source"]:
            out = MIC + "\n"
        elif args == ["list", "short", "sources"]:
            out = f"1\t{MIC}\tPipeWire\ts24le 1ch 48000Hz\tRUNNING\n"
        return subprocess.CompletedProcess(argv, code, out, "")

    def mutations(self) -> list[list[str]]:
        return [c[1:] for c in self.commands if c[1].startswith(("suspend", "set-"))]


class Sleeper:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def test_revive_step_one_is_enough_when_suspend_resume_works() -> None:
    pactl, sleeper, said = Pactl(), Sleeper(), []
    result = micwatch.revive_mic(MIC, run=pactl, sleep=sleeper, stalled=lambda: False, say=said.append)
    assert result.ok and "suspend and resume" in result.message
    assert result.steps == ["suspend/resume"]
    assert pactl.mutations() == [["suspend-source", MIC, "1"], ["suspend-source", MIC, "0"]]
    assert sleeper.slept[:2] == [0.4, 1.5]  # 0.4 s between suspend and resume, then settle
    assert len(said) == 1


def test_revive_escalates_to_a_card_profile_cycle_and_restores_the_default_source() -> None:
    pactl, sleeper = Pactl(), Sleeper()
    answers = iter([True, False])  # stalled after step 1, healthy after step 2
    said: list[str] = []
    result = micwatch.revive_mic(MIC, run=pactl, sleep=sleeper, stalled=lambda: next(answers), say=said.append)
    assert result.ok and "card profile cycle" in result.message
    assert result.steps == ["suspend/resume", "card profile cycle"]
    profile = "output:analog-stereo+input:mono-fallback"
    assert pactl.mutations() == [
        ["suspend-source", MIC, "1"],
        ["suspend-source", MIC, "0"],
        ["set-card-profile", "alsa_card.usb-Wave", "off"],
        ["set-card-profile", "alsa_card.usb-Wave", profile],  # the profile it had, not a guess
        ["set-default-source", MIC],
    ]
    assert any("profile" in text for text in said)


def test_revive_step_three_asks_for_hands_and_never_restarts_wireplumber() -> None:
    pactl = Pactl()
    result = micwatch.revive_mic(MIC, run=pactl, sleep=Sleeper(), stalled=lambda: True)
    assert not result.ok
    assert result.steps[-1] == "manual help needed"
    assert "replug" in result.message.lower() and "systemctl --user restart wireplumber" in result.message
    assert not any(c[0] in ("systemctl", "pkill", "killall") for c in pactl.commands)


def test_revive_without_a_findable_card_goes_straight_to_the_manual_step() -> None:
    pactl = Pactl(cards=False)
    result = micwatch.revive_mic(MIC, run=pactl, sleep=Sleeper(), stalled=lambda: True)
    assert not result.ok and "card not found" in result.steps
    assert not any(m[0] == "set-card-profile" for m in pactl.mutations())


def test_revive_reports_an_unverifiable_fix_honestly() -> None:
    result = micwatch.revive_mic(MIC, run=Pactl(), sleep=Sleeper(), stalled=lambda: None)
    assert result.ok and "could not be verified" in result.message


def test_revive_stops_when_the_server_refuses() -> None:
    result = micwatch.revive_mic(MIC, run=Pactl(refuse=("suspend-source",)), sleep=Sleeper(), stalled=lambda: True)
    assert not result.ok and "did not accept" in result.message


def test_revive_puts_the_profile_back_even_when_the_first_restore_fails() -> None:
    calls = {"n": 0}

    class Flaky(Pactl):
        def __call__(self, argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str] | None:
            if argv[1:3] == ["set-card-profile", "alsa_card.usb-Wave"] and argv[3] != "off":
                calls["n"] += 1
                self.commands.append(argv)
                return subprocess.CompletedProcess(argv, 1, "", "")
            return super().__call__(argv, timeout)

    result = micwatch.revive_mic(MIC, run=Flaky(), sleep=Sleeper(), stalled=lambda: True)
    assert not result.ok and "card profile" in result.message
    assert calls["n"] == 2  # tried again rather than leaving the card off


def test_find_card_matches_by_device_name_or_alsa_card() -> None:
    assert micwatch.find_card(SOURCES, CARDS, MIC) == ("alsa_card.usb-Wave", "output:analog-stereo+input:mono-fallback")
    by_alsa = json.dumps([{"name": MIC, "properties": {"alsa.card": "1"}}])
    assert micwatch.find_card(by_alsa, CARDS, MIC) is not None
    assert micwatch.find_card(SOURCES, CARDS, "other") is None
    assert micwatch.find_card("junk", CARDS, MIC) is None
    assert micwatch.find_card(SOURCES, "junk", MIC) is None
