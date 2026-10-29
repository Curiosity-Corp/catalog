"""Seat policy (#127): who else runs a WirePlumber on the desk, what `doctor` says, and the fix script.

The loginctl, ps and id texts are recorded on dm313red (tests/fixtures/hardware/seat/dm313red, read-only, 2026-10-27).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from hardware_support import FIXTURES

from streamstack import cli
from streamstack.hardware import doctor, schema, seat
from streamstack.hardware.found import Found
from streamstack.hardware.plan import resolve
from streamstack.hardware.seat import (
    CONTENDS,
    DROPIN_CARDS,
    DROPIN_FEATURES,
    DROPIN_NONE,
    DROPIN_UNKNOWN,
    GUARDED,
    IDLE,
    LINGERING,
    UNVERIFIED,
    Evidence,
    Host,
)

DIR = FIXTURES / "seat" / "dm313red"


def text(name: str) -> str:
    return (DIR / name).read_text(encoding="utf-8")


def fake_run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    answers = {
        ("loginctl", "list-sessions"): text("loginctl-sessions.txt"),
        ("loginctl", "show-seat"): text("loginctl-active-session.txt"),
        ("ps", "-eo"): text("ps.txt"),
        ("id", "-nG"): text("id-seantech.txt") if argv[-1] == "seantech" else text("id-mrh.txt"),
    }
    return subprocess.CompletedProcess(argv, 0, answers[(argv[0], argv[1])], "")


def host(tmp_path: Path, dropins: dict[str, list[str] | None] | None = None) -> Host:
    linger = tmp_path / "linger"
    linger.mkdir(exist_ok=True)
    for name in text("linger.txt").split():
        (linger / name).touch()
    homes = {"mrh": "/home/mrh", "seantech": "/home/seantech"}
    uids = {"mrh": 1000, "seantech": 1001}
    drop = {"seantech": None} if dropins is None else dropins
    return Host(
        run=fake_run,
        which=lambda name: f"/usr/bin/{name}",
        linger_dir=linger,
        me=lambda: "mrh",
        device_groups=lambda: ("audio", "video"),
        home_of=homes.get,
        uid_of=uids.get,
        read_dropins=lambda home: drop.get(home.rsplit("/", 1)[-1], []),
    )


def evidence(
    tmp_path: Path, dropins: dict[str, list[str] | None] | None = None, guarded: tuple[str, ...] = ()
) -> Evidence:
    return seat.collect(guarded, host(tmp_path, dropins))


# -- parsing the recordings --------------------------------------------------------------------------


def test_the_recorded_sessions_give_mrh_the_seat() -> None:
    sessions = seat.parse_sessions(text("loginctl-sessions.txt"))
    assert [(s.id, s.user, s.seat, s.klass) for s in sessions] == [
        ("1", "mrh", "", "manager"),
        ("2", "seantech", "", "manager"),
        ("416", "mrh", "seat0", "user"),
        ("683", "mrh", "", "user"),
        ("686", "mrh", "", "user"),
    ]


def test_the_recorded_processes_show_two_wireplumbers() -> None:
    procs = seat.parse_processes(text("ps.txt"))
    wp = [(p.user, p.comm) for p in procs if p.comm == "wireplumber"]
    assert wp == [("seantech", "wireplumber"), ("mrh", "wireplumber")]
    assert next(p for p in procs if p.user == "seantech" and p.comm == "wireplumber").age_s == 246093


def test_groups_and_sessions_ignore_junk() -> None:
    assert seat.parse_groups("a b  c\n") == ("a", "b", "c")
    assert seat.parse_sessions("garbage\n\n# note\nSESSION UID USER SEAT\n") == ()
    assert seat.parse_processes("not ps output\n 1 root x y bash\n") == ()


# -- the diagnosis on the recorded desk --------------------------------------------------------------


def test_dm313red_as_recorded_the_other_users_wireplumber_contends(tmp_path: Path) -> None:
    ev = evidence(tmp_path)
    assert seat.seat_user(ev) == "mrh"
    diagnosis = seat.diagnose(ev)
    [guest] = diagnosis.guests
    assert guest.user == "seantech" and guest.linger and guest.groups == ("audio", "video")
    assert guest.wireplumber is not None and guest.dropin == DROPIN_UNKNOWN  # mrh cannot read seantech's home
    assert diagnosis.worst == UNVERIFIED


def test_a_readable_home_without_a_drop_in_is_a_confirmed_contender(tmp_path: Path) -> None:
    assert seat.diagnose(evidence(tmp_path, {"seantech": []})).worst == CONTENDS


def test_the_feature_drop_in_guards_the_desk(tmp_path: Path) -> None:
    ev = evidence(tmp_path, {"seantech": [seat.DROPIN_TEXT]})
    assert ev.dropins["seantech"] == DROPIN_FEATURES
    assert seat.diagnose(ev).worst == GUARDED


def test_a_commented_out_drop_in_does_not_count() -> None:
    assert seat.classify_dropins(["# hardware.audio = disabled\n"]) == DROPIN_NONE


def test_the_catalogue_card_rule_guards_the_named_cards(tmp_path: Path) -> None:
    rule = (
        'monitor.alsa.rules = [ { matches = [ { device.name = "~alsa_card.usb-Elgato.*" } ] '
        "actions = { update-props = { device.disabled = true } } } ]"
    )
    ev = evidence(tmp_path, {"seantech": [rule]})
    assert ev.dropins["seantech"] == DROPIN_CARDS and seat.diagnose(ev).worst == GUARDED


def test_the_owner_can_confirm_the_policy(tmp_path: Path) -> None:
    ev = evidence(tmp_path, guarded=("seantech",))
    assert seat.diagnose(ev).worst == GUARDED
    assert seat.parse_guarded("seantech, bob  carol") == {"seantech", "bob", "carol"}


def test_a_user_in_no_device_group_cannot_open_the_cards(tmp_path: Path) -> None:
    ev = replace(evidence(tmp_path), groups={"seantech": ("adm", "sudo")})
    [guest] = seat.diagnose(ev).guests
    assert not guest.has_device_access and guest.verdict == GUARDED


def test_linger_and_groups_without_a_running_wireplumber_still_warn(tmp_path: Path) -> None:
    ev = evidence(tmp_path)
    ev = replace(ev, processes=tuple(p for p in ev.processes if p.user != "seantech"))
    [guest] = seat.diagnose(ev).guests
    assert guest.verdict == LINGERING


def test_a_user_with_nothing_running_and_no_linger_is_not_a_guest(tmp_path: Path) -> None:
    ev = evidence(tmp_path)
    ev = replace(ev, linger=frozenset({"mrh"}), processes=tuple(p for p in ev.processes if p.user == "mrh"))
    diagnosis = seat.diagnose(ev)
    assert diagnosis.guests == () and diagnosis.worst == IDLE and seat.rows(diagnosis) == []


def test_a_single_user_desk_says_nothing(tmp_path: Path) -> None:
    ev = evidence(tmp_path)
    ev = replace(ev, linger=frozenset({"mrh"}), processes=tuple(p for p in ev.processes if p.user == "mrh"))
    assert seat.rows(seat.diagnose(ev)) == []


def test_the_seat_user_is_the_active_session_user_not_the_caller() -> None:
    sessions = (seat.Session("1", 1001, "bob", "seat0", "user"), seat.Session("2", 1000, "mrh", "seat0", "user"))
    assert seat.seat_user(Evidence(me="mrh", sessions=sessions, active_session="2")) == "mrh"
    assert seat.seat_user(Evidence(me="mrh", sessions=sessions, active_session="1")) == "bob"
    assert seat.seat_user(Evidence(me="mrh")) == "mrh"


# -- the doctor rows ---------------------------------------------------------------------------------


def test_rows_name_the_contender_and_print_the_fix_command(tmp_path: Path) -> None:
    [row] = seat.rows(seat.diagnose(evidence(tmp_path, {"seantech": []})))
    assert row["id"] == "hw-seat" and row["group"] == "hardware" and row["state"] == "warn"
    assert "seantech" in row["title"] and "second WirePlumber" in row["title"]
    assert "linger on" in row["detail"] and "wireplumber up 2 d 20 h" in row["detail"]
    assert "streamstack seat-policy script --user seantech" in row["hint"]


def test_rows_for_an_unreadable_home_name_the_setting(tmp_path: Path) -> None:
    [row] = seat.rows(seat.diagnose(evidence(tmp_path)))
    assert row["state"] == "warn" and "STREAMSTACK_SEAT_GUARDED=seantech" in row["hint"]


def test_rows_are_green_once_guarded(tmp_path: Path) -> None:
    [row] = seat.rows(seat.diagnose(evidence(tmp_path, {"seantech": [seat.DROPIN_TEXT]})))
    assert row["state"] == "ok" and row["hint"] == ""


def test_the_doctor_carries_the_row_only_when_given_evidence(tmp_path: Path) -> None:
    catalogue = schema.packaged()
    from hardware_support import FakeMachine, studio_wave

    snapshot = studio_wave(FakeMachine(tmp_path / "m")).snapshot()
    found = Found(resolve(snapshot, catalogue, {"STREAMSTACK_RIG": "studio-wave-xlr"}), snapshot, catalogue)
    without = [r["id"] for r in doctor.checks(found.plan, found.snapshot, found.catalogue)]
    withit = [r["id"] for r in doctor.checks(found.plan, found.snapshot, found.catalogue, seats=evidence(tmp_path))]
    assert "hw-seat" not in without and withit.count("hw-seat") == 1
    assert cli.summarize(doctor.checks(found.plan, found.snapshot, found.catalogue, seats=evidence(tmp_path))) in (
        "warn",
        "fail",
    )


# -- collecting, failing alone -----------------------------------------------------------------------


def test_a_missing_tool_is_a_note_not_a_crash(tmp_path: Path) -> None:
    base = host(tmp_path)
    base.which = lambda name: None if name == "loginctl" else f"/usr/bin/{name}"
    ev = seat.collect((), base)
    assert any("loginctl is not installed" in e for e in ev.errors)
    assert seat.seat_user(ev) == "mrh"  # falls back to the caller


def test_a_failing_and_a_hung_tool_are_notes(tmp_path: Path) -> None:
    base = host(tmp_path)

    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
        if argv[0] == "ps":
            return None
        if argv[0] == "id":
            return subprocess.CompletedProcess(argv, 1, "", "id: 'x': no such user\n")
        return fake_run(argv, timeout)

    base.run = run
    ev = seat.collect((), base)
    assert "ps did not answer" in ev.errors and any(e.startswith("id failed") for e in ev.errors)


def test_the_real_collector_never_raises_on_this_machine() -> None:
    ev = seat.collect()
    assert isinstance(ev, Evidence) and ev.me
    seat.rows(seat.diagnose(ev))


def test_read_dropin_files_tells_closed_from_empty(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert seat.read_dropin_files(str(home)) == []  # no config at all: readable, empty
    conf = home / seat.DROPIN_DIR
    conf.mkdir(parents=True)
    (conf / "a.conf").write_text("hardware.audio = disabled\n")
    assert seat.classify_dropins(seat.read_dropin_files(str(home)) or []) == DROPIN_FEATURES


# -- the fix script ----------------------------------------------------------------------------------


def test_the_dropin_script_writes_the_file_restarts_only_their_wireplumber_and_says_how_to_undo(tmp_path: Path) -> None:
    script = seat.fix_script("seantech", "dropin", evidence(tmp_path))
    assert script.startswith("#!/bin/sh") and "set -eu" in script
    assert "/home/seantech/.config/wireplumber/wireplumber.conf.d/90-streamstack-seat-guest.conf" in script
    assert "hardware.audio = disabled" in script and "hardware.video-capture = disabled" in script
    assert "sudo -u seantech env XDG_RUNTIME_DIR=/run/user/1001" in script
    assert "Undo: rm /home/seantech" in script
    assert "mrh" in script.splitlines()[1]  # names the person at the seat
    assert "uid 1000" not in script  # never touches the seat user's services


def test_the_groups_script_removes_only_the_device_groups_they_hold(tmp_path: Path) -> None:
    script = seat.fix_script("seantech", "groups", evidence(tmp_path))
    assert "gpasswd -d seantech audio" in script and "gpasswd -d seantech video" in script
    assert "gpasswd -d seantech render" not in script and "gpasswd -d seantech sudo" not in script
    assert "systemctl restart user@1001.service" in script
    assert "Undo: gpasswd -a seantech audio; gpasswd -a seantech video" in script


def test_the_stop_script_masks_their_wireplumber(tmp_path: Path) -> None:
    script = seat.fix_script("seantech", "stop", evidence(tmp_path))
    assert "systemctl --user stop wireplumber.service" in script and "mask wireplumber.service" in script
    assert "unmask" in script


def test_the_script_refuses_the_seat_user_an_unknown_user_and_an_unknown_option(tmp_path: Path) -> None:
    ev = evidence(tmp_path)
    for user, option in (("mrh", "dropin"), ("nobody-here", "dropin"), ("seantech", "format-disk")):
        with pytest.raises(seat.SeatError):
            seat.fix_script(user, option, ev)


def test_the_dropin_text_is_the_wireplumber_0_5_profile_override() -> None:
    body = [line for line in seat.DROPIN_TEXT.splitlines() if not line.startswith("#")]
    assert body == [
        "wireplumber.profiles = {",
        "  main = {",
        "    hardware.audio = disabled",
        "    hardware.video-capture = disabled",
        "  }",
        "}",
    ]
    assert seat.classify_dropins([seat.DROPIN_TEXT]) == DROPIN_FEATURES


# -- the command line --------------------------------------------------------------------------------


def run_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str) -> int:
    monkeypatch.setattr(seat, "Host", lambda: host(tmp_path))
    return cli.main(["seat-policy", *argv])


def test_the_command_prints_status_and_a_script_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, tmp_path, "status") == 0  # unverified home: warns but does not fail
    out = capsys.readouterr().out
    assert "[unverified] seantech:" in out and "recommended: dropin" in out
    assert run_cli(monkeypatch, tmp_path, "script", "--user", "seantech", "--option", "groups") == 0
    assert "gpasswd -d seantech audio" in capsys.readouterr().out
    assert run_cli(monkeypatch, tmp_path, "script") == 2
    assert run_cli(monkeypatch, tmp_path, "script", "--user", "mrh") == 1
    assert "person at the seat" in capsys.readouterr().err
    assert run_cli(monkeypatch, tmp_path, "status", "--json") == 0
    assert '"recommended": "dropin"' in capsys.readouterr().out


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a closed directory")
def test_a_closed_home_is_unknown_not_empty(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / seat.DROPIN_DIR).mkdir(parents=True)
    home.chmod(0o000)
    try:
        assert seat.read_dropin_files(str(home)) is None
    finally:
        home.chmod(0o755)


def test_current_user_survives_a_uid_with_no_passwd_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(uid: int) -> None:
        raise KeyError(f"getpwuid(): uid not found: {uid}")

    monkeypatch.setattr(seat.pwd, "getpwuid", missing)
    monkeypatch.setenv("USER", "ci")
    assert seat.current_user() == "ci"
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    assert seat.current_user() == str(seat.os.getuid())
