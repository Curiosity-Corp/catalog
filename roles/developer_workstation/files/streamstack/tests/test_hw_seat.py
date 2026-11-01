"""Seat policy (#127): who else runs a WirePlumber on the desk, what `doctor` says, and the fix script.

The loginctl, ps and id texts are recorded on dm313red (tests/fixtures/hardware/seat/dm313red, read-only, 2026-10-27).
"""

from __future__ import annotations

import os
import shlex
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
    DROPIN_AUDIO,
    DROPIN_CARDS,
    DROPIN_FEATURES,
    DROPIN_NONE,
    DROPIN_UNKNOWN,
    GUARDED,
    IDLE,
    LINGERING,
    RESTART_PENDING,
    UNVERIFIED,
    Evidence,
    Host,
    Marker,
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


NOW = 1_800_000_000.0  # the clock of the recorded desk; seantech's wireplumber is up 246093 s at this moment
STARTED = NOW - 246093


def host(
    tmp_path: Path,
    dropins: dict[str, list[str] | None] | None = None,
    markers: dict[str, Marker] | None = None,
    fds: dict[int, int | None] | None = None,
) -> Host:
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
        read_markers=lambda: dict(markers or {}),
        count_device_fds=lambda pid: (fds or {}).get(pid),  # a pid the test did not describe is unreadable
        now=lambda: NOW,
    )


def evidence(
    tmp_path: Path,
    dropins: dict[str, list[str] | None] | None = None,
    guarded: tuple[str, ...] = (),
    markers: dict[str, Marker] | None = None,
    fds: dict[int, int | None] | None = None,
) -> Evidence:
    return seat.collect(guarded, host(tmp_path, dropins, markers, fds))


def marker(written: float, user: str = "seantech") -> dict[str, Marker]:
    return {user: Marker(user, seat.DROPIN_NAME, written)}


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
    (conf / "a.conf").write_text("hardware.audio = disabled\nhardware.video-capture = disabled\n")
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
        "    monitor.alsa = disabled",
        "    monitor.alsa-midi = disabled",
        "    monitor.v4l2 = disabled",
        "    monitor.libcamera = disabled",
        "  }",
        "}",
    ]
    assert seat.classify_dropins([seat.DROPIN_TEXT]) == DROPIN_FEATURES


# -- the catalog marker and what the kernel says (#127, owner decision 2026-11-01) ---------------------


def test_a_drop_in_for_audio_only_leaves_the_cameras_open(tmp_path: Path) -> None:
    assert seat.classify_dropins(["monitor.alsa = disabled\n"]) == DROPIN_AUDIO
    assert seat.classify_dropins(["hardware.audio = disabled\nmonitor.v4l2 = disabled\n"]) == DROPIN_FEATURES
    assert seat.classify_dropins(["monitor.alsa = disabled\nhardware.video-capture = disabled\n"]) == DROPIN_FEATURES
    assert seat.classify_dropins(["hardware.video-capture = disabled\n"]) == DROPIN_NONE
    ev = evidence(tmp_path, {"seantech": ["hardware.audio = disabled\n"]})
    assert ev.dropins["seantech"] == DROPIN_AUDIO and seat.diagnose(ev).worst == CONTENDS


def test_the_catalog_marker_satisfies_the_policy_when_the_home_is_unreadable(tmp_path: Path) -> None:
    ev = evidence(tmp_path, markers=marker(STARTED - 3600))  # installed an hour before seantech's wireplumber started
    [guest] = seat.diagnose(ev).guests
    assert guest.dropin == DROPIN_UNKNOWN and guest.marker is not None
    assert guest.verdict == GUARDED and not guest.restart_pending
    [row] = seat.rows(seat.diagnose(ev))
    assert row["state"] == "ok" and row["hint"] == ""
    assert "second WirePlumber" not in row["title"] and "out of sight" not in row["title"]
    assert "catalog marker says 90-streamstack-seat-guest.conf is installed" in row["detail"]


