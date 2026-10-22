"""The lights user service: the unit file, installing it, `streamstack lights`, and `streamstack setup`."""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from streamstack import cli, lightauto, lightplan, lightservice
from streamstack.director import settings

REPO = Path(__file__).resolve().parents[1]


class Systemctl:
    """A fake `systemctl --user`: records the calls, answers like a running user session."""

    def __init__(self, *, active: bool = False, returncode: int = 0, answers: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.active, self.returncode, self.answers = active, returncode, answers

    def __call__(self, cmd: list[str]) -> CompletedProcess[str] | None:
        self.calls.append(cmd)
        if not self.answers:
            return None
        if "is-active" in cmd:
            return CompletedProcess(cmd, 0 if self.active else 3, "active\n" if self.active else "inactive\n", "")
        return CompletedProcess(cmd, self.returncode, "", "Failed to enable unit" if self.returncode else "")


@pytest.fixture(autouse=True)
def no_system_units(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lightservice, "SYSTEM_UNIT_DIRS", ())


def test_the_unit_template_runs_streamstack_lights_run() -> None:
    text = lightservice.render_unit("/opt/ss/bin/streamstack")
    assert "ExecStart=/opt/ss/bin/streamstack lights run" in text
    assert "@STREAMSTACK@" not in text
    assert "[Install]" in text and "WantedBy=graphical-session.target" in text


def test_the_packaged_unit_matches_the_template() -> None:
    packaged = (REPO / "packaging/systemd/user/streamstack-lights.service").read_text()
    assert packaged == lightservice.render_unit("streamstack")


def test_the_makefile_installs_and_removes_the_unit() -> None:
    makefile = (REPO / "Makefile").read_text()
    assert "packaging/systemd/user/*.service" in makefile
    assert "streamstack-lights.service" in makefile.split("uninstall:")[1]


def test_installing_writes_the_unit_once(tmp_path: Path) -> None:
    ctl = Systemctl()
    assert lightservice.install_unit(ctl) == "installed"
    unit = lightservice.user_unit_dir() / lightservice.UNIT
    assert unit.is_file() and "lights run" in unit.read_text()
    assert ["systemctl", "--user", "daemon-reload"] in ctl.calls
    assert lightservice.install_unit(ctl) == "present"
    unit.write_text("[Service]\nExecStart=/old/streamstack lights run\n")
    assert lightservice.install_unit(ctl) == "updated"


def test_a_packaged_unit_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    system_dir = tmp_path / "usr-lib-systemd-user"
    system_dir.mkdir()
    (system_dir / lightservice.UNIT).write_text("[Service]\n")
    monkeypatch.setattr(lightservice, "SYSTEM_UNIT_DIRS", (system_dir,))
    ctl = Systemctl()
    assert lightservice.install_unit(ctl) == "packaged"
    assert not (lightservice.user_unit_dir() / lightservice.UNIT).exists() and ctl.calls == []


def test_an_unwritable_unit_directory_is_a_service_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("file")
    monkeypatch.setattr(lightservice, "user_unit_dir", lambda: blocker / "systemd/user")
    with pytest.raises(lightservice.ServiceError, match="Cannot write"):
        lightservice.install_unit(Systemctl())


def test_enable_installs_then_enables_now() -> None:
    ctl = Systemctl()
    assert lightservice.enable(ctl) == "started"
    assert ["systemctl", "--user", "enable", "--now", lightservice.UNIT] in ctl.calls


def test_enable_and_disable_report_a_failing_or_missing_systemd() -> None:
    with pytest.raises(lightservice.ServiceError, match="Failed to enable"):
        lightservice.enable(Systemctl(returncode=1))
    with pytest.raises(lightservice.ServiceError, match="did not answer"):
        lightservice.enable(Systemctl(answers=False))
    with pytest.raises(lightservice.ServiceError, match="did not answer"):
        lightservice.disable(Systemctl(answers=False))
    ctl = Systemctl()
    assert lightservice.disable(ctl) == "stopped"
    assert ["systemctl", "--user", "disable", "--now", lightservice.UNIT] in ctl.calls


def test_is_active_reads_systemctl() -> None:
    assert lightservice.is_active(Systemctl(active=True))
    assert not lightservice.is_active(Systemctl(active=False))
    assert not lightservice.is_active(Systemctl(answers=False))


# ---- the command ---------------------------------------------------------------------------------------------


def test_lights_enable_and_disable_print_the_outcome(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(lightservice, "enable", lambda run=None: "started")
    monkeypatch.setattr(lightservice, "disable", lambda run=None: "stopped")
    assert cli.main(["lights", "enable"]) == 0
    assert "follow the camera" in capsys.readouterr().out
    assert cli.main(["lights", "disable"]) == 0
    assert "stopped" in capsys.readouterr().out


def test_lights_enable_failure_is_a_clear_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    def refuse(run: object = None) -> str:
        raise lightservice.ServiceError("systemctl --user did not answer. Is a systemd user session running?")

    monkeypatch.setattr(lightservice, "enable", refuse)
    assert cli.main(["lights", "enable"]) == 1
    assert "did not answer" in capsys.readouterr().err


def test_lights_status_without_the_service(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(lightservice, "is_active", lambda run=None: False)
    assert cli.main(["lights"]) == 1
    assert "not running" in capsys.readouterr().out


def test_lights_status_prints_what_the_service_says(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import time

    settings.write_json(
        lightplan.state_path(),
        {
            "running": True,
            "heartbeat": time.time(),
            "where": "service",
            "summary": "Camera in use since 14:02: Teams opened the camera.",
            "camera": {"reason": "Teams opened the camera"},
            "lights": {"c901-a": {"state": "On for the camera session", "held": True}},
        },
    )
    monkeypatch.setattr(lightservice, "is_active", lambda run=None: True)
    assert cli.main(["lights", "status"]) == 0
    out = capsys.readouterr().out
    assert "Teams opened the camera" in out and "held by hand" in out


def test_lights_run_hands_over_to_the_service_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lightauto, "run_service", lambda cfg=None: 0)
    assert cli.main(["lights", "run"]) == 0


# ---- setup -----------------------------------------------------------------------------------------------------


def test_setup_installs_and_starts_the_service_unless_told_not_to(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("STREAMSTACK_SETUP_SERVICE", "on")
    monkeypatch.setattr(lightservice, "install_unit", lambda run=None: "installed")
    started: list[str] = []
    monkeypatch.setattr(lightservice, "enable", lambda run=None: started.append("enable") or "started")
    monkeypatch.setattr(lightservice, "is_active", lambda run=None: False)
    assert cli.main(["setup"]) == 0
    assert started == ["enable"]
    assert "lights service (installed)" in capsys.readouterr().out
    started.clear()
    assert cli.main(["setup", "--no-service"]) == 0
    assert started == []


def test_setup_survives_a_machine_without_systemd(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("STREAMSTACK_SETUP_SERVICE", "on")

    def refuse(run: object = None) -> str:
        raise lightservice.ServiceError("Cannot write /nowhere")

    monkeypatch.setattr(lightservice, "install_unit", refuse)
    assert cli.main(["setup", "--quiet"]) == 0
    assert "lights service not started" in capsys.readouterr().err


def test_the_test_suite_never_touches_the_real_systemd(capsys: pytest.CaptureFixture) -> None:
    assert cli.main(["setup", "--quiet"]) == 0
    assert "lights service" not in capsys.readouterr().err
