"""Seat policy: one WirePlumber per studio desk (#127, ADR 0007).

Two local users can each run a full PipeWire + WirePlumber on one desk. The second user is not at the seat, but
`loginctl enable-linger` keeps their user manager, and so their WirePlumber, alive all the time, and membership of the
`audio` and `video` groups lets it open every sound card and camera although the seat's ACL names only the person at the
desk. WirePlumber's ALSA monitor arbitrates cards with `org.freedesktop.ReserveDevice1` on the *session* bus, which is
per user, so it cannot arbitrate between two users. On dm313red (2026-10-27) both users ran WirePlumber for 2 days 9
hours and the Wave XLR came up output-only after a replug (#125 B).

This module is read-only. `collect` looks at `loginctl`, `ps`, `id`, the linger directory and the device groups;
`diagnose` is pure and says, per other user, whether they contend for the studio hardware; `fix_script` prints the
commands for the chosen option for the owner to run. StreamStack never edits another user's account or services.

The three options (the ADR says why the first is the policy):

* `dropin`  a WirePlumber 0.5 drop-in in the other user's config that switches off the `hardware.audio` and
            `hardware.video-capture` features (what the shipped `video-only` profile does for audio), so their
            WirePlumber never probes a card or a camera. Reversible by deleting one file.
* `groups`  remove the other user from `audio` and `video`; the seat ACL (uaccess) still gives the person at the desk
            every device, and gives it to the other user when they sit down.
* `stop`    stop and mask their `wireplumber.service`. Last resort: it also breaks their own remote audio.
"""

from __future__ import annotations

import grp
import os
import pwd
import re
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

from streamstack import system

CHECK_ID = "hw-seat"
GROUP = "hardware"

SESSION_MANAGERS = ("wireplumber",)
AUDIO_PROCESSES = ("pipewire", "wireplumber", "pipewire-pulse")
DEFAULT_DEVICE_GROUPS = ("audio", "video")
LINGER_DIR = Path("/var/lib/systemd/linger")
DROPIN_DIR = ".config/wireplumber/wireplumber.conf.d"
DROPIN_NAME = "90-streamstack-seat-guest.conf"

OPTIONS = ("dropin", "groups", "stop")
RECOMMENDED = "dropin"

# What a drop-in in the other user's config can say about the cards, read from their files when this user may.
DROPIN_FEATURES = "features"  # hardware.audio / monitor.alsa disabled: no card is probed at all
DROPIN_CARDS = "cards"  # name rules that disable the studio cards only (the catalogue's workstation role)
DROPIN_NONE = "none"  # the directory is readable and holds no such file
DROPIN_UNKNOWN = "unknown"  # the home is not readable by this user

_FEATURES = re.compile(r"(hardware\.audio|monitor\.alsa)\s*=\s*disabled")
_CARD_RULE = re.compile(r"device\.disabled\s*=\s*true")

DROPIN_TEXT = """\
# Managed by StreamStack (docs/adr/0007-seat-policy.md, issue #127).
#
# This user is not the person at the studio desk, but their WirePlumber runs all the time (linger). Two WirePlumbers
# probing the same USB sound card or camera at the same moment can leave the card output-only (Wave XLR, #125 B).
# Switch off the hardware features here; the person at the seat has them. To undo: delete this file and restart the
# user's wireplumber (systemctl --user restart wireplumber).
wireplumber.profiles = {
  main = {
    hardware.audio = disabled
    hardware.video-capture = disabled
  }
}
"""


@dataclass(frozen=True)
class Session:
    id: str
    uid: int
    user: str
    seat: str  # "" when the session has no seat (ssh, user manager)
    klass: str  # "user", "manager", "greeter", ...


@dataclass(frozen=True)
class AudioProcess:
    uid: int
    user: str
    pid: int
    age_s: int
    comm: str


