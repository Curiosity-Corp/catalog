#!/usr/bin/env python3
"""The seat-policy A/B trial (issue #127): replug the Wave XLR N times and count the cycles that end with no input.

A manual tool, not shipped. It needs the owner at the machine, root for the USB switch, and a desk with no call.

    # 1. read what it would do (the default; touches nothing)
    python3 tools/seat_replug_trial.py --label other-wp-running

    # 2. A: ten cycles with the other user's WirePlumber as it is
    sudo -E python3 tools/seat_replug_trial.py --execute --label other-wp-running --cycles 10

    # 3. B: ten cycles with it stopped for the run (restarted afterwards, also on Ctrl-C)
    sudo -E python3 tools/seat_replug_trial.py --execute --label other-wp-stopped --cycles 10 \
        --stop-other-wireplumber seantech

    # 4. after the owner applied the policy (`streamstack seat-policy script --user seantech`): ten more cycles
    sudo -E python3 tools/seat_replug_trial.py --execute --label policy-applied --cycles 10

A "replug" is the kernel's own soft one: the device's `authorized` attribute in sysfs goes 0, then 1, so the card
disappears and is probed again exactly as after a cable pull. A cycle ends `duplex` when the card's PipeWire profile
has an input part and the Wave XLR's source exists within --wait seconds and audio flows from it, `stalled` when the
source exists but its ALSA hw_ptr does not advance or no samples arrive (the #186 fault: RUNNING, but silent; judged
by the same check as `streamstack doctor` and `mic-revive`, which adds about six seconds per cycle), `no-input` when
the card is back but with output profiles only (the #125 B fault), and `no-card` when it never came back.

A stall is judged over a window, not at one look (#188): the Console revives a stalled mic by itself, a few seconds
after the stall is confirmed, so a cycle that is stalled at first look is checked again until --recover-window seconds
after the replug. `duplex` is healthy at first look, `recovered` is stalled first and healthy within the window (the
line carries `recovered_after_s`, counted from the replug), `stalled` is still stalled when the window ends. Exit
status 1 when any cycle was `stalled`, `no-input` or `no-card`; `recovered` passes. Results are appended,
one JSON line per cycle, to --results so they can be pasted on the issue.

Safety, all checked before the first cycle and again before every cycle:

* no call app (msedge, teams, zoom, chrome, ...) holds a capture stream (`pactl list source-outputs`), and nothing
  but StreamStack's own services has the program or camera loopbacks open (`fuser /dev/video42 /dev/video43`);
* the default source is `streamstack_mic` and stays whatever it is; this tool never sets it and never touches a
  mixer control, the voice chain, PipeWire or the seat user's WirePlumber;
* --stop-other-wireplumber only stops and starts that other user's own `wireplumber.service` (and restarts it in a
  `finally`), never the seat user's.

A replug drops the microphone for a few seconds each cycle, so the desk must be idle for the whole run.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from streamstack import micwatch
from streamstack.hardware import duplex

CALL_APPS = re.compile(r"msedge|microsoft[- ]edge|teams|zoom|chrome|chromium|webex|slack|discord|skype|firefox", re.I)
LOOPBACKS = ("/dev/video42", "/dev/video43")
# StreamStack's own services write and read the loopbacks all the time (the engine, the Canon bridge); they are not a
# call. A PID counts as StreamStack's own when its cgroup is one of these user units.
OWN_UNITS = re.compile(r"/(streamstack-[\w.-]+|canonctl-ptp-webcam)\.service$", re.M)
DEFAULT_SOURCE = "streamstack_mic"
SYSFS_USB = Path("/sys/bus/usb/devices")

DUPLEX = "duplex"
NO_INPUT = "no-input"
NO_CARD = "no-card"
STALLED = "stalled"
RECOVERED = "recovered"
PASSING = (DUPLEX, RECOVERED)

Run = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class TrialError(Exception):
    """The trial cannot start or must stop: a call is active, the device is not found, root is missing."""


def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: not installed")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"{argv[0]}: timed out")


# -- the guards (pure parsers, then one function that reads) ------------------------------------------------------


def call_apps_in(source_outputs: str) -> list[str]:
    """The call apps named in `pactl list source-outputs` (application.name, process.binary, media.name)."""
    found: list[str] = []
    for line in source_outputs.splitlines():
        if re.search(r"application\.(name|process\.binary)|media\.name", line):
            match = CALL_APPS.search(line)
            if match and match.group(0).lower() not in found:
                found.append(match.group(0).lower())
    return found


def fuser_pids(output: str) -> list[int]:
    """The PIDs `fuser` printed on stdout (stderr carries the file names)."""
    return [int(token) for token in output.split() if token.isdigit()]


def own_pid(pid: int, proc: Path = Path("/proc")) -> bool:
    """Whether this PID runs inside one of StreamStack's own user services (read from its cgroup)."""
    try:
        return bool(OWN_UNITS.search((proc / str(pid) / "cgroup").read_text()))
    except OSError:
        return False  # gone or unreadable: count it, so an unknown holder never lets the trial run


