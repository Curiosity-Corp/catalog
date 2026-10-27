"""The deck's systemd user unit and `streamstack deck`."""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from streamstack import cli, deckservice
from streamstack.deck import status

REPO = Path(__file__).resolve().parents[1]


class Systemctl:
    def __init__(self, *, active: bool = False, answers: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.active, self.answers = active, answers

    def __call__(self, cmd: list[str]) -> CompletedProcess[str] | None:
        self.calls.append(cmd)
        if not self.answers:
            return None
        if "is-active" in cmd:
            return CompletedProcess(cmd, 0 if self.active else 3, "active\n" if self.active else "inactive\n", "")
        return CompletedProcess(cmd, 0, "", "")


@pytest.fixture(autouse=True)
def no_system_units(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deckservice, "SYSTEM_UNIT_DIRS", ())


def test_the_unit_runs_streamstack_deck_run() -> None:
    text = deckservice.render_unit("/opt/ss/bin/streamstack")
    assert "ExecStart=/opt/ss/bin/streamstack deck run" in text and "@STREAMSTACK@" not in text


def test_the_packaged_unit_matches_the_template_and_the_makefile_removes_it() -> None:
    packaged = (REPO / "packaging/systemd/user/streamstack-deck.service").read_text()
    assert packaged == deckservice.render_unit("streamstack")
    assert "streamstack-deck.service" in (REPO / "Makefile").read_text().split("uninstall:")[1]


def test_installing_writes_the_unit_once() -> None:
    ctl = Systemctl()
    assert deckservice.install_unit(ctl) == "installed"
    assert deckservice.install_unit(ctl) == "present"
    assert ["systemctl", "--user", "daemon-reload"] in ctl.calls


def test_enable_disable_and_is_active() -> None:
    ctl = Systemctl()
    assert deckservice.enable(ctl) == "started"
    assert ["systemctl", "--user", "enable", "--now", deckservice.UNIT] in ctl.calls
    assert deckservice.disable(ctl) == "stopped"
    assert deckservice.is_active(Systemctl(active=True)) and not deckservice.is_active(Systemctl())
    with pytest.raises(deckservice.ServiceError, match="did not answer"):
        deckservice.enable(Systemctl(answers=False))


def test_deck_enable_and_disable_print_the_outcome(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(deckservice, "enable", lambda run=None: "started")
    monkeypatch.setattr(deckservice, "disable", lambda run=None: "stopped")
    assert cli.main(["deck", "enable"]) == 0
    assert "Stream Deck" in capsys.readouterr().out
    assert cli.main(["deck", "disable"]) == 0
    assert "stopped" in capsys.readouterr().out


def test_deck_enable_failure_is_a_clear_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    def refuse(run: object = None) -> str:
        raise deckservice.ServiceError("systemctl --user did not answer.")

    monkeypatch.setattr(deckservice, "enable", refuse)
    assert cli.main(["deck", "enable"]) == 1
    assert "did not answer" in capsys.readouterr().err


def test_deck_status_reads_the_console(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(deckservice, "is_active", lambda run=None: True)
    monkeypatch.setattr(status, "console_deck", lambda: None)
    assert cli.main(["deck"]) == 1
    assert "Console is not running" in capsys.readouterr().out
    connected = {
        "device": {"connected": True, "name": "Stream Deck XL", "rows": 4, "cols": 8},
        "surface": {"page": {"name": "Live"}},
    }
    monkeypatch.setattr(status, "console_deck", lambda: connected)
    assert cli.main(["deck", "status"]) == 0
    out = capsys.readouterr().out
    assert "Stream Deck XL" in out and "Live" in out
    monkeypatch.setattr(
        status, "console_deck", lambda: {"device": {"connected": False, "error": "Another app has it."}}
    )
    assert cli.main(["deck", "status"]) == 1
    assert "Another app has it." in capsys.readouterr().out


def test_deck_run_without_the_extra_explains(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    from streamstack.deck import service

    monkeypatch.setattr(service, "run", lambda autostart=True: 2)
    assert cli.main(["deck", "run", "--no-autostart"]) == 2