@dataclass(frozen=True)
class Evidence:
    """Everything `diagnose` reads. Built by `collect`, or by a test from the recorded fixtures."""

    me: str = ""  # the user running the doctor
    sessions: tuple[Session, ...] = ()
    active_session: str = ""  # seat0's ActiveSession id
    linger: frozenset[str] = frozenset()
    processes: tuple[AudioProcess, ...] = ()
    groups: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # user -> group names (`id -nG`)
    device_groups: tuple[str, ...] = DEFAULT_DEVICE_GROUPS  # the groups that own /dev/snd and /dev/video*
    dropins: Mapping[str, str] = field(default_factory=dict)  # user -> DROPIN_*
    homes: Mapping[str, str] = field(default_factory=dict)
    uids: Mapping[str, int] = field(default_factory=dict)
    guarded: frozenset[str] = frozenset()  # users the owner says have the policy (STREAMSTACK_SEAT_GUARDED)
    errors: tuple[str, ...] = ()  # what could not be read, one line each

    def seat_users(self) -> tuple[str, ...]:
        """The users with a graphical or console session on a seat, the active one first."""
        seated = [s for s in self.sessions if s.seat and s.klass in ("user", "greeter", "")]
        seated.sort(key=lambda s: s.id != self.active_session)
        return tuple(dict.fromkeys(s.user for s in seated))


@dataclass(frozen=True)
class Guest:
    """Another user's audio stack on this desk."""

    user: str
    uid: int
    processes: tuple[AudioProcess, ...]  # their pipewire, wireplumber and pipewire-pulse
    linger: bool
    groups: tuple[str, ...]  # the device groups they are in
    dropin: str
    guarded_by_owner: bool

    @property
    def wireplumber(self) -> AudioProcess | None:
        return next((p for p in self.processes if p.comm == "wireplumber"), None)

    @property
    def has_device_access(self) -> bool:
        return bool(self.groups)

    @property
    def verdict(self) -> str:
        return verdict_of(self)


# Verdicts, worst first.
CONTENDS = "contends"  # their WirePlumber runs and may open the studio cards
UNVERIFIED = "unverified"  # as above, but their config is out of sight, so a drop-in may already guard it
LINGERING = "lingering"  # no WirePlumber now, but linger and device groups bring it back at boot
GUARDED = "guarded"  # a drop-in, no device access, or the owner confirmed the policy
IDLE = "idle"  # nothing of theirs runs and nothing will


def verdict_of(guest: Guest) -> str:
    wp = guest.wireplumber
    if guest.dropin == DROPIN_FEATURES or guest.guarded_by_owner:
        return GUARDED
    if not guest.has_device_access:
        return GUARDED if wp else IDLE
    if wp is None:
        return LINGERING if guest.linger else IDLE
    if guest.dropin == DROPIN_CARDS:
        return GUARDED
    return UNVERIFIED if guest.dropin == DROPIN_UNKNOWN else CONTENDS


@dataclass(frozen=True)
class Diagnosis:
    seat_user: str
    guests: tuple[Guest, ...]
    recommended: str = RECOMMENDED
    errors: tuple[str, ...] = ()

    @property
    def worst(self) -> str:
        order = (CONTENDS, UNVERIFIED, LINGERING, GUARDED, IDLE)
        return min((g.verdict for g in self.guests), key=order.index, default=IDLE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seat_user": self.seat_user,
            "verdict": self.worst,
            "recommended": self.recommended,
            "guests": [
                {
                    "user": g.user,
                    "verdict": g.verdict,
                    "linger": g.linger,
                    "device_groups": list(g.groups),
                    "dropin": g.dropin,
                    "processes": [{"comm": p.comm, "pid": p.pid, "age_s": p.age_s} for p in g.processes],
                }
                for g in self.guests
            ],
            "errors": list(self.errors),
        }


# -- parsing ---------------------------------------------------------------------------------------------------------

KNOWN_CLASSES = frozenset(
    {"user", "manager", "greeter", "lock-screen", "background", "background-light", "manager-early"}
)


def parse_sessions(text: str) -> tuple[Session, ...]:
    """`loginctl list-sessions --no-legend`: SESSION UID USER SEAT LEADER CLASS TTY IDLE SINCE (a "-" is empty).
    The class is found by name rather than by column, because the column set differs between systemd versions."""
    found = []
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) < 4 or not tokens[1].isdigit():
            continue
        klass = next((t for t in tokens[4:7] if t in KNOWN_CLASSES), "")
        found.append(Session(tokens[0], int(tokens[1]), tokens[2], "" if tokens[3] == "-" else tokens[3], klass))
    return tuple(found)


def parse_processes(text: str) -> tuple[AudioProcess, ...]:
    """`ps -eo uid=,user:32=,pid=,etimes=,comm=` rows for pipewire, wireplumber and pipewire-pulse only."""
    found = []
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) < 5 or not (tokens[0].isdigit() and tokens[2].isdigit() and tokens[3].isdigit()):
            continue
        if tokens[4] in AUDIO_PROCESSES:
            found.append(AudioProcess(int(tokens[0]), tokens[1], int(tokens[2]), int(tokens[3]), tokens[4]))
    return tuple(found)


