"""The seat-policy replug trial (tools/seat_replug_trial.py, issue #127): guards, classification and the cycle loop,
all with a fake runner and a fake sysfs. Nothing is replugged and no PipeWire is touched."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "tools" / "seat_replug_trial.py"
    spec = importlib.util.spec_from_file_location("seat_replug_trial", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["seat_replug_trial"] = module
    spec.loader.exec_module(module)
    return module


t = _load()

CARDS_DUPLEX = json.dumps([{"name": "alsa_card.usb-Elgato_Wave_XLR-00", "active_profile": "pro-audio"}])
CARDS_OUTPUT_ONLY = json.dumps([{"name": "alsa_card.usb-Elgato_Wave_XLR-00", "active_profile": "output:analog-stereo"}])
SOURCES = "5\talsa_input.usb-Elgato_Wave_XLR-00.mono-fallback\tPipeWire\n6\tstreamstack_mic\tPipeWire\n"


def cp(argv: list[str], out: str = "", rc: int = 0, err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, rc, out, err)


class Desk:
    """A runner that answers like an idle desk; tests override pieces."""

    def __init__(
        self, outputs: str = "", fuser: str = "", default: str = "streamstack_mic", cards: list[str] | None = None
    ):
        self.outputs, self.fuser, self.default = outputs, fuser, default
        self.cards = list(cards or [CARDS_DUPLEX])
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[:3] == ["pactl", "list", "source-outputs"]:
            return cp(argv, self.outputs)
        if argv[0] == "fuser":
            return cp(argv, self.fuser, 1 if not self.fuser else 0)
        if argv[:2] == ["pactl", "get-default-source"]:
            return cp(argv, self.default + "\n")
        if argv[:4] == ["pactl", "-f", "json", "list"]:
            return cp(argv, self.cards.pop(0) if len(self.cards) > 1 else self.cards[0])
        if argv[:3] == ["pactl", "list", "short"]:
            return cp(argv, SOURCES)
        if argv[0] == "id":
            return cp(argv, "1001\n")
        return cp(argv)


def fake_sysfs(tmp_path: Path) -> Path:
    dev = tmp_path / "1-3"
    dev.mkdir()
    (dev / "idVendor").write_text("0fd9\n")
    (dev / "idProduct").write_text("007d\n")
    (dev / "authorized").write_text("1\n")
    (tmp_path / "1-3:1.0").mkdir()
    return tmp_path


def args(**kw: object) -> argparse.Namespace:
    base = {
        "cycles": 2,
        "label": "x",
        "usb": "0fd9:007d",
        "card_match": "Wave_XLR",
        "source_match": "Wave_XLR",
        "wait": 3.0,
        "settle": 0.0,
        "stop_other_wireplumber": None,
        "results": None,
        "execute": True,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_call_apps_found_in_source_outputs() -> None:
    text = '\t\tapplication.name = "Microsoft Edge"\n\t\tapplication.process.binary = "msedge"\n\t\tmedia.name = "x"\n'
    assert t.call_apps_in(text) == ["microsoft edge", "msedge"]
    assert t.call_apps_in('\t\tapplication.name = "streamstack-engine"\n') == []


def test_guard_blocks_on_call_and_loopback() -> None:
    assert t.guard(Desk()) == []
    reasons = t.guard(Desk(outputs='application.process.binary = "zoom"', fuser="4242"))
    assert any("zoom" in r for r in reasons) and any("4242" in r for r in reasons)


def test_guard_ignores_streamstacks_own_loopback_users(tmp_path: Path) -> None:
    for pid, cgroup in (
        (11, "0::/user.slice/user-1000.slice/user@1000.service/app.slice/streamstack-console.service\n"),
        (12, "0::/user.slice/user-1000.slice/user@1000.service/app.slice/canonctl-ptp-webcam.service\n"),
        (13, "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-chrome-1234.scope\n"),
    ):
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "cgroup").write_text(cgroup)
    own = lambda pid: t.own_pid(pid, tmp_path)  # noqa: E731
    assert t.guard(Desk(fuser="11 12"), own) == []
    reasons = t.guard(Desk(fuser="11 12 13 99"), own)
    assert reasons == ["/dev/video42 and /dev/video43 are open (pids 13, 99)"]


@pytest.mark.parametrize(
    ("cards", "result"),
    [(CARDS_DUPLEX, t.DUPLEX), (CARDS_OUTPUT_ONLY, t.NO_INPUT), ("[]", t.NO_CARD), ("not json", t.NO_CARD)],
)
def test_classify(cards: str, result: str) -> None:
    assert t.classify(cards, "Wave_XLR", SOURCES, "Wave_XLR")[0] == result


def test_classify_duplex_profile_without_source_is_no_input() -> None:
    assert t.classify(CARDS_DUPLEX, "Wave_XLR", "6\tstreamstack_mic\tPipeWire\n", "Wave_XLR")[0] == t.NO_INPUT


def test_find_usb_skips_interfaces_and_reports_missing(tmp_path: Path) -> None:
    root = fake_sysfs(tmp_path)
    assert t.find_usb("0fd9:007d", root).name == "1-3"
    with pytest.raises(t.TrialError):
        t.find_usb("1235:8219", root)


def test_dry_run_touches_nothing(tmp_path: Path) -> None:
    root = fake_sysfs(tmp_path)
    lines: list[str] = []
    out = t.trial(args(execute=False), Desk(), sysfs=root, out=lines.append)
    assert out == [] and (root / "1-3" / "authorized").read_text() == "1\n"
    assert "dry run" in lines[0]


def test_refuses_during_a_call_and_on_wrong_default_source(tmp_path: Path) -> None:
    root = fake_sysfs(tmp_path)
    with pytest.raises(t.TrialError, match="not idle"):
        t.trial(args(), Desk(outputs='application.name = "teams"'), sysfs=root, out=lambda s: None)
    with pytest.raises(t.TrialError, match="never changes"):
        t.trial(args(), Desk(default="other"), sysfs=root, out=lambda s: None)
    assert (root / "1-3" / "authorized").read_text() == "1\n"


HEALTHY = t.micwatch.StallProbe(False, "healthy", "the microphone is captured and consumed")
FROZEN = t.micwatch.StallProbe(
    True, "stalled", "the device's hw_ptr is not advancing: the microphone delivers no audio", "stall"
)
NO_SAMPLES = t.micwatch.StallProbe(True, "stalled", "the source is running but a capture received no samples", "stall")
WAVE_SOURCE = "alsa_input.usb-Elgato_Wave_XLR-00.mono-fallback"


def test_source_name_is_the_capture_source_never_a_monitor() -> None:
    sources = "4\talsa_output.usb-Elgato_Wave_XLR-00.analog-stereo.monitor\tPipeWire\n" + SOURCES
    assert t.source_name(sources, "Wave_XLR") == WAVE_SOURCE
    assert t.source_name("6\tstreamstack_mic\tPipeWire\n", "Wave_XLR") is None


@pytest.mark.parametrize(
    ("probe", "result"),
    [
        (HEALTHY, t.DUPLEX),
        (FROZEN, t.STALLED),
        (NO_SAMPLES, t.STALLED),
        (t.micwatch.StallProbe(True, "digital-silence", "every sample is zero", "silence"), t.STALLED),
        (t.micwatch.StallProbe(None, "unsupported", "not an ALSA device"), t.DUPLEX),
    ],
)
def test_a_duplex_card_is_judged_by_the_doctors_stall_check(probe: Any, result: str) -> None:
    seen: list[str] = []

    def check(source: str) -> Any:
        seen.append(source)
        return probe

    got, _, detail = t.wait_for_outcome(Desk(), "Wave_XLR", "Wave_XLR", 3.0, sleep=lambda s: None, check=check)
    assert got == result and seen == [WAVE_SOURCE]
    assert bool(detail) == (result == t.STALLED)


def test_no_input_cards_are_never_stall_checked() -> None:
    def check(source: str) -> Any:
        raise AssertionError("the card has no input; there is nothing to check")

    ticks = iter(range(100))
    got = t.wait_for_outcome(
        Desk(cards=[CARDS_OUTPUT_ONLY]),
        "Wave_XLR",
        "Wave_XLR",
        2.0,
        clock=lambda: float(next(ticks)),
        sleep=lambda s: None,
        check=check,
    )
    assert got[0] == t.NO_INPUT


def test_a_stalled_cycle_is_counted_written_and_fails_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 2026-11-04 trial: every cycle had the duplex profile and the source, yet the second one was frozen."""
    root = fake_sysfs(tmp_path)
    results = tmp_path / "r.jsonl"
    probes = iter([HEALTHY, FROZEN])
    lines: list[str] = []
    outcomes = t.trial(
        args(cycles=2, wait=0.0, results=str(results)),
        Desk(),
        sleep=lambda s: None,
        sysfs=root,
        out=lines.append,
        check=lambda source: next(probes),
    )
    assert [o.result for o in outcomes] == [t.DUPLEX, t.STALLED]
    written = [json.loads(line) for line in results.read_text().splitlines()]
    assert written[1]["result"] == "stalled" and "hw_ptr" in written[1]["detail"]
    assert "1 duplex, 1 stalled, 0 no-input" in lines[-1]
    monkeypatch.setattr(t, "trial", lambda a: outcomes)
    assert t.main(["--cycles", "2"]) == 1