def guard(runner: Run, own: Callable[[int], bool] = own_pid) -> list[str]:
    """Why the trial must not run right now; empty when the desk is idle. Read-only."""
    reasons: list[str] = []
    outputs = runner(["pactl", "list", "source-outputs"])
    if outputs.returncode == 0:
        apps = call_apps_in(outputs.stdout)
        if apps:
            reasons.append(f"a call app holds a capture stream: {', '.join(apps)}")
    else:
        reasons.append(f"cannot read the capture streams (pactl: {outputs.stderr.strip() or outputs.returncode})")
    held = [pid for pid in fuser_pids(runner(["fuser", *LOOPBACKS]).stdout) if not own(pid)]
    if held:
        reasons.append(f"{' and '.join(LOOPBACKS)} are open (pids {', '.join(map(str, held))})")
    return reasons


def default_source(runner: Run) -> str:
    return runner(["pactl", "get-default-source"]).stdout.strip()


# -- the device ---------------------------------------------------------------------------------------------------


def find_usb(usb_id: str, sysfs: Path = SYSFS_USB) -> Path:
    """The sysfs directory of the device with this vendor:product id (never an interface directory)."""
    vendor, _, product = usb_id.partition(":")
    for entry in sorted(sysfs.glob("*")):
        if ":" in entry.name:
            continue
        try:
            seen = ((entry / "idVendor").read_text().strip(), (entry / "idProduct").read_text().strip())
        except OSError:
            continue
        if seen == (vendor, product):
            return entry
    raise TrialError(f"no USB device {usb_id} under {sysfs}; is it plugged in?")


def soft_replug(device: Path, sleep: Callable[[float], None] = time.sleep, gap: float = 2.0) -> None:
    """authorized 0, wait, authorized 1: the card goes away and is probed again. Needs root."""
    attribute = device / "authorized"
    try:
        attribute.write_text("0\n")
        sleep(gap)
        attribute.write_text("1\n")
    except PermissionError as exc:
        raise TrialError(f"cannot write {attribute}: run with sudo (the USB switch needs root)") from exc
    except OSError as exc:
        # Leave the device authorized whatever happened.
        try:
            attribute.write_text("1\n")
        except OSError:
            pass
        raise TrialError(f"cannot switch {device.name}: {exc.strerror or exc}") from exc


@dataclass(frozen=True)
class Outcome:
    cycle: int
    label: str
    result: str
    seconds: float
    profile: str = ""
    detail: str = ""
    recovered_after_s: float | None = None  # seconds from the replug to the first healthy look (recovered only)

    def line(self) -> str:
        return json.dumps(
            {
                "cycle": self.cycle,
                "label": self.label,
                "result": self.result,
                "seconds": round(self.seconds, 1),
                "profile": self.profile,
                "detail": self.detail,
                **(
                    {"recovered_after_s": round(self.recovered_after_s, 1)}
                    if self.recovered_after_s is not None
                    else {}
                ),
            }
        )