def parse_groups(text: str) -> tuple[str, ...]:
    """`id -nG USER`: the group names on one line."""
    return tuple(text.split())


def classify_dropins(texts: Iterable[str]) -> str:
    """What the contents of a user's `wireplumber.conf.d/*.conf` files say about the studio hardware."""
    joined = "\n".join(
        "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#")) for text in texts
    )
    if _FEATURES.search(joined):
        return DROPIN_FEATURES
    if _CARD_RULE.search(joined):
        return DROPIN_CARDS
    return DROPIN_NONE


# -- collecting (read-only) ------------------------------------------------------------------------------------------

Runner = Callable[[list[str], float], "CompletedProcess[str] | None"]


@dataclass
class Host:
    """Where the evidence comes from. The defaults are this machine; tests pass fixtures."""

    run: Runner = system.run
    which: Callable[[str], str | None] = shutil.which
    linger_dir: Path = LINGER_DIR
    me: Callable[[], str] = lambda: current_user()
    device_groups: Callable[[], tuple[str, ...]] = lambda: observed_device_groups()
    home_of: Callable[[str], str | None] = lambda user: home_of(user)
    uid_of: Callable[[str], int | None] = lambda user: uid_of(user)
    read_dropins: Callable[[str], list[str] | None] = lambda home: read_dropin_files(home)


def current_user() -> str:
    """The user this process runs as; a container uid with no passwd entry falls back to $USER or the uid."""
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return os.environ.get("USER") or os.environ.get("LOGNAME") or str(os.getuid())


def home_of(user: str) -> str | None:
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        return None


def uid_of(user: str) -> int | None:
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        return None


def observed_device_groups(
    patterns: tuple[str, ...] = ("/dev/snd/controlC*", "/dev/video*"),
) -> tuple[str, ...]:
    """The groups that own the sound and video nodes (usually audio and video), from the real nodes."""
    from glob import glob

    names: list[str] = []
    for pattern in patterns:
        for path in sorted(glob(pattern))[:4]:
            try:
                names.append(grp.getgrgid(os.stat(path).st_gid).gr_name)
            except (OSError, KeyError):
                continue
    seen = tuple(dict.fromkeys(n for n in names if n != "root"))
    return seen or DEFAULT_DEVICE_GROUPS


def read_dropin_files(home: str) -> list[str] | None:
    """The user's WirePlumber drop-ins, or None when their config directory cannot be read by this user. An absent
    directory is an empty list: it is readable and holds nothing (when the home itself is closed, that is None)."""
    base = Path(home) / DROPIN_DIR
    try:
        # scandir, not glob: glob swallows a permission error and would make a closed home look empty.
        with os.scandir(base) as entries:
            names = sorted(e.name for e in entries if e.name.endswith(".conf"))
        return [(base / name).read_text(errors="replace") for name in names]
    except FileNotFoundError:
        return []
    except OSError:
        return None  # PermissionError, or a file that vanished or cannot be read: the config is out of sight


def _out(host: Host, argv: list[str], errors: list[str]) -> str:
    if not host.which(argv[0]):
        errors.append(f"{argv[0]} is not installed")
        return ""
    result = host.run(argv, 5.0)
    if result is None:
        errors.append(f"{argv[0]} did not answer")
        return ""
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip().splitlines()
        errors.append(f"{argv[0]} failed: {message[-1][:120] if message else result.returncode}")
        return ""
    return result.stdout or ""


