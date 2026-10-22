"""Microphone watchdog: detect a stalled capture node and bring it back.

The incident this exists for (2026-10-22, mid-call): the Wave XLR capture node showed RUNNING in
`pactl list short sources`, but nothing reached the call. `parec` returned 0 bytes. A card profile cycle
did not help; `pactl suspend-source <mic> 1; pactl suspend-source <mic> 0` fixed it at once.

The ground truth: the ALSA PCM itself
-------------------------------------
PipeWire's own views are NOT reliable evidence. `pw-dump` dumps a stalled node exactly like a healthy
one, and `pw-top -b` only reports the driver that happened to run in the sampled cycle: with two
independent drivers (the USB mic and a Bluetooth sink) a perfectly healthy mic flips between
`R 1024 48000` and `R 0 0 ??? ???` from one snapshot to the next. Neither is used here.

The kernel is the arbiter. For an ALSA source the PCM's procfs file
`/proc/asound/card{C}/pcm{D}c/sub{S}/status` says whether the device is capturing (`state: RUNNING`)
and carries two frame counters: `hw_ptr` (where the hardware has written) and `appl_ptr` (where the
reader, PipeWire, has consumed up to). A healthy 48 kHz microphone advances both by about 48000 frames
a second, `appl_ptr` trailing `hw_ptr` by a period or two. The card/device/subdevice come from the
source's own properties (`alsa.card`, `alsa.device`, `alsa.subdevice`, `device.api`) in
`pactl -f json list sources`.

A microphone is stalled when it is present, unmuted and RUNNING in pactl (somebody is capturing from
it) and, over a window of at least five seconds, the PCM

* is not `RUNNING` at all (closed, XRUN, SETUP, ...), or
* `hw_ptr` does not advance (the device delivers nothing), or
* `hw_ptr` advances but `appl_ptr` does not (PipeWire is not reading).

Sources that are not ALSA (Bluetooth, virtual) are never judged: stalled is "unknown", there is no check
and no banner, rather than a guess. The meter `parec` receiving no bytes is only corroboration, mentioned
in the detail when the ALSA signal already says stalled, never a trigger on its own.

Nothing here ever revives a microphone by itself: that is an explicit action (Console button, CLI).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import system

WINDOW_S = 5.0  # the PCM must show the fault over at least this long before it is believed
SAMPLE_S = 1.0  # one cheap procfs read per second
CLEAR_S = 2.0  # the flag drops only after the PCM looked healthy this long (no flapping)
VERIFY_S = 4.0  # a revive step is judged within this long (healthy as soon as both pointers move)
PACTL_TIMEOUT = 5.0
SUSPEND_GAP_S = 0.4  # between suspend and resume
SETTLE_S = 1.5  # let the node come back before looking again
PROFILE_OFF_S = 1.0
SOURCE_BACK_S = 6.0  # how long to wait for the source to reappear after a profile cycle
PROC_ASOUND = Path("/proc/asound")

CHECK_ID = "mic-stall"
STALL_TITLE = "Microphone stalled - nobody can hear you"
STALL_HINT = "press Revive mic in the Console, or run `streamstack mic-revive`"
REPLUG_HINT = (
    "Still stalled after suspend/resume and a card profile cycle. Unplug and replug the microphone's USB cable, "
    "or run `systemctl --user restart wireplumber` (that briefly drops every audio stream, so it is never done "
    "automatically)."
)

Run = Callable[..., Any]
ReadStatus = Callable[["AlsaPcm"], "PcmSample | None"]


# ---------------------------------------------------------------------------
# Mapping a pactl source to its ALSA PCM
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlsaPcm:
    card: int
    device: int
    subdevice: int = 0

    @property
    def path(self) -> Path:
        return PROC_ASOUND / f"card{self.card}" / f"pcm{self.device}c" / f"sub{self.subdevice}" / "status"


@dataclass(frozen=True)
class SourceInfo:
    """What `pactl -f json list sources` says about one source, as far as the watchdog cares."""

    name: str
    state: str  # RUNNING | IDLE | SUSPENDED
    muted: bool
    pcm: AlsaPcm | None  # None for anything that is not an ALSA capture device


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_source(sources_json: str, name: str) -> SourceInfo | None:
    """The named source from `pactl -f json list sources` (None when absent or unreadable)."""
    try:
        sources = json.loads(sources_json)
    except ValueError:
        return None
    entry = (
        next((s for s in sources if isinstance(s, dict) and s.get("name") == name), None)
        if isinstance(sources, list)
        else None
    )
    if entry is None:
        return None
    props = entry.get("properties")
    props = props if isinstance(props, dict) else {}
    pcm: AlsaPcm | None = None
    card = _int(props.get("alsa.card"))
    if card is None:
        card = _int(props.get("api.alsa.pcm.card"))
    device = _int(props.get("alsa.device"))
    capture = props.get("api.alsa.pcm.stream", "capture") == "capture"
    if props.get("device.api") == "alsa" and card is not None and device is not None and capture:
        sub = _int(props.get("alsa.subdevice"))
        pcm = AlsaPcm(card, device, sub if sub is not None else 0)
    return SourceInfo(name, str(entry.get("state", "")).upper(), bool(entry.get("mute", False)), pcm)


# ---------------------------------------------------------------------------
# /proc/asound/.../status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PcmSample:
    state: str  # RUNNING | PREPARED | XRUN | SETUP | closed | ...
    hw_ptr: int | None = None
    appl_ptr: int | None = None
    at: float = 0.0


def parse_pcm_status(text: str, at: float = 0.0) -> PcmSample | None:
    """A PCM status file as a sample (None when it is not one). A closed device is just `closed`."""
    fields: dict[str, str] = {}
    first = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("-"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
        elif not first:
            first = line
    state = fields.get("state") or first
    if not state:
        return None
    return PcmSample(state, _int(fields.get("hw_ptr")), _int(fields.get("appl_ptr")), at)


def read_pcm_status(pcm: AlsaPcm, clock: Callable[[], float] = time.monotonic) -> PcmSample | None:
    """Read the live status file; None when it cannot be read (no such card, no permission)."""
    try:
        text = pcm.path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    return parse_pcm_status(text, clock())


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StallProbe:
    """One verdict: `stalled` is None when it cannot be judged (non-ALSA, too early, unreadable)."""

    stalled: bool | None
    state: str = "unknown"  # watching | healthy | stalled | muted | idle | absent | unsupported | unreadable
    detail: str = ""


def judge(samples: list[PcmSample]) -> StallProbe:
    """Is a PCM that something is capturing from actually moving? `samples` span the whole window."""
    running = [s for s in samples if s.state == "RUNNING"]
    if not running:
        states = sorted({s.state for s in samples})
        return StallProbe(True, "stalled", f"the ALSA capture device is {'/'.join(states)}, not RUNNING")
    first, last = running[0], running[-1]
    if first is last or last.at <= first.at:
        return StallProbe(None, "watching", "not enough samples yet")
    hw = _advance(first.hw_ptr, last.hw_ptr)
    if hw is False:
        return StallProbe(True, "stalled", "the device's hw_ptr is not advancing: the microphone delivers no audio")
    appl = _advance(first.appl_ptr, last.appl_ptr)
    if appl is False:
        return StallProbe(
            True, "stalled", "the device captures but PipeWire is not reading it (appl_ptr is not advancing)"
        )
    if hw is None or appl is None:
        return StallProbe(None, "unreadable", "the PCM status has no pointers to compare")
    return StallProbe(False, "healthy", "the microphone is captured and consumed (hw_ptr and appl_ptr advance)")


def _advance(a: int | None, b: int | None) -> bool | None:
    """Did a counter move between two reads? None when unknown. A decrease is a wrap, which is movement."""
    if a is None or b is None:
        return None
    return a != b


class StallMonitor:
    """Samples the microphone's ALSA PCM once per `SAMPLE_S` and judges it over `window` seconds.

    Stateful (it keeps the recent samples); `probe()` is cheap, so the supervisor calls it every second.
    `read`, `clock` are injected for tests.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        read: Callable[[AlsaPcm], PcmSample | None] | None = None,
        window: float = WINDOW_S,
    ) -> None:
        self._clock = clock
        self._read = read
        self.window = window
        self._samples: list[PcmSample] = []
        self._pcm: AlsaPcm | None = None

    def reset(self) -> None:
        self._samples.clear()

    def _sample(self, pcm: AlsaPcm) -> PcmSample | None:
        if self._read is not None:
            got = self._read(pcm)
            return PcmSample(got.state, got.hw_ptr, got.appl_ptr, self._clock()) if got else None
        return read_pcm_status(pcm, self._clock)

    def probe(self, mic: str, sources_json: str, silent: bool = False) -> StallProbe:
        """Judge `mic` from a fresh `pactl -f json list sources` text. `silent`: our own meter heard nothing."""
        info = parse_source(sources_json, mic)
        if info is None:
            self.reset()
            return StallProbe(None, "absent", "the microphone is not listed")
        if info.pcm is None:
            self.reset()
            return StallProbe(None, "unsupported", "not an ALSA device: no stall detection for this source")
        if info.muted:
            self.reset()
            return StallProbe(False, "muted", "the microphone is muted")
        if info.state != "RUNNING":
            self.reset()
            return StallProbe(False, "idle", f"nothing is capturing (source is {info.state.lower() or 'unknown'})")
        if info.pcm != self._pcm:
            self.reset()
            self._pcm = info.pcm
        sample = self._sample(info.pcm)
        if sample is None:
            self.reset()
            return StallProbe(None, "unreadable", f"cannot read {info.pcm.path}")
        now = sample.at
        self._samples = [s for s in self._samples if now - s.at <= self.window + SAMPLE_S] + [sample]
        if now - self._samples[0].at < self.window:
            return StallProbe(None, "watching", "watching the microphone")
        verdict = judge(self._samples)
        if verdict.stalled and silent:
            return StallProbe(True, "stalled", verdict.detail + "; our own level meter heard nothing either")
        return verdict

    def verify(
        self,
        mic: str,
        run: Run | None = None,
        sleep: Callable[[float], object] = time.sleep,
        timeout: float = VERIFY_S,
    ) -> bool | None:
        """After a revive step: True while still stalled, False once audio flows, None when it cannot tell.

        Uses the same signal, with a short window: healthy the moment RUNNING and both pointers advance.
        """
        run = run or system.run
        listing = run(["pactl", "-f", "json", "list", "sources"], timeout=PACTL_TIMEOUT)
        info = parse_source(listing.stdout, mic) if listing and listing.returncode == 0 else None
        if info is None or info.pcm is None:
            return None
        samples: list[PcmSample] = []
        start = self._clock()
        while True:
            got = self._sample(info.pcm)
            if got is None:
                return None
            samples.append(got)
            if len(samples) > 1 and judge(samples).stalled is False:
                return False
            if self._clock() - start >= timeout:
                break
            sleep(0.5)
        return True if judge(samples).stalled else None


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------


