"""`streamstack preflight`: asks the running Console, prints the score and the reasons, exit code says ready or not."""

from __future__ import annotations

import json
from typing import Any

import pytest

from streamstack import cli
from streamstack.consolelink import ConsoleRefused, ConsoleUnavailable

CHECKS = [
    {"id": "mic", "title": "Microphone", "state": "warn", "detail": "No voice was heard.", "fix": None},
    {"id": "lights", "title": "Lights", "state": "warn", "detail": "Beam is off.", "fix": {"label": "Turn on"}},
]


class FakeLink:
    def __init__(self, result: dict[str, Any] | None, *, refuse: str | None = None, forever: bool = False) -> None:
        self.result, self.refuse, self.forever = result, refuse, forever
        self.acts: list[tuple[str, dict[str, Any]]] = []
        self.polls = 0

    def act(self, action: str, **params: Any) -> None:
        if self.refuse:
            raise ConsoleRefused(self.refuse)
        self.acts.append((action, params))

    def section(self, name: str) -> dict[str, Any]:
        assert name == "preflight"
        self.polls += 1
        running = self.forever or self.polls < 2
        return {"state": "running" if running else "done", "result": self.result}


def run(monkeypatch: pytest.MonkeyPatch, link: FakeLink | None, *argv: str) -> int:
    monkeypatch.setattr(cli.ConsoleLink, "find", classmethod(lambda cls, timeout=5.0: link))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    args = cli.build_parser().parse_args(["preflight", *argv])
    return args.func(args)


def test_prints_the_score_each_check_and_the_fix_and_exits_1_when_not_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    link = FakeLink({"score": 84, "verdict": "check", "checks": CHECKS})
    assert run(monkeypatch, link) == 1
    out = capsys.readouterr().out
    assert "Pre-flight 84/100: check" in out and "WARN  Microphone: No voice was heard." in out
    assert "fix: Turn on" in out
    assert link.acts == [("preflight.run", {"listen": True})]


def test_json_is_the_result_and_no_listen_skips_the_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    link = FakeLink({"score": 100, "verdict": "ready", "checks": []})
    assert run(monkeypatch, link, "--json", "--no-listen") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["score"] == 100 and "Say your name" not in captured.err
    assert link.acts == [("preflight.run", {"listen": False})]


def test_no_console_is_exit_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(monkeypatch, None) == 2
    assert "streamstack console" in capsys.readouterr().err


def test_a_refusal_is_said_in_the_consoles_words(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(monkeypatch, FakeLink(None, refuse="A pre-flight check is already running.")) == 1
    assert "already running" in capsys.readouterr().err


def test_a_console_that_stops_answering_is_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    link = FakeLink(None)

    def gone(name: str) -> dict[str, Any]:
        raise ConsoleUnavailable("the Console is not answering")

    link.section = gone  # type: ignore[method-assign]
    assert run(monkeypatch, link) == 2


def test_a_check_that_never_finishes_gives_up(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = iter(range(0, 10_000, 5))
    monkeypatch.setattr(cli.time, "monotonic", lambda: float(next(clock)))
    assert run(monkeypatch, FakeLink(None, forever=True)) == 1
    assert "did not finish" in capsys.readouterr().err


def test_status_carries_the_last_score(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli._preflight_line(FakeLink({"score": 92})) == "pre-flight 92/100"  # type: ignore[arg-type]
    assert cli._preflight_line(FakeLink(None)) == ""  # type: ignore[arg-type]