def collect(guarded: Iterable[str] = (), host: Host | None = None) -> Evidence:
    """Read what `diagnose` needs. Every read can fail alone; a failure is listed in `errors`, never raised."""
    host = host or Host()
    errors: list[str] = []
    sessions = parse_sessions(_out(host, ["loginctl", "list-sessions", "--no-legend"], errors))
    active = _out(host, ["loginctl", "show-seat", "seat0", "-p", "ActiveSession", "--value"], errors).strip()
    try:
        linger = frozenset(p.name for p in host.linger_dir.iterdir())
    except OSError as exc:
        linger = frozenset()
        errors.append(f"linger list unreadable: {exc.strerror or exc}")
    processes = parse_processes(_out(host, ["ps", "-eo", "uid=,user:32=,pid=,etimes=,comm="], errors))
    me = host.me()
    others = sorted(({p.user for p in processes} | set(linger) | {s.user for s in sessions}) - {me})
    groups: dict[str, tuple[str, ...]] = {}
    dropins: dict[str, str] = {}
    homes: dict[str, str] = {}
    uids: dict[str, int] = {}
    for user in others:
        if user == me:
            continue
        groups[user] = parse_groups(_out(host, ["id", "-nG", user], errors))
        uid = host.uid_of(user)
        if uid is not None:
            uids[user] = uid
        home = host.home_of(user)
        if home:
            homes[user] = home
            texts = host.read_dropins(home)
            dropins[user] = DROPIN_UNKNOWN if texts is None else classify_dropins(texts)
        else:
            dropins[user] = DROPIN_UNKNOWN
    return Evidence(
        me=me,
        sessions=sessions,
        active_session=active,
        linger=linger,
        processes=processes,
        groups=groups,
        device_groups=host.device_groups(),
        dropins=dropins,
        homes=homes,
        uids=uids,
        guarded=frozenset(guarded),
        errors=tuple(errors),
    )


def parse_guarded(setting: str) -> frozenset[str]:
    """STREAMSTACK_SEAT_GUARDED: the other users the owner has applied the policy for, comma or space separated."""
    return frozenset(t for t in re.split(r"[,\s]+", setting.strip()) if t)


# -- diagnosis (pure) ------------------------------------------------------------------------------------------------


def seat_user(evidence: Evidence) -> str:
    users = evidence.seat_users()
    return users[0] if users else evidence.me


def diagnose(evidence: Evidence) -> Diagnosis:
    """The other users' audio stacks on this desk and whether each contends for the studio cards. A user is "other"
    when they are not the seat's user (the person at the desk) and have a PipeWire process or linger."""
    seated = seat_user(evidence)
    runners = {p.user for p in evidence.processes}
    guests = []
    for user in sorted((runners | set(evidence.linger)) - {seated}):
        mine = tuple(p for p in evidence.processes if p.user == user)
        held = tuple(g for g in evidence.groups.get(user, ()) if g in evidence.device_groups)
        guests.append(
            Guest(
                user=user,
                uid=evidence.uids.get(user, mine[0].uid if mine else -1),
                processes=mine,
                linger=user in evidence.linger,
                groups=held,
                dropin=evidence.dropins.get(user, DROPIN_UNKNOWN),
                guarded_by_owner=user in evidence.guarded,
            )
        )
    return Diagnosis(seated, tuple(guests), errors=evidence.errors)


def age_text(seconds: int) -> str:
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days} d {hours} h"
    return f"{hours} h {rest // 60} min" if hours else f"{rest // 60} min"


def describe(guest: Guest) -> str:
    parts = []
    if guest.processes:
        names = ", ".join(sorted({p.comm for p in guest.processes}))
        wp = guest.wireplumber
        parts.append(f"runs {names}" + (f" (wireplumber up {age_text(wp.age_s)})" if wp else ""))
    parts.append("linger on (their services start at boot and never stop)" if guest.linger else "no linger")
    parts.append(
        f"in group {', '.join(guest.groups)}: may open every sound card and camera"
        if guest.groups
        else "in no device group: the seat ACL does not name them"
    )
    parts.append(
        {
            DROPIN_FEATURES: "a WirePlumber drop-in switches the hardware features off",
            DROPIN_CARDS: "a drop-in disables the studio cards only (new gear is not covered)",
            DROPIN_NONE: "no hardware drop-in in their WirePlumber config",
            DROPIN_UNKNOWN: "their config is not readable by this user, so a drop-in cannot be checked",
        }[guest.dropin]
    )
    if guest.guarded_by_owner:
        parts.append("the owner confirmed the policy (STREAMSTACK_SEAT_GUARDED)")
    return f"{guest.user}: " + "; ".join(parts)


# -- the fix, for the owner to run -----------------------------------------------------------------------------------


class SeatError(Exception):
    """A request for a fix script that cannot be answered (unknown option, the seat user, no such user)."""


def _user_env(uid: int) -> str:
    return f"XDG_RUNTIME_DIR=/run/user/{uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus"