def test_cycles_are_counted_and_results_written(tmp_path: Path) -> None:
    root = fake_sysfs(tmp_path)
    results = tmp_path / "r.jsonl"
    desk = Desk(cards=[CARDS_OUTPUT_ONLY, CARDS_DUPLEX])
    clock = iter(range(0, 1000)).__next__
    lines: list[str] = []
    outcomes = t.trial(
        args(cycles=2, wait=0.0, results=str(results)),
        desk,
        sleep=lambda s: None,
        clock=lambda: float(clock()),
        sysfs=root,
        out=lines.append,
        check=lambda source: HEALTHY,
    )
    assert [o.result for o in outcomes] == [t.NO_INPUT, t.DUPLEX]
    assert len(results.read_text().splitlines()) == 2
    assert "1 duplex, 0 stalled, 1 no-input" in lines[-1]
    assert (root / "1-3" / "authorized").read_text() == "1\n"
    assert not any(c[:2] == ["pactl", "set-default-source"] for c in desk.calls)


def test_stop_other_wireplumber_is_restarted_even_when_a_call_starts(tmp_path: Path) -> None:
    root = fake_sysfs(tmp_path)

    class CallStarts(Desk):
        def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["pactl", "list", "source-outputs"] and sum(c[:3] == argv[:3] for c in self.calls) >= 1:
                self.calls.append(argv)
                return cp(argv, 'application.name = "zoom"')
            return super().__call__(argv)

    desk = CallStarts()
    with pytest.raises(t.TrialError, match="stopped before cycle 1"):
        t.trial(args(stop_other_wireplumber="seantech"), desk, sleep=lambda s: None, sysfs=root, out=lambda s: None)
    systemctl = [c[-3:] for c in desk.calls if "systemctl" in c]
    assert systemctl == [["--user", "stop", "wireplumber.service"], ["--user", "start", "wireplumber.service"]]


def test_soft_replug_needs_root(tmp_path: Path) -> None:
    dev = tmp_path / "1-3"
    dev.mkdir()
    (dev / "authorized").write_text("1\n")
    dev.chmod(0o555)
    (dev / "authorized").chmod(0o444)
    try:
        if Path("/proc/self").stat().st_uid == 0:
            pytest.skip("root can write anything")
        with pytest.raises(t.TrialError, match="sudo"):
            t.soft_replug(dev, sleep=lambda s: None)
    finally:
        dev.chmod(0o755)


def test_main_rejects_zero_cycles() -> None:
    assert t.main(["--cycles", "0"]) == 2
