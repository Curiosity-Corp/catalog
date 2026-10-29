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
    )
    assert [o.result for o in outcomes] == [t.NO_INPUT, t.DUPLEX]
    assert len(results.read_text().splitlines()) == 2
    assert "1 duplex, 1 no-input" in lines[-1]
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