def fix_script(user: str, option: str, evidence: Evidence) -> str:
    """The commands that apply one option for one other user, as a shell script to read and run with sudo. Nothing
    here is executed by StreamStack."""
    if option not in OPTIONS:
        raise SeatError(f"option must be one of: {', '.join(OPTIONS)}.")
    seated = seat_user(evidence)
    if user == seated:
        raise SeatError(f"{user} is the person at the seat; the policy is for the other users.")
    uid = evidence.uids.get(user)
    if uid is None:
        raise SeatError(f"no user named {user!r} on this machine.")
    home = evidence.homes.get(user) or f"/home/{user}"
    env = _user_env(uid)
    head = [
        "#!/bin/sh",
        f"# StreamStack seat policy, option {option}, for {user} (uid {uid}); the person at the seat is {seated}.",
        "# Read it, then run it as root (sudo sh <file>). StreamStack never runs it for you.",
        "# Do not run it during a call: restarting a WirePlumber briefly drops that user's audio streams.",
        "set -eu",
    ]
    if option == "dropin":
        target = f"{home}/{DROPIN_DIR}/{DROPIN_NAME}"
        body = [
            f"install -d -o {user} -g {user} -m 0755 {home}/{DROPIN_DIR}",
            f"cat > {target} <<'EOF'",
            DROPIN_TEXT.rstrip("\n"),
            "EOF",
            f"chown {user}:{user} {target}",
            "chmod 0644 " + target,
            f"sudo -u {user} env {env} systemctl --user restart wireplumber.service",
            f"echo 'Undo: rm {target} && sudo -u {user} env {env} systemctl --user restart wireplumber.service'",
        ]
    elif option == "groups":
        held = [g for g in evidence.device_groups if g in evidence.groups.get(user, ())]
        body = [f"gpasswd -d {user} {group}" for group in held]
        body += [
            "# Group changes reach running services only after the user manager restarts; this ends that user's",
            "# sessions and services (their SSH audio too):",
            f"systemctl restart user@{uid}.service",
            "echo 'Undo: " + "; ".join(f"gpasswd -a {user} {g}" for g in held) + "'",
        ]
    else:
        body = [
            f"sudo -u {user} env {env} systemctl --user stop wireplumber.service",
            f"sudo -u {user} env {env} systemctl --user mask wireplumber.service",
            f"echo 'Undo: sudo -u {user} env {env} systemctl --user unmask wireplumber.service'",
        ]
    return "\n".join(head + body) + "\n"


# -- doctor rows -----------------------------------------------------------------------------------------------------


def _row(state: str, title: str, detail: str = "", hint: str = "") -> dict[str, str]:
    return {"id": CHECK_ID, "group": GROUP, "state": state, "title": title, "detail": detail, "hint": hint}


def rows(diagnosis: Diagnosis) -> list[dict[str, str]]:
    """The doctor row. Nothing to say (no other user has an audio stack) is no row; a guarded desk is a green row."""
    if not diagnosis.guests:
        return []
    detail = " | ".join(describe(g) for g in diagnosis.guests)
    names = ", ".join(g.user for g in diagnosis.guests)
    worst = diagnosis.worst
    first = next(g for g in diagnosis.guests if g.verdict == worst)
    command = f"streamstack seat-policy script --user {first.user}"
    if worst == CONTENDS:
        return [
            _row(
                "warn",
                f"{names} runs a second WirePlumber that can open this desk's sound cards and cameras",
                detail,
                f"the two users' WirePlumbers can probe the same card at once (a lost duplex profile, #125 B). The "
                f"policy is a drop-in that switches their hardware features off: `{command}` prints the commands "
                f"for you to run as root; or remove them from {'/'.join(diagnosis.guests[0].groups or ('audio',))} "
                f"(`{command} --option groups`)",
            )
        ]
    if worst == UNVERIFIED:
        return [
            _row(
                "warn",
                f"{names} runs a second WirePlumber; their config is out of sight, so the policy cannot be checked",
                detail,
                f"if you applied the policy for them, record it with STREAMSTACK_SEAT_GUARDED={first.user} in "
                f"~/.config/streamstack/streamstack.env; otherwise `{command}` prints the drop-in to apply",
            )
        ]
    if worst == LINGERING:
        return [
            _row(
                "warn",
                f"{names} has linger and device access: their WirePlumber starts at boot",
                detail,
                f"`{command}` prints the drop-in that keeps it off the studio hardware",
            )
        ]
    return [_row("ok", f"seat policy: {names} cannot take this desk's studio hardware", detail)]