def test_a_marker_newer_than_their_wireplumber_asks_for_a_restart(tmp_path: Path) -> None:
    ev = evidence(tmp_path, markers=marker(NOW - 600))  # installed ten minutes ago; wireplumber is days old
    [guest] = seat.diagnose(ev).guests
    assert guest.restart_pending and guest.verdict == RESTART_PENDING
    [row] = seat.rows(seat.diagnose(ev))
    assert row["state"] == "warn" and "still runs the old config" in row["title"]
    assert "sudo systemctl --user -M seantech@ restart wireplumber" in row["hint"] and "never during" in row["hint"]


def test_a_marker_written_within_the_slack_of_the_start_is_not_pending(tmp_path: Path) -> None:
    ev = evidence(tmp_path, markers=marker(STARTED + 2))
    assert seat.diagnose(ev).worst == GUARDED


def test_a_marker_without_a_running_wireplumber_is_guarded(tmp_path: Path) -> None:
    ev = evidence(tmp_path, markers=marker(NOW - 60))
    ev = replace(ev, processes=tuple(p for p in ev.processes if p.user != "seantech"))
    [guest] = seat.diagnose(ev).guests
    assert guest.wireplumber is None and guest.verdict == GUARDED  # linger starts it with the drop-in in place


def test_a_marker_for_another_user_does_not_guard_seantech(tmp_path: Path) -> None:
    ev = evidence(tmp_path, markers=marker(STARTED - 60, user="bob"))
    assert seat.diagnose(ev).worst == UNVERIFIED


def test_a_device_node_held_open_beats_any_marker_or_drop_in(tmp_path: Path) -> None:
    ev = evidence(
        tmp_path,
        {"seantech": [seat.DROPIN_TEXT]},
        markers=marker(STARTED - 60),
        fds={4385: 2, 4355: 0, 4388: 0, 4390: 0},
    )
    [guest] = seat.diagnose(ev).guests
    assert guest.device_fds == 2 and guest.verdict == CONTENDS
    [row] = seat.rows(seat.diagnose(ev))
    assert row["state"] == "warn" and "hold 2 sound or video device node(s)" in row["detail"]


def test_no_device_node_held_open_is_inferred_as_satisfied_when_the_config_is_unreadable(tmp_path: Path) -> None:
    ev = evidence(
        tmp_path, fds={4355: 0, 4385: 0, 4388: 0, 4390: 0}
    )  # root can list seantech's descriptors; none is a device
    [guest] = seat.diagnose(ev).guests
    assert guest.device_fds == 0 and guest.dropin == DROPIN_UNKNOWN and guest.marker is None
    assert guest.verdict == GUARDED
    assert "hold no sound or video device node" in seat.describe(guest)


def test_no_held_node_does_not_excuse_a_readable_home_without_a_drop_in(tmp_path: Path) -> None:
    ev = evidence(tmp_path, {"seantech": []}, fds={4355: 0, 4385: 0, 4388: 0, 4390: 0})
    assert seat.diagnose(ev).worst == CONTENDS  # nothing opens a card now, but nothing stops the next replug either


def test_one_unreadable_process_makes_the_count_unknown(tmp_path: Path) -> None:
    ev = evidence(tmp_path, fds={4385: 0, 4388: 0, 4390: 0})  # pid 4355's descriptors are not readable
    assert ev.device_fds["seantech"] is None
    assert seat.diagnose(ev).worst == UNVERIFIED