class StallDetector:
    """Turns a yes/no condition into a stable flag: set after `confirm` seconds (the monitor already waited out
    its own window, so 0 by default), cleared after `clear`.

    Pure logic with an injected clock, so it is tested without waiting.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        confirm: float = 0.0,
        clear: float = CLEAR_S,
    ) -> None:
        self._clock, self._wall = clock, wall
        self.confirm, self.clear = confirm, clear
        self.stalled = False
        self.since: str | None = None  # ISO local time the condition began
        self._bad_at: float | None = None
        self._bad_wall = 0.0
        self._good_at: float | None = None

    def reset(self) -> None:
        self.stalled, self.since = False, None
        self._bad_at = self._good_at = None

    def update(self, condition: bool) -> str | None:
        """Feed the current condition; returns "started" or "cleared" on the tick the flag flips."""
        now = self._clock()
        if condition:
            self._good_at = None
            if self._bad_at is None:
                self._bad_at, self._bad_wall = now, self._wall()
            if not self.stalled and now - self._bad_at >= self.confirm:
                self.stalled = True
                self.since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._bad_wall))
                return "started"
            return None
        if not self.stalled:
            self._bad_at = None
            return None
        if self._good_at is None:
            self._good_at = now
        if now - self._good_at >= self.clear:
            self.reset()
            return "cleared"
        return None


# ---------------------------------------------------------------------------
# The health check
# ---------------------------------------------------------------------------


def check_item(stalled: bool | None, detail: str = "") -> dict[str, str] | None:
    """The doctor / Console health check (same shape as cli.check); None when there is nothing to say."""
    if stalled is None:
        return None
    if stalled:
        return {
            "id": CHECK_ID,
            "group": "audio",
            "state": "fail",
            "title": STALL_TITLE,
            "detail": detail or "the microphone is capturing nothing that reaches PipeWire",
            "hint": STALL_HINT,
        }
    return {
        "id": CHECK_ID,
        "group": "audio",
        "state": "ok",
        "title": "microphone is delivering audio",
        "detail": detail,
        "hint": "",
    }


# ---------------------------------------------------------------------------
# Revive
# ---------------------------------------------------------------------------


@dataclass
class ReviveResult:
    ok: bool
    message: str
    steps: list[str] = field(default_factory=list)


def find_card(sources_json: str, cards_json: str, source: str) -> tuple[str, str] | None:
    """(card name, active profile) of the card the source belongs to, from `pactl -f json list sources|cards`."""
    try:
        sources = json.loads(sources_json)
        cards = json.loads(cards_json)
    except ValueError:
        return None
    entry = next((s for s in sources if isinstance(s, dict) and s.get("name") == source), None)
    props = entry.get("properties") if isinstance(entry, dict) else None
    if not isinstance(props, dict) or not isinstance(cards, list):
        return None
    wanted = props.get("device.name")
    alsa_card = props.get("alsa.card")
    for card in cards:
        if not isinstance(card, dict) or not card.get("name") or not card.get("active_profile"):
            continue
        card_props = card.get("properties") if isinstance(card.get("properties"), dict) else {}
        if card["name"] == wanted or (alsa_card is not None and str(card_props.get("alsa.card")) == str(alsa_card)):
            return str(card["name"]), str(card["active_profile"])
    return None


def revive_mic(
    mic: str,
    *,
    run: Run | None = None,
    sleep: Callable[[float], object] = time.sleep,
    stalled: Callable[[], bool | None] = lambda: None,
    say: Callable[[str], object] = lambda text: None,
) -> ReviveResult:
    """Bring a stalled microphone back, escalating only as far as needed.

    1. `pactl suspend-source <mic> 1`, 0.4 s, `... 0`, then look again (this fixed the incident at once);
    2. cycle the card profile off and back to the one it had, then restore the default source;
    3. otherwise say what the human has to do (replug, restart wireplumber); nothing drastic is automatic.

    `stalled()` re-checks and returns True/False, or None when it cannot tell (then a finished step is
    reported as done but unverified). `run`, `sleep`, `stalled` and `say` are injected for tests.
    """
    run = run or system.run
    steps: list[str] = []

    def pactl(*args: str) -> bool:
        result = run(["pactl", *args], timeout=PACTL_TIMEOUT)
        return bool(result and result.returncode == 0)

    def verdict(step: str) -> ReviveResult | None:
        state = stalled()
        if state is False:
            return ReviveResult(True, f"Microphone is back ({step}).", steps)
        if state is None:
            return ReviveResult(True, f"{step.capitalize()} done; the microphone could not be verified.", steps)
        return None

    say("Suspending and resuming the microphone...")
    steps.append("suspend/resume")
    if not pactl("suspend-source", mic, "1"):
        return ReviveResult(False, "The audio server did not accept the suspend request (is pactl working?).", steps)
    sleep(SUSPEND_GAP_S)
    resumed = pactl("suspend-source", mic, "0")
    sleep(SETTLE_S)
    if not resumed:
        return ReviveResult(False, "The audio server did not accept the resume request.", steps)
    if (done := verdict("suspend and resume")) is not None:
        return done

    say("Still stalled. Cycling the sound card profile...")
    steps.append("card profile cycle")
    ran, failure = _cycle_profile(mic, run, sleep, pactl, steps)
    if failure is not None:
        return failure
    if ran and (done := verdict("card profile cycle")) is not None:
        return done

    steps.append("manual help needed")
    return ReviveResult(False, REPLUG_HINT, steps)


def _cycle_profile(
    mic: str, run: Run, sleep: Callable[[float], object], pactl: Callable[..., bool], steps: list[str]
) -> tuple[bool, ReviveResult | None]:
    """Card profile off and back on: (whether the cycle ran, a failure result when it went wrong)."""
    sources = run(["pactl", "-f", "json", "list", "sources"], timeout=PACTL_TIMEOUT)
    cards = run(["pactl", "-f", "json", "list", "cards"], timeout=PACTL_TIMEOUT)
    default = run(["pactl", "get-default-source"], timeout=PACTL_TIMEOUT)
    if not (sources and cards and sources.returncode == 0 and cards.returncode == 0):
        steps.append("card lookup failed")
        return False, None
    found = find_card(sources.stdout, cards.stdout, mic)
    if found is None:
        steps.append("card not found")
        return False, None
    card, profile = found
    previous_default = default.stdout.strip() if default and default.returncode == 0 else ""
    if not pactl("set-card-profile", card, "off"):
        return False, ReviveResult(False, "The audio server did not accept the card profile change.", steps)
    sleep(PROFILE_OFF_S)
    restored = pactl("set-card-profile", card, profile)
    deadline = SOURCE_BACK_S
    while restored and deadline > 0 and not _source_listed(run, mic):
        sleep(0.5)
        deadline -= 0.5
    if not restored:
        pactl("set-card-profile", card, profile)  # one more try: leaving the card off would be worse than anything else
        failed = f"Could not restore the card profile {profile!r}; set it in the sound settings."
        return False, ReviveResult(False, failed, steps)
    if previous_default:
        pactl("set-default-source", previous_default)
    sleep(SETTLE_S)
    return True, None


def _source_listed(run: Run, mic: str) -> bool:
    result = run(["pactl", "list", "short", "sources"], timeout=PACTL_TIMEOUT)
    return bool(
        result and result.returncode == 0 and any(mic in line.split("\t") for line in result.stdout.splitlines())
    )
