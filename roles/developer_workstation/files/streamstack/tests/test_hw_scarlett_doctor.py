"""The `hw-scarlett` doctor row: a Scarlett that shows no controls gets a named cause and its fix (#130).

The kernel lines and the amixer listings in tests/fixtures/hardware/scarlett2 are synthesized from the format strings
of sound/usb/mixer_scarlett2.c (see their headers); the real-device pass waits for the 2i2 to enumerate on deskmeet1.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from hardware_support import FIXTURES, FakeMachine

from streamstack.hardware import doctor, scarlett_doctor, schema
from streamstack.hardware.alsa import parse_contents
from streamstack.hardware.found import Found
from streamstack.hardware.plan import resolve
from streamstack.hardware.scarlett_doctor import (
    ABSENT,
    CAUSE_DRIVER_OFF,
    CAUSE_MSD,
    CAUSE_OLD_FIRMWARE,
    CAUSE_UNKNOWN,
    CAUSE_UNREADABLE,
    HEALTHY,
    NO_CARD,
    NO_CONTROLS,
    Evidence,
)

CATALOGUE = schema.packaged()
DIR = FIXTURES / "scarlett2"
GEN4_HEALTHY = Path(__file__).parent / "fixtures" / "amixer_scarlett_2i2_gen4.txt"
RIG = "studio-deck"
USB = "1235:8219"
NAME = "Scarlett 2i2 4th Gen"


def fixture(name: str) -> str:
    return (DIR / name).read_text(encoding="utf-8")


def controls(name: str) -> dict:
    path = GEN4_HEALTHY if name == "healthy" else DIR / f"amixer-{name}.txt"
    return parse_contents(path.read_text(encoding="utf-8"))


def machine_with(tmp_path: Path, *, usb: bool, card: bool) -> FakeMachine:
    machine = FakeMachine(tmp_path)
    if usb:
        machine.add_usb(USB, NAME)
    if card:
        machine.add_alsa_usb(4, "Gen4", USB, f"Focusrite {NAME}", "44100, 48000", "S32_LE", 2, 2)
    return machine


def found(machine: FakeMachine) -> Found:
    snapshot = machine.snapshot()
    return Found(resolve(snapshot, CATALOGUE, {"STREAMSTACK_RIG": RIG}), snapshot, CATALOGUE)


def diagnose(machine: FakeMachine, evidence: Evidence) -> scarlett_doctor.Diagnosis:
    got = scarlett_doctor.diagnose(machine.snapshot(), CATALOGUE, evidence, RIG)
    assert got is not None
    return got


def scarlett_rows(machine: FakeMachine, evidence: Evidence | None) -> list[dict[str, str]]:
    f = found(machine)
    return [
        r for r in doctor.checks(f.plan, f.snapshot, f.catalogue, "deepfilternet", evidence) if r["id"] == "hw-scarlett"
    ]


# -- the kernel log ------------------------------------------------------------------------------------


def test_the_kernel_log_parser_reads_every_scarlett2_line() -> None:
    log = scarlett_doctor.parse_kernel_log(fixture("kernel-old-firmware.txt"))
    assert log.firmware_old == ("Scarlett Gen 4", 2000, 2115)
    assert log.firmware_old_line == "usb 1-3: Focusrite Scarlett Gen 4 firmware version 2000 is too old; need 2115"
    assert log.driver_enabled == "Scarlett Gen 4" and log.driver_enabled_pid == "8219"


def test_the_parser_reads_the_driver_disabled_line_and_the_usb_errors() -> None:
    off = scarlett_doctor.parse_kernel_log(fixture("kernel-driver-off.txt"))
    assert off.driver_disabled is not None and "device_setup=1" in off.driver_disabled
    errors = scarlett_doctor.parse_kernel_log(fixture("kernel-no-card.txt")).usb_errors
    assert [e.split(": ", 1)[-1] for e in errors] == [
        "can't set config #1, error -71",
        "device descriptor read/64, error -71",
    ]


def test_a_later_replug_replaces_the_earlier_verdict() -> None:
    text = fixture("kernel-old-firmware.txt") + fixture("kernel-healthy.txt")
    log = scarlett_doctor.parse_kernel_log(text)
    assert log.firmware_old is None and log.driver_enabled == "Scarlett Gen 4"


def test_the_parser_takes_journalctl_syslog_and_dmesg_prefixes() -> None:
    for line in (
        "[ 5.612] host kernel: usb 1-3: Focusrite Scarlett Gen 4 firmware version 2000 is too old; need 2115",
        "Oct 27 12:00:01 host kernel: [    5.612345] usb 1-3: Focusrite Scarlett Gen 4 firmware version 2000 "
        "is too old; need 2115",
        "usb 1-3: Focusrite Scarlett Gen 4 firmware version 2000 is too old; need 2115",
    ):
        got = scarlett_doctor.parse_kernel_log(line)
        assert got.firmware_old == ("Scarlett Gen 4", 2000, 2115)
        assert got.firmware_old_line.startswith("usb 1-3: Focusrite")


def test_unrelated_text_gives_an_empty_log() -> None:
    log = scarlett_doctor.parse_kernel_log("nothing about audio here\n\n")
    assert not log.mentions_focusrite and log.usb_errors == ()


# -- the four states -----------------------------------------------------------------------------------


def test_state_one_not_on_usb_defers_to_the_interface_row_when_the_log_is_clean(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=False, card=False)
    evidence = Evidence(kernel_log=fixture("kernel-absent-clean.txt"))
    got = diagnose(machine, evidence)
    assert got.state == ABSENT and got.expected
    assert scarlett_rows(machine, evidence) == []
    interface = [
        r
        for r in doctor.checks(*(lambda f: (f.plan, f.snapshot, f.catalogue))(found(machine)))
        if r["id"] == "hw-audio-interface"
    ]
    assert interface and interface[0]["state"] == "warn" and "re-seat" in interface[0]["hint"]


def test_state_one_with_usb_errors_quotes_them(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=False, card=False)
    [row] = scarlett_rows(machine, Evidence(kernel_log=fixture("kernel-absent-errors.txt")))
    assert row["state"] == "warn" and "not on USB" in row["title"]
    assert "device descriptor read/64, error -71" in row["detail"] and "unable to enumerate USB device" in row["detail"]
    assert "re-seat" in row["hint"] and "rear USB port" in row["hint"]


def test_state_two_on_usb_but_no_sound_card_quotes_the_usb_error(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=True, card=False)
    evidence = Evidence(kernel_log=fixture("kernel-no-card.txt"))
    assert diagnose(machine, evidence).state == NO_CARD
    [row] = scarlett_rows(machine, evidence)
    assert row["state"] == "fail" and "1235:8219" in row["title"] and "no sound card" in row["title"]
    assert "can't set config #1, error -71" in row["detail"]
    assert "journalctl -k" in row["hint"]


def test_state_two_without_a_kernel_log_says_why(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=True, card=False)
    evidence = Evidence(kernel_log=None, kernel_log_state="denied")
    [row] = scarlett_rows(machine, evidence)
    assert "may not read the kernel log" in row["detail"]


def test_state_three_msd_mode(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=True, card=True)
    evidence = Evidence(controls=controls("msd"), kernel_log=fixture("kernel-msd.txt"))
    got = diagnose(machine, evidence)
    assert got.state == NO_CONTROLS and got.cause == CAUSE_MSD
    assert got.msd is True and got.firmware == 2417 and got.minimum_firmware == 2115
    [row] = scarlett_rows(machine, evidence)
    assert row["state"] == "fail" and "MSD" in row["title"]
    assert "no other controls are created while MSD mode is on" in row["detail"]
    assert "hold the 48V button for five seconds" in row["hint"]
    assert "amixer -c 4 cset name='MSD Mode Switch' off" in row["hint"]


def test_state_three_firmware_too_old_quotes_the_kernel_line_and_prints_the_tool(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=True, card=True)
    evidence = Evidence(controls=controls("old-firmware"), kernel_log=fixture("kernel-old-firmware.txt"))
    got = diagnose(machine, evidence)
    assert got.cause == CAUSE_OLD_FIRMWARE and got.firmware == 2000
    [row] = scarlett_rows(machine, evidence)
    assert row["state"] == "fail" and "firmware is too old" in row["title"]
    assert "kernel: usb 1-3: Focusrite Scarlett Gen 4 firmware version 2000 is too old; need 2115" in row["detail"]
    assert "firmware 2000, the driver needs 2115" in row["detail"]
    assert "scarlett2 update" in row["hint"] and "github.com/geoffreybennett/scarlett2" in row["hint"]


def test_firmware_is_judged_from_the_controls_when_the_log_is_unreadable(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=True, card=True)
    got = diagnose(machine, Evidence(controls=controls("old-firmware"), kernel_log=None, kernel_log_state="denied"))
    assert got.cause == CAUSE_OLD_FIRMWARE
    assert "below Minimum Firmware Version 2115" in got.kernel_lines[0]


def test_old_firmware_wins_over_msd_because_the_driver_checks_it_first(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=True, card=True)
    both = parse_contents(fixture("amixer-msd.txt").replace("values=2417", "values=2000"))
    assert diagnose(machine, Evidence(controls=both)).cause == CAUSE_OLD_FIRMWARE


def test_state_three_driver_switched_off_by_modprobe(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=True, card=True)
    evidence = Evidence(controls=controls("driver-off"), kernel_log=fixture("kernel-driver-off.txt"))
    got = diagnose(machine, evidence)
    assert got.cause == CAUSE_DRIVER_OFF
    [row] = scarlett_rows(machine, evidence)
    assert "device_setup=1" in row["detail"] and "device_setup" in row["hint"]


def test_state_three_with_no_explanation_is_unknown_and_asks_for_the_log(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=True, card=True)
    got = diagnose(machine, Evidence(controls={}, kernel_log="", kernel_log_state="denied"))
    assert got.cause == CAUSE_UNKNOWN
    [row] = scarlett_rows(machine, Evidence(controls={}, kernel_log=None, kernel_log_state="denied"))
    assert row["state"] == "fail" and "systemd-journal" in row["hint"]


def test_a_mixer_that_cannot_be_listed_is_a_distinct_unreadable_cause(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=True, card=True)
    evidence = Evidence(controls=None, controls_error="amixer is not installed (alsa-utils)")
    got = diagnose(machine, evidence)
    assert got.cause == CAUSE_UNREADABLE
    [row] = scarlett_rows(machine, evidence)
    assert "could not be read" in row["title"] and "alsa-utils" in row["detail"] and row["hint"]


def test_state_four_healthy_shows_firmware_and_msd(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=True, card=True)
    evidence = Evidence(controls=controls("healthy"))
    got = diagnose(machine, evidence)
    assert got.state == HEALTHY and got.firmware == 2417 and got.msd is False
    [row] = scarlett_rows(machine, evidence)
    assert row["state"] == "ok" and row["hint"] == ""
    assert "firmware 2417 (driver minimum 2115)" in row["detail"] and "MSD mode off" in row["detail"]


def test_a_stale_old_firmware_line_does_not_hide_a_healthy_card(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=True, card=True)
    text = fixture("kernel-old-firmware.txt") + fixture("kernel-healthy.txt")
    assert diagnose(machine, Evidence(controls=controls("healthy"), kernel_log=text)).state == HEALTHY


def test_no_scarlett_and_no_expectation_says_nothing(tmp_path: Path) -> None:
    machine = FakeMachine(tmp_path)
    machine.add_usb("0fd9:007d", "Wave XLR")
    snapshot = machine.snapshot()
    assert scarlett_doctor.diagnose(snapshot, CATALOGUE, Evidence(), "studio-wave-xlr") is None
    assert scarlett_doctor.rows(None) == []
    assert scarlett_doctor.collect(snapshot, CATALOGUE, "studio-wave-xlr") is None


def test_every_row_that_is_not_ok_names_the_fix(tmp_path: Path) -> None:
    cases = [
        (False, False, Evidence(kernel_log=fixture("kernel-absent-errors.txt"))),
        (True, False, Evidence(kernel_log=fixture("kernel-no-card.txt"))),
        (True, True, Evidence(controls=controls("msd"))),
        (True, True, Evidence(controls=controls("old-firmware"))),
        (True, True, Evidence(controls=controls("driver-off"), kernel_log=fixture("kernel-driver-off.txt"))),
        (True, True, Evidence(controls={})),
        (True, True, Evidence(controls=None, controls_error="boom")),
    ]
    for n, (usb, card, evidence) in enumerate(cases):
        for row in scarlett_rows(machine_with(tmp_path / str(n), usb=usb, card=card), evidence):
            assert row["state"] != "ok" and row["hint"], row


def test_the_row_never_runs_a_command_that_changes_the_interface(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=True, card=True)
    machine.tools["amixer"] = lambda argv: subprocess.CompletedProcess(argv, 0, fixture("amixer-msd.txt"), "")
    machine.tools["journalctl"] = fixture("kernel-msd.txt")
    scarlett_doctor.collect(machine.snapshot(), CATALOGUE, RIG, machine._run, machine._which)
    ran = [c for c in machine.calls if c[0] in ("amixer", "journalctl", "dmesg")]
    assert ran and all(c[0] != "amixer" or c[3:] == ["contents"] for c in ran)
    assert not any(c[0] in ("scarlett2", "scarlett2-firmware") or "cset" in c or "reboot" in c for c in machine.calls)


# -- collecting the evidence ---------------------------------------------------------------------------


def test_collect_skips_the_slow_kernel_log_when_the_card_is_healthy(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=True, card=True)
    machine.tools["amixer"] = lambda argv: subprocess.CompletedProcess(argv, 0, GEN4_HEALTHY.read_text(), "")
    machine.tools["journalctl"] = fixture("kernel-healthy.txt")
    evidence = scarlett_doctor.collect(machine.snapshot(), CATALOGUE, RIG, machine._run, machine._which)
    assert evidence is not None and evidence.kernel_log is None
    assert [c[0] for c in machine.calls if c[0] == "journalctl"] == []
    assert diagnose(machine, evidence).state == HEALTHY


def test_collect_reads_the_log_when_controls_are_missing(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=True, card=True)
    machine.tools["amixer"] = lambda argv: subprocess.CompletedProcess(argv, 0, fixture("amixer-old-firmware.txt"), "")
    machine.tools["journalctl"] = fixture("kernel-old-firmware.txt")
    evidence = scarlett_doctor.collect(machine.snapshot(), CATALOGUE, RIG, machine._run, machine._which)
    assert evidence is not None and evidence.kernel_log is not None
    assert diagnose(machine, evidence).cause == CAUSE_OLD_FIRMWARE


def test_collect_treats_an_empty_card_listing_as_no_controls(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=True, card=True)
    machine.tools["amixer"] = lambda argv: subprocess.CompletedProcess(argv, 0, "", "")
    machine.tools["journalctl"] = ""
    evidence = scarlett_doctor.collect(machine.snapshot(), CATALOGUE, RIG, machine._run, machine._which)
    assert evidence is not None and evidence.controls == {}
    assert diagnose(machine, evidence).cause == CAUSE_UNKNOWN


def test_collect_without_amixer_is_unreadable_not_a_crash(tmp_path: Path) -> None:
    machine = machine_with(tmp_path, usb=True, card=True)
    machine.tools["journalctl"] = ""
    evidence = scarlett_doctor.collect(machine.snapshot(), CATALOGUE, RIG, machine._run, machine._which)
    assert evidence is not None and evidence.controls is None and "alsa-utils" in evidence.controls_error


@pytest.mark.parametrize(
    ("stdout", "stderr", "code", "state"),
    [
        ("", "Hint: You are currently not seeing messages from other users and the system.", 0, "denied"),
        ("", "dmesg: read kernel buffer failed: Operation not permitted", 1, "denied"),
        ("", "Failed to open journal: Input/output error", 1, "error"),
        ("log text", "", 0, "ok"),
    ],
)
def test_the_kernel_log_reader_classifies_failures(stdout: str, stderr: str, code: int, state: str) -> None:
    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    text, got, error = scarlett_doctor.read_kernel_log(run, lambda name: f"/usr/bin/{name}")
    assert got == state
    assert (text is None) == (state != "ok")
    assert (error == "") == (state == "ok")


def test_the_kernel_log_reader_with_no_tool_or_a_timeout() -> None:
    assert scarlett_doctor.read_kernel_log(lambda a, t: None, lambda n: None)[1] == "absent"
    assert scarlett_doctor.read_kernel_log(lambda a, t: None, lambda n: f"/usr/bin/{n}")[1] == "timeout"


def test_the_kernel_log_reader_falls_back_to_dmesg_when_the_journal_is_closed() -> None:
    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if argv[0] == "journalctl":
            return subprocess.CompletedProcess(argv, 1, "", "Failed to open journal: Permission denied")
        return subprocess.CompletedProcess(argv, 0, "from dmesg", "")

    assert scarlett_doctor.read_kernel_log(run, lambda n: f"/usr/bin/{n}") == ("from dmesg", "ok", "")