def classify(cards_json: str, card_match: str, sources: str, source_match: str) -> tuple[str, str]:
    """(result, profile) from `pactl -f json list cards` and `pactl list short sources`."""
    try:
        cards = json.loads(cards_json)
    except ValueError:
        return NO_CARD, ""
    card = next(
        (c for c in cards if isinstance(c, dict) and card_match in str(c.get("name", ""))),
        None,
    )
    if card is None:
        return NO_CARD, ""
    profile = str(card.get("active_profile") or "")
    has_source = any(
        source_match in line.split("\t")[1] and not line.split("\t")[1].endswith(".monitor")
        for line in sources.splitlines()
        if "\t" in line
    )
    if duplex.profile_has_input(profile) and has_source:
        return DUPLEX, profile
    return NO_INPUT, profile


def source_name(sources: str, source_match: str) -> str | None:
    """The full name of the capture source that matches, from `pactl list short sources` (never a monitor)."""
    for line in sources.splitlines():
        fields = line.split("\t")
        if len(fields) > 1 and source_match in fields[1] and not fields[1].endswith(".monitor"):
            return fields[1]
    return None


StallCheck = Callable[[str], "micwatch.StallProbe"]


def stall_check(source: str) -> micwatch.StallProbe:
    """The doctor's / mic-revive's check on a real source: its ALSA PCM must advance and samples must arrive."""
    return micwatch.watch_microphone(source, require_samples=True)


def judge_flow(source: str, check: StallCheck) -> tuple[str, str]:
    """(DUPLEX, "") when audio flows from the source, (STALLED, why) when it is stalled or only zeros. A source the
    check cannot judge (not ALSA, unreadable) is not called stalled: it keeps the duplex verdict."""
    probe = check(source)
    if probe.stalled is True:
        return STALLED, probe.detail or "the source delivers no audio"
    return DUPLEX, ""


def wait_for_outcome(
    runner: Run,
    card_match: str,
    source_match: str,
    wait: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    check: StallCheck | None = None,
) -> tuple[str, str, str]:
    """Poll until the card is duplex or `wait` seconds pass; the last classification is the answer. A duplex card
    whose source does not deliver audio is `stalled` (`check` is the stall check; None skips it).
    Returns (result, profile, detail)."""
    deadline = clock() + wait
    result, profile = NO_CARD, ""
    while True:
        cards = runner(["pactl", "-f", "json", "list", "cards"]).stdout
        sources = runner(["pactl", "list", "short", "sources"]).stdout
        result, profile = classify(cards, card_match, sources, source_match)
        if result == DUPLEX:
            name = source_name(sources, source_match)
            if check is not None and name is not None:
                result, detail = judge_flow(name, check)
                return result, profile, detail
            return result, profile, ""
        if clock() >= deadline:
            return result, profile, ""
        sleep(1.0)


def await_recovery(
    runner: Run,
    source_match: str,
    began: float,
    window: float,
    check: StallCheck,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    detail: str = "",
) -> tuple[str, str, float | None]:
    """A cycle that was stalled at first look: check again until `window` seconds after the replug (`began`).
    Returns (RECOVERED, "", seconds since the replug) at the first healthy look, else (STALLED, the last reason,
    None) when the window ends. A source that is momentarily not listed is waited for, not called healthy."""
    while clock() - began < window:
        name = source_name(runner(["pactl", "list", "short", "sources"]).stdout, source_match)
        if name is None:
            sleep(1.0)
            continue
        result, why = judge_flow(name, check)
        if result == DUPLEX:
            return RECOVERED, "", clock() - began
        detail = why or detail
    return STALLED, detail, None


def summarize(outcomes: list[Outcome]) -> str:
    label = outcomes[0].label if outcomes else "?"
    counts = {r: sum(1 for o in outcomes if o.result == r) for r in (DUPLEX, RECOVERED, STALLED, NO_INPUT, NO_CARD)}
    return (
        f"{label}: {len(outcomes)} cycles, {counts[DUPLEX]} duplex, {counts[RECOVERED]} recovered, "
        f"{counts[STALLED]} stalled, {counts[NO_INPUT]} no-input, {counts[NO_CARD]} no-card"
    )