def test_read_markers_keeps_only_root_owned_well_formed_files(tmp_path: Path) -> None:
    d = tmp_path / "seat-guarded.d"
    d.mkdir()
    (d / "seantech").write_text("# note\nuser=seantech\ndropin=90-streamstack-seat-guest.conf\nsha256=ab\n")
    (d / "bob").write_text("user=alice\ndropin=90-streamstack-seat-guest.conf\n")  # name and content disagree
    (d / "carol").write_text("sha256=ab\n")  # no drop-in named
    (d / "sub").mkdir()
    found = seat.read_markers(d, owner_uid=os.getuid())
    assert list(found) == ["seantech"] and found["seantech"].dropin == "90-streamstack-seat-guest.conf"
    assert found["seantech"].mtime == (d / "seantech").stat().st_mtime
    assert seat.read_markers(d, owner_uid=os.getuid() + 1) == {}  # somebody else's files are not the catalog's
    assert seat.read_markers(tmp_path / "missing") == {}


def test_count_device_fds_reads_proc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    targets = {
        "0": "/dev/null",
        "1": "/dev/snd/controlC2",
        "2": "/dev/video42",
        "3": "socket:[1]",
        "4": "/dev/snd/pcmC2D0c",
    }
    monkeypatch.setattr(seat.os, "listdir", lambda p: list(targets))

    def readlink(path: object) -> str:
        name = Path(str(path)).name
        if name == "4":
            raise FileNotFoundError  # closed meanwhile
        return targets[name]

    monkeypatch.setattr(seat.os, "readlink", readlink)
    assert seat.count_device_fds(1234) == 2

    def denied(p: object) -> list[str]:
        raise PermissionError

    monkeypatch.setattr(seat.os, "listdir", denied)
    assert seat.count_device_fds(1234) is None

    def gone(p: object) -> list[str]:
        raise FileNotFoundError

    monkeypatch.setattr(seat.os, "listdir", gone)
    assert seat.count_device_fds(1234) == 0


def test_the_real_collector_reads_the_marker_dir_and_proc_without_raising() -> None:
    assert isinstance(seat.read_markers(), dict)
    assert seat.count_device_fds(os.getpid()) is not None  # our own descriptors are always readable


def test_the_dropin_script_also_writes_the_marker_the_doctor_reads(tmp_path: Path) -> None:
    script = seat.fix_script("seantech", "dropin", evidence(tmp_path))
    assert (
        "monitor.alsa = disabled" in script
        and "monitor.v4l2 = disabled" in script
        and "monitor.libcamera = disabled" in script
    )
    assert "install -d -o root -g root -m 0755 /etc/streamstack/seat-guarded.d" in script
    assert "dropin=%s\\n' seantech 90-streamstack-seat-guest.conf > /etc/streamstack/seat-guarded.d/seantech" in script
    undo = "Undo: rm /home/seantech/.config/wireplumber/wireplumber.conf.d/90-streamstack-seat-guest.conf"
    assert undo + " /etc/streamstack/seat-guarded.d/seantech" in script


def test_the_script_marker_round_trips_through_the_reader(tmp_path: Path) -> None:
    script = seat.fix_script("seantech", "dropin", evidence(tmp_path))
    [line] = [ln for ln in script.splitlines() if ln.startswith("printf ")]
    args = line.split(" > ")[0].split(" ", 1)[1]
    d = tmp_path / "markers"
    d.mkdir()
    fmt, *values = shlex.split(args)
    (d / "seantech").write_text(fmt.replace("%s", "{}").replace("\\n", "\n").format(*values))
    assert seat.read_markers(d, owner_uid=os.getuid())["seantech"].dropin == seat.DROPIN_NAME


def test_status_prints_the_restart_command_for_a_pending_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(seat, "Host", lambda: host(tmp_path, markers=marker(NOW - 600)))
    assert cli.main(["seat-policy", "status"]) == 0
    out = capsys.readouterr().out
    assert "[restart-pending] seantech:" in out
    assert "sudo systemctl --user -M seantech@ restart wireplumber" in out and "recommended:" not in out
    monkeypatch.setattr(seat, "Host", lambda: host(tmp_path, markers=marker(STARTED - 60)))
    assert cli.main(["seat-policy", "status", "--json"]) == 0
    assert '"verdict": "guarded"' in capsys.readouterr().out


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