def other_wireplumber(user: str, action: str, runner: Run) -> None:
    """Stop or start another user's own wireplumber.service (never the seat user's)."""
    uid = runner(["id", "-u", user])
    if uid.returncode != 0 or not uid.stdout.strip().isdigit():
        raise TrialError(f"no user named {user!r}")
    env = f"XDG_RUNTIME_DIR=/run/user/{uid.stdout.strip()}"
    result = runner(["sudo", "-u", user, "env", env, "systemctl", "--user", action, "wireplumber.service"])
    if result.returncode != 0:
        raise TrialError(f"could not {action} {user}'s wireplumber: {result.stderr.strip()}")


def trial(
    args: argparse.Namespace,
    runner: Run = run,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    sysfs: Path = SYSFS_USB,
    out: Callable[[str], None] = print,
    check: StallCheck | None = stall_check,
) -> list[Outcome]:
    reasons = guard(runner)
    if reasons:
        raise TrialError("the desk is not idle: " + "; ".join(reasons))
    source = default_source(runner)
    if source != DEFAULT_SOURCE:
        raise TrialError(f"the default source is {source or 'unset'}, not {DEFAULT_SOURCE}; this tool never changes it")
    device = find_usb(args.usb, sysfs)
    if not args.execute:
        out(f"dry run: would switch {device} off and on {args.cycles} times, label {args.label!r}")
        out("nothing was touched; add --execute (as root) at the machine with the desk idle")
        return []
    outcomes: list[Outcome] = []
    stopped = False
    try:
        if args.stop_other_wireplumber:
            other_wireplumber(args.stop_other_wireplumber, "stop", runner)
            stopped = True
            sleep(2.0)
        for n in range(1, args.cycles + 1):
            reasons = guard(runner)  # a call may have started since the last cycle
            if reasons:
                raise TrialError(f"stopped before cycle {n}: " + "; ".join(reasons))
            began = clock()
            soft_replug(device, sleep)
            result, profile, detail = wait_for_outcome(
                runner, args.card_match, args.source_match, args.wait, clock, sleep, check
            )
            recovered_after: float | None = None
            if result == STALLED and check is not None:
                result, detail, recovered_after = await_recovery(
                    runner, args.source_match, began, args.recover_window, check, clock, sleep, detail
                )
            outcome = Outcome(n, args.label, result, clock() - began, profile, detail, recovered_after)
            outcomes.append(outcome)
            out(outcome.line())
            if args.results:
                with open(args.results, "a", encoding="utf-8") as handle:
                    handle.write(outcome.line() + "\n")
            sleep(args.settle)
    finally:
        if stopped:
            other_wireplumber(args.stop_other_wireplumber, "start", runner)
    out(summarize(outcomes))
    return outcomes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--label", default="trial", help="what this run is (other-wp-running, other-wp-stopped, ...)")
    parser.add_argument("--usb", default="0fd9:007d", help="the interface's USB id (default: Wave XLR)")
    parser.add_argument("--card-match", default="Wave_XLR", help="part of the PipeWire card name")
    parser.add_argument("--source-match", default="Wave_XLR", help="part of the PipeWire source name")
    parser.add_argument("--wait", type=float, default=20.0, help="seconds to wait for the input to return per cycle")
    parser.add_argument(
        "--recover-window",
        type=float,
        default=45.0,
        help="a stalled cycle passes as `recovered` when the mic is healthy within this many seconds of the replug",
    )
    parser.add_argument("--settle", type=float, default=5.0, help="seconds to rest between cycles")
    parser.add_argument("--stop-other-wireplumber", metavar="USER", help="stop this user's wireplumber for the run")
    parser.add_argument("--results", help="append one JSON line per cycle to this file")
    parser.add_argument("--execute", action="store_true", help="really replug the device (needs root)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cycles < 1:
        print("seat_replug_trial: --cycles must be at least 1", file=sys.stderr)
        return 2
    try:
        outcomes = trial(args)
    except TrialError as exc:
        print(f"seat_replug_trial: {exc}", file=sys.stderr)
        return 2
    return 1 if any(o.result not in PASSING for o in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
