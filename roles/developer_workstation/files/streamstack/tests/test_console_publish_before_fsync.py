"""Issue #168: a Console action shows its change first and saves it afterwards, for EVERY saving action.

The bug, three times over (#161 telemetry, #163 outputs, #167 presets, then #168 for the rest): an action did its
durable write (an fsync: tens of seconds on a starved disk) while it held the lock `state()` takes, or before it set
the value `state()` reads, so a stalled disk froze the page. Each module now keeps its saves in a
`streamstack.saving.LatestWriter`: change the state under the state lock, publish the value, save outside the lock.

This test is the gate for the whole class. It makes the store's fsyncing write BLOCK, runs each saving action against
a real controller, and asserts that `state()` shows the change while the write is still stuck (and that `state()` does
not itself wait for the disk). Then it releases the write and asserts the action finished without an error.

It enumerates the actions two ways, so a new saving action cannot slip in without a case:

* `test_every_saving_function_has_a_case` scans the source: every function that hands a value to a writer, or
  calls one of the save helpers, must be listed in `COVERED` (with the ids of the cases that run it) or `EXEMPT`
  (with the reason it is not an action path). A new one that is in neither fails.
* `test_every_write_site_is_a_latest_writer` scans for direct writes: in a module that has a writer, a write call
  must sit inside the writer's own `_write`/lambda, never in an action.
* `test_every_writer_is_exercised` runs the cases and fails if any `LatestWriter` the source creates was never
  written to while blocked.
"""

from __future__ import annotations

import ast
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from streamstack import store
from streamstack.saving import LatestWriter

SRC = Path(__file__).resolve().parent.parent / "src" / "streamstack"
WAIT_S = 15.0  # a blocked write must be reached, and a released one finished, within this
VISIBLE_S = 5.0  # the state must show the change within this while the write is still stuck


# --------------------------------------------------------------------------------------- the blocked disk


class Disk:
    """The store's write, stuck until `release`. Every fsyncing write goes through `store.atomic_write_bytes`."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.hits: list[str] = []  # the writers (by source file) that reached the disk
        self._real = store.atomic_write_bytes

    def block(self, *args: Any, **kwargs: Any) -> Any:
        self.entered.set()
        if not self.release.wait(WAIT_S):
            raise OSError("the test never released the disk")
        # The disk is "back" now. The real write still runs (the file, the backup and the rename are all checked), but
        # without its fsyncs: a write is two fsyncs (file, directory) and a loaded runner's fsync can take tens of
        # seconds (infra#639), which is the runner's disk, not the action under test, and WAIT_S cannot tell them apart.
        # A real hang still shows: the blocked write is the one thing this test holds, and once released a thread that
        # is alive after WAIT_S is stuck on something other than disk speed.
        kwargs["durable"] = False
        return self._real(*args, **kwargs)


@pytest.fixture
def disk(monkeypatch: pytest.MonkeyPatch) -> Disk:
    d = Disk()
    monkeypatch.setattr(store, "atomic_write_bytes", d.block)
    # The deck's profile store writes with a plain tempfile and no fsync, but it is a save all the same: stall it too.
    from streamstack.deck import profile

    real_write, real_delete = profile.Store._write, profile.Store.delete

    def write(self: Any, payload: dict) -> None:
        d.entered.set()
        if not d.release.wait(WAIT_S):
            raise OSError("the test never released the disk")
        real_write(self, payload)

    def delete(self: Any) -> None:
        d.entered.set()
        if not d.release.wait(WAIT_S):
            raise OSError("the test never released the disk")
        real_delete(self)

    monkeypatch.setattr(profile.Store, "_write", write)
    monkeypatch.setattr(profile.Store, "delete", delete)
    # Record which writer (by the file that created it) reached the disk.
    real_init = LatestWriter.__init__

    def init(self: LatestWriter, write: Callable[[Any], object]) -> None:
        import sys

        site = Path(sys._getframe(1).f_code.co_filename).resolve().relative_to(SRC).as_posix()

        def recorded(value: Any) -> object:
            d.hits.append(site)
            return write(value)

        real_init(self, recorded)

    monkeypatch.setattr(LatestWriter, "__init__", init)
    return d


# --------------------------------------------------------------------------------------------- the cases


@dataclass
class Case:
    """One saving action: `steps` run in order (each in its own thread, the first one stuck on the disk), and
    `visible()` must be true while it is, from a thread that has to answer within VISIBLE_S."""

    steps: list[Callable[[], object]]
    visible: Callable[[], bool]
    stop: Callable[[], object] = lambda: None


CASES: dict[str, Callable[[Path, pytest.MonkeyPatch], Case]] = {}
CASE_SITES: dict[str, set[str]] = {}  # case id -> the source files whose LatestWriter must reach the disk in it


def case(case_id: str, *sites: str) -> Callable[[Callable[[Path, pytest.MonkeyPatch], Case]], Callable[..., Case]]:
    def register(build: Callable[[Path, pytest.MonkeyPatch], Case]) -> Callable[[Path, pytest.MonkeyPatch], Case]:
        assert case_id not in CASES, case_id
        CASES[case_id] = build
        CASE_SITES[case_id] = set(sites)
        return build

    return register


def run_case(case_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disk: Disk) -> None:
    disk.release.set()  # setting a case up saves too (a first token, a first file): that goes straight through
    built = CASES[case_id](tmp_path, monkeypatch)
    disk.release.clear()
    disk.entered.clear()
    disk.hits.clear()
    errors: list[BaseException] = []
    threads: list[threading.Thread] = []

    def runner(step: Callable[[], object]) -> None:
        try:
            step()
        except BaseException as exc:  # reported below, with the case id
            errors.append(exc)

    try:
        for index, step in enumerate(built.steps):
            thread = threading.Thread(target=runner, args=(step,), daemon=True, name=f"{case_id}#{index}")
            thread.start()
            threads.append(thread)
            if index == 0:
                assert disk.entered.wait(WAIT_S), f"{case_id}: the first step never reached the disk (nothing saved?)"
            else:
                time.sleep(0.2)  # let the later step reach whatever it blocks on
        seen: list[bool] = []
        looker = threading.Thread(target=lambda: seen.append(built.visible()), daemon=True)
        looker.start()
        looker.join(VISIBLE_S)
        assert not looker.is_alive(), f"{case_id}: state() blocked behind the disk write"
        assert seen == [True], f"{case_id} {disk.hits}: the change is not in state() while the write is still blocked"
    finally:
        disk.release.set()
        for thread in threads:
            thread.join(WAIT_S)
        built.stop()
    assert not any(t.is_alive() for t in threads), f"{case_id}: an action never finished after the disk came back"
    assert not errors, f"{case_id}: the action failed once the write finished: {errors!r}"
    wanted = CASE_SITES[case_id]
    assert wanted <= set(disk.hits), f"{case_id}: expected the writers {wanted} to reach the disk, got {disk.hits}"


# ---- telemetry (Diagnostics): the switch, the scrape token


@case("telemetry.set", "console/diagnostics.py")
def _telemetry_set(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    from streamstack.console.diagnostics import Diagnostics

    diag = Diagnostics({})
    return Case(
        [lambda: diag.act("telemetry.set", {"on": True})],
        lambda: diag.state()["telemetry"]["enabled"] is True,
        diag._stop_sampler,
    )


@case("telemetry.set.off_while_the_enable_is_saving", "console/diagnostics.py")
def _telemetry_off_behind_on(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    """The #168 freeze: the next click (off) waited for the first click's save, which held the switch's lock."""
    from streamstack.console.diagnostics import Diagnostics

    diag = Diagnostics({})
    return Case(
        [lambda: diag.act("telemetry.set", {"on": True}), lambda: diag.act("telemetry.set", {"on": False})],
        lambda: diag.state()["telemetry"]["enabled"] is False,
        diag._stop_sampler,
    )


@case("telemetry.rotate_token", "console/diagnostics.py")
def _telemetry_rotate(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    from streamstack.console.diagnostics import Diagnostics

    diag = Diagnostics({})
    old = diag._ensure_token()  # an existing token, saved before the disk stalls
    return Case(
        [lambda: diag.act("telemetry.rotate_token", {})],
        lambda: not diag.token_ok(old),
    )


@case("telemetry.scrape_config", "console/diagnostics.py")
def _telemetry_scrape_config(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    """The first scrape config creates the token: it is in effect (state says so) before the file has it."""
    from streamstack.console.diagnostics import ENABLE_KEY, Diagnostics

    diag = Diagnostics({ENABLE_KEY: "true"})
    return Case(
        [lambda: diag.act("telemetry.scrape_config", {})],
        lambda: diag.state()["telemetry"]["token_set"] is True,
    )


# ---- call, safejoin, network: one settings file each


@case("call.set", "console/call.py")
def _call_set(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    from streamstack.console.call import CallController

    ctl = CallController({}, settings_path=tmp_path / "call.json")
    return Case(
        [lambda: ctl.act("call.set", {"enabled": False})],
        lambda: ctl.state()["muted_warning"]["enabled"] is False,
    )


@case("safejoin.set", "console/safejoin.py")
def _safejoin_set(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    from streamstack.console.safejoin import MODES, SafeJoinController

    ctl = SafeJoinController(settings_path=tmp_path / "safejoin.json")
    other = next(m for m in MODES if m != ctl.state()["mode"])
    return Case(
        [lambda: ctl.act("safejoin.set", {"mode": other})],
        lambda: ctl.state()["mode"] == other,
    )


@case("network.set", "console/network.py")
def _network_set(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    from streamstack.console.network import DemoNetwork

    ctl = DemoNetwork(settings_path=tmp_path / "network.json")
    was = ctl.state()["enabled"]
    return Case(
        [lambda: ctl.act("network.set", {"enabled": not was})],
        lambda: ctl.state()["enabled"] is (not was),
        ctl.stop,
    )


# ---- echo (a background check saves its result), meetings


@case("echo.check", "console/echocheck.py")
def _echo_check(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    """The check is done (state says so, with its result) before the result is on the disk."""
    from streamstack.console.echocheck import DemoEcho
    from streamstack.console.rig import Rig

    hdmi = "alsa_output.demo-monitor.hdmi-stereo"
    ctl = DemoEcho(store_path=tmp_path / "echo.json")
    snapshot = Rig.demo().snapshot()  # taken now: building a rig saves a file or two, and the disk is free during setup
    ctl.bind(lambda: snapshot)
    return Case(
        [lambda: ctl.act("echo.check", {"name": hdmi})],
        lambda: ctl.state()["state"] == "done" and bool(ctl.state()["results"]),
        ctl.stop,
    )


def _meetings(tmp_path: Path) -> Any:
    from streamstack.console.meetings import MeetingsController
    from streamstack.meetings import config as meetings_config

    return MeetingsController(meetings_config.Store(tmp_path / "meetings.json"))


@case("meetings.enable", "console/meetings.py")
def _meetings_enable(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _meetings(tmp_path)
    return Case(
        [lambda: ctl.act("meetings.enable", {"on": True})],
        lambda: ctl.state()["enabled"] is True,
    )


@case("meetings.settings", "console/meetings.py")
def _meetings_settings(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _meetings(tmp_path)
    return Case(
        [lambda: ctl.act("meetings.settings", {"lead_minutes": 25})],
        lambda: ctl.state()["lead_minutes"] == 25,
    )


_ICS = (
    "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:u\r\nSUMMARY:X\r\nDTSTART:20270115T080000Z\r\n"
    "DTEND:20270115T090000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)
_LINK = "https://calendar.example.com/feed/abc123secret.ics"


def _meetings_with_calendars(tmp_path: Path) -> Any:
    from streamstack.console.meetings import MeetingsController
    from streamstack.meetings import config as meetings_config
    from streamstack.meetings.tokens import MemorySecrets

    saved = meetings_config.Store(tmp_path / "meetings.json")
    return MeetingsController(saved, MemorySecrets(), fetch=lambda url: _ICS)


@case("meetings.ics.add", "console/meetings.py")
def _meetings_ics_add(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _meetings_with_calendars(tmp_path)
    return Case(
        [lambda: ctl.act("meetings.ics.add", {"link": _LINK, "name": "Work"})],
        lambda: any(src["kind"] == "ics" for src in ctl.state()["sources"]),
    )


@case("meetings.ics.remove", "console/meetings.py")
def _meetings_ics_remove(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _meetings_with_calendars(tmp_path)
    ctl.act("meetings.ics.add", {"link": _LINK, "name": "Work"})
    ident = next(src["id"] for src in ctl.state()["sources"] if src["kind"] == "ics")
    return Case(
        [lambda: ctl.act("meetings.ics.remove", {"id": ident})],
        lambda: not any(src["kind"] == "ics" for src in ctl.state()["sources"]),
    )


# ---- hotkeys


class _NoMidi:
    reason: str | None = None


def _hotkeys(tmp_path: Path) -> Any:
    from streamstack import hotkeys
    from streamstack.console.hotkeys import HotkeysController

    return HotkeysController(hotkeys.Store(tmp_path / "hotkeys.json"), lambda a, b: None, lambda a, b: _NoMidi())


F9 = {"kind": "key", "combo": "f9"}
F10 = {"kind": "key", "combo": "f10"}


@case("hotkeys.add", "console/hotkeys.py")
def _hotkeys_add(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _hotkeys(tmp_path)
    return Case(
        [lambda: ctl.act("hotkeys.add", {"trigger": F9, "action": "audio.mute"})],
        lambda: len(ctl.state()["bindings"]) == 1,
    )


def _with_one_binding(tmp_path: Path) -> Any:
    ctl = _hotkeys(tmp_path)
    ctl.act("hotkeys.add", {"id": "one", "trigger": F9, "action": "audio.mute"})
    return ctl


@case("hotkeys.update", "console/hotkeys.py")
def _hotkeys_update(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _with_one_binding(tmp_path)
    return Case(
        [lambda: ctl.act("hotkeys.update", {"id": "one", "label": "Mute me"})],
        lambda: ctl.state()["bindings"][0].get("label") == "Mute me",
    )


@case("hotkeys.remove", "console/hotkeys.py")
def _hotkeys_remove(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _with_one_binding(tmp_path)
    return Case([lambda: ctl.act("hotkeys.remove", {"id": "one"})], lambda: ctl.state()["bindings"] == [])


@case("hotkeys.clear", "console/hotkeys.py")
def _hotkeys_clear(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _with_one_binding(tmp_path)
    return Case([lambda: ctl.act("hotkeys.clear", {})], lambda: ctl.state()["bindings"] == [])


@case("hotkeys.import", "console/hotkeys.py")
def _hotkeys_import(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _hotkeys(tmp_path)
    document = [{"id": "two", "trigger": F10, "action": "audio.mute"}]
    return Case(
        [lambda: ctl.act("hotkeys.import", {"document": document})],
        lambda: [b["id"] for b in ctl.state()["bindings"]] == ["two"],
    )


@case("hotkeys.enable", "console/hotkeys.py")
def _hotkeys_enable(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _with_one_binding(tmp_path)
    return Case([lambda: ctl.act("hotkeys.enable", {"on": False})], lambda: ctl.state()["enabled"] is False)


# ---- the deck


def _deck(tmp_path: Path) -> Any:
    from streamstack.console import deck as console_deck
    from streamstack.deck import profile

    return console_deck.DeckController(profile.Store(tmp_path / "deck.json"), lambda: 100.0)


@case("deck.page_add", "console/deck.py")
def _deck_page_add(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _deck(tmp_path)
    return Case(
        [lambda: ctl.act("deck.page_add", {"name": "Streaming"})],
        lambda: "Streaming" in [p["name"] for p in ctl.state({})["profile"]["pages"]],
    )


@case("deck.settings", "console/deck.py")
def _deck_settings(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _deck(tmp_path)
    return Case(
        [lambda: ctl.act("deck.settings", {"brightness": 33})],
        lambda: ctl.state({})["profile"]["settings"]["brightness"] == 33,
    )


@case("deck.reset", "console/deck.py")
def _deck_reset(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _deck(tmp_path)
    ctl.act("deck.page_add", {"name": "Streaming"})  # a saved profile to go back from
    return Case([lambda: ctl.act("deck.reset", {})], lambda: ctl.state({})["profile"]["pristine"] is True)


# ---- lighting: presets, the automation settings


def _lighting(tmp_path: Path) -> Any:
    from streamstack import lightplan
    from streamstack.console.lighting import DemoLighting, PresetLibrary

    ctl = DemoLighting(library=PresetLibrary(tmp_path / "lights.json"), plan=lightplan.Store(tmp_path / "plan.json"))
    ctl.start()
    return ctl


@case("lighting.presets.save", "console/lighting.py")
def _lighting_save(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _lighting(tmp_path)
    return Case(
        [lambda: ctl.act("lighting.presets.save", {"name": "Podcast"})],
        lambda: "Podcast" in [p["name"] for p in ctl.state()["presets"]],
        ctl.stop,
    )


@case("lighting.presets.delete", "console/lighting.py")
def _lighting_delete(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _lighting(tmp_path)
    ctl.act("lighting.presets.save", {"name": "Podcast"})
    return Case(
        [lambda: ctl.act("lighting.presets.delete", {"name": "Podcast"})],
        lambda: "Podcast" not in [p["name"] for p in ctl.state()["presets"]],
        ctl.stop,
    )


@case("lighting.auto.follow", "lightplan.py")
def _lighting_auto(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _lighting(tmp_path)
    return Case(
        [lambda: ctl.act("lighting.auto.follow", {"grace_seconds": 45})],
        lambda: ctl.state()["automation"]["settings"]["follow"]["grace_seconds"] == 45,
        ctl.stop,
    )


# ---- video looks


@case("video.looks.save", "console/video.py")
def _video_save(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    from streamstack.console.video import DemoVideo, LookLibrary

    ctl = DemoVideo(library=LookLibrary(tmp_path / "looks.json"))
    return Case(
        [lambda: ctl.act("video.looks.save", {"name": "Mine"})],
        lambda: any(x["name"] == "Mine" for x in ctl.state()["looks"]),
    )


@case("video.looks.delete", "console/video.py")
def _video_delete(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    from streamstack.console.video import DemoVideo, LookLibrary

    ctl = DemoVideo(library=LookLibrary(tmp_path / "looks.json"))
    ctl.act("video.looks.save", {"name": "Mine"})
    return Case(
        [lambda: ctl.act("video.looks.delete", {"name": "Mine"})],
        lambda: not any(x["name"] == "Mine" for x in ctl.state()["looks"]),
    )


# ---- presets: the platform and tier choice, applying a profile (ProfileStore saves under its own lock, no writer)


def _presets(tmp_path: Path) -> Any:
    from streamstack.console.presets import PresetsController
    from streamstack.presets import catalog
    from streamstack.presets.store import ProfileStore

    cat = catalog.load()
    profiles = ProfileStore(
        tmp_path / "profiles.json",
        platforms={p["id"] for p in cat.platforms},
        tiers={t["id"] for t in cat.tiers},
    )
    return PresetsController(lambda action, params: f"did {action}", profiles)


@case("presets.platform")
def _presets_platform(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _presets(tmp_path)
    return Case(
        [lambda: ctl.act("presets.platform", {"id": "kick"})],
        lambda: ctl.state()["platform"] == "kick",
    )


@case("presets.tier")
def _presets_tier(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _presets(tmp_path)
    return Case(
        [lambda: ctl.act("presets.tier", {"id": "cellular"})],
        lambda: ctl.state()["tier"] == "cellular",
    )


@case("presets.profile.apply")
def _presets_apply(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _presets(tmp_path)

    def shown() -> bool:
        s = ctl.state()
        return bool(
            s["profile"] == "Interview"
            and (s["platform"], s["tier"]) == ("youtube", "high")
            and s["last_apply"]["steps"][-1]["status"] == "ok"
        )

    return Case([lambda: ctl.act("presets.profile.apply", {"id": "interview"})], shown)


# ---- eye-line coach and auto-framing (their state file)


def _eyeline(tmp_path: Path) -> Any:
    from streamstack.console.eyeline import EyeLineController

    class Ready(EyeLineController):
        def _readiness(self) -> tuple[bool, str, str]:
            return True, "", ""

    return Ready({}, None, state_file=tmp_path / "eyeline.json")


@case("eyeline.enable", "console/eyeline.py")
def _eyeline_enable(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _eyeline(tmp_path)
    return Case([lambda: ctl.act("eyeline.enable", {"on": True})], lambda: ctl.state()["enabled"] is True)


@case("eyeline.reminder", "console/eyeline.py")
def _eyeline_reminder(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _eyeline(tmp_path)
    return Case(
        [lambda: ctl.act("eyeline.reminder", {"on": True, "minutes": 20})],
        lambda: ctl.state()["reminder"]["on"] is True,
    )


@case("eyeline.calibration.clear", "console/eyeline.py")
def _eyeline_clear(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _eyeline(tmp_path)
    return Case([lambda: ctl.act("eyeline.calibration.clear", {})], lambda: ctl.state()["calibrated"] is False)


def _framing(tmp_path: Path, mp: pytest.MonkeyPatch) -> Any:
    from types import SimpleNamespace

    from streamstack import config
    from streamstack.console import framing as console_framing
    from streamstack.framing import health

    mp.setattr(health, "readiness", lambda: (True, "", ""))
    return console_framing.FramingController(
        {**config.DEFAULTS}, SimpleNamespace(), None, opener=lambda device: None, state_file=tmp_path / "framing.json"
    )


@case("framing.mode", "console/framing.py")
def _framing_mode(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _framing(tmp_path, mp)
    return Case([lambda: ctl.act("framing.mode", {"mode": "tight"})], lambda: ctl.state()["mode"] == "tight")


@case("framing.shot", "console/framing.py")
def _framing_shot(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _framing(tmp_path, mp)
    ctl.act("framing.mode", {"mode": "gentle"})
    return Case([lambda: ctl.act("framing.shot", {"shot": "close"})], lambda: ctl.state()["shot"] == "close")


@case("framing.subjects", "console/framing.py")
def _framing_subjects(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    from streamstack.framing.engine import SUBJECTS

    ctl = _framing(tmp_path, mp)
    other = next(name for name in SUBJECTS if name != ctl.state()["subjects"])
    return Case([lambda: ctl.act("framing.subjects", {"subjects": other})], lambda: ctl.state()["subjects"] == other)


@case("framing.handoff", "console/framing.py")
def _framing_handoff(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _framing(tmp_path, mp)
    on = not ctl.state()["handoff"]
    return Case([lambda: ctl.act("framing.handoff", {"on": on})], lambda: ctl.state()["handoff"] is on)


# ---- time-of-day profiles


def _slots(tmp_path: Path, snapshot: dict | None = None) -> Any:
    from types import SimpleNamespace

    from streamstack import slots as slot_store
    from streamstack.console.slots import SlotsController

    ctl = SlotsController(lambda action, params: None, slot_store.Store(tmp_path / "slots.json"), seed=None)
    ctl.rig = SimpleNamespace(snapshot=lambda: snapshot or {})
    return ctl


@case("slots.mode", "slots.py")
def _slots_mode(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _slots(tmp_path)
    return Case([lambda: ctl.act("slots.mode", {"mode": "suggest"})], lambda: ctl.state({})["mode"] == "suggest")


@case("slots.fade", "slots.py")
def _slots_fade(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _slots(tmp_path)
    return Case([lambda: ctl.act("slots.fade", {"seconds": 9})], lambda: ctl.state({})["fade_seconds"] == 9)


@case("slots.profile", "slots.py")
def _slots_profile(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _slots(tmp_path)
    return Case(
        [lambda: ctl.act("slots.profile", {"slot": "dawn", "profile": {"lighting": "Soft"}})],
        lambda: ctl.store.load()["profiles"].get("dawn") == {"lighting": "Soft"},
    )


@case("slots.starter", "slots.py")
def _slots_starter(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    ctl = _slots(tmp_path)
    return Case(
        [lambda: ctl.act("slots.starter", {})],
        lambda: any(s.get("profile") for s in ctl.state({})["slots"]),
    )


@case("slots.apply")
def _slots_apply(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    """The profile shows as applied before the Director's baseline (an fsynced learning record) is written."""
    preset = {"name": "Soft", "on": True, "brightness": 60, "kelvin": 4000}
    snapshot = {"auto": {"running": True}, "lighting": {"presets": [preset], "master": {}}}
    ctl = _slots(tmp_path, snapshot)
    ctl.act("slots.profile", {"slot": "midday", "profile": {"lighting": "Soft"}})
    return Case(
        [lambda: ctl.act("slots.apply", {"slot": "midday"})],
        lambda: ctl.state(snapshot)["status"]["state"] == "applied",
    )


# ---- the director panel (its choices and personas)


class _Panel:
    @staticmethod
    def make() -> Any:
        from datetime import UTC, datetime

        from streamstack.console import director

        class Panel(director.DirectorPanel):
            def _now(self) -> datetime:
                return datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

        return Panel({})


def _persona_doc(persona_id: str = "executive") -> dict:
    import json

    from streamstack.director import personas

    return json.loads((personas.shipped_dir() / f"{persona_id}.json").read_text())


@case("auto.persona", "console/director.py")
def _auto_persona(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    panel = _Panel.make()
    return Case(
        [lambda: panel.act("auto.persona", {"id": "late-night-calm"})],
        lambda: panel.state()["persona"] == "late-night-calm",
    )


@case("auto.location", "console/director.py")
def _auto_location(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    panel = _Panel.make()
    return Case(
        [lambda: panel.act("auto.location", {"name": "Lake house", "latitude": 44.0, "longitude": -79.5})],
        lambda: panel.state()["time"]["location"]["latitude"] == 44.0,
    )


@case("auto.persona_save")
def _auto_persona_save(tmp_path: Path, mp: pytest.MonkeyPatch) -> Case:
    panel = _Panel.make()
    edited = _persona_doc()
    edited["name"] = "Executive, my way"
    return Case(
        [lambda: panel.act("auto.persona_save", {"persona": edited})],
        lambda: {p["id"]: p["name"] for p in panel.state()["personas"]}["executive"] == "Executive, my way",
    )


# ----------------------------------------------------------------------------- the enumeration (static)

# Direct calls that end in an fsyncing file write.
WRITE_CALLS = {"write_json", "atomic_write_text", "atomic_write_bytes", "update_env_file", "set_user_setting"}
# Calls an action makes to put a change on its way to the disk (the writer's two halves and each module's save helper).
SAVE_CALLS = {
    "publish", "flush", "_save", "_save_mode", "_commit", "_stage", "_change_prefs", "_ensure_token", "_save_results",
    "_edit", "_queue_persona", "_seed",
}  # fmt: skip
# Store methods that save, by the module whose actions call them (the call has no writer name in it).
STORE_CALLS = {
    "console/lighting.py": {"save", "delete"},
    "console/video.py": {"save", "delete"},
    "console/slots.py": {
        "set_mode", "set_fade", "set_profile", "clear_profile", "fill_starter", "import_document", "reset",
    },
    "console/lightauto.py": {"update", "set_slot", "set_hour", "set_light", "seed_hours", "reset", "resume"},
    "console/director.py": {"remove_user"},
}  # fmt: skip
SCANNED = [
    "console/*.py", "lightplan.py", "slots.py", "hotkeys.py", "meetings/config.py", "deck/profile.py",
    "director/settings.py", "director/personas.py", "director/learning.py", "director/status.py", "presets/store.py",
    "config.py", "auto.py",
]  # fmt: skip

# Every function that writes a file directly, and why it is not a saving ACTION (or which writer it serves).
WRITE_SITES: dict[str, str] = {
    # the writers' own saves: reached only through `LatestWriter.flush`, outside every state lock
    "console/call.py:CallController.__init__": "writer",
    "console/diagnostics.py:Diagnostics.__init__": "writer",
    "console/echocheck.py:EchoController.__init__": "writer",
    "console/eyeline.py:EyeLineController.__init__": "writer",
    "console/framing.py:FramingController.__init__": "writer",
    "console/lighting.py:PresetLibrary._write": "writer",
    "console/network.py:NetworkController.__init__": "writer",
    "console/safejoin.py:SafeJoinController.__init__": "writer",
    "console/video.py:LookLibrary._write": "writer",
    "lightplan.py:Store._write": "writer",
    "slots.py:Store._write": "writer",
    "hotkeys.py:Store.save": "writer (HotkeysController)",
    "meetings/config.py:Store.save": "writer (MeetingsController)",
    "director/settings.py:save": "writer (DirectorPanel._prefs_writer)",
    "director/settings.py:write_json": "the helper under settings.save and the status heartbeat",
    "config.py:set_user_setting": "writer (the telemetry switch)",
    # the director's persona files: queued under their own save lock, shown from `_persona_pending` first
    "director/personas.py:save_user": "queued by DirectorPanel._queue_persona (case auto.persona_save)",
    # not Console actions
    "console/launcher.py:write_runtime": "startup only: the pid and port file, before any page is served",
    "console/outputs.py:Outputs._save_results": "own fix: !166 (#163)",
    "console/preflight.py:PreflightController._save_history": "own fix: already on main (the history is saved last)",
    "presets/store.py:ProfileStore._write": "no writer: shows the choice under its own lock first (cases presets.*)",
    "director/learning.py:record": "the Director's own baseline (its loop; after the profile shows: slots.apply)",
    "director/status.py:publish_sheet": "the Director's heartbeat: no fsync (durable=False)",
    "director/status.py:write": "the Director's heartbeat: no fsync (ephemeral)",
    "config.py:retire_leftovers": "startup only: removes retired settings",
    "auto.py:remember": "the Director's own loop, never a Console action",
}  # fmt: skip

# Every function that saves a change on its way from an action: the cases that run it (ids in CASES), or, as a plain
# string, why it is not an action path. The cases are the same few per module because a module's actions share one save.
BACKGROUND = "background work, not an action: it saves through the same writer as the module's actions (see its cases)"
SAVE_SITES: dict[str, list[str] | str] = {
    "console/presets.py:PresetsController._apply": ["presets.platform", "presets.tier", "presets.profile.apply"],
    "console/call.py:CallController._act_set": ["call.set"],
    "console/call.py:CallController._save": ["call.set"],
    "console/call.py:CallController._stage": ["call.set"],
    "console/deck.py:DeckController._stage": ["deck.page_add", "deck.settings", "deck.reset"],
    "console/deck.py:DeckController.act": ["deck.page_add", "deck.settings", "deck.reset"],
    **{
        f"console/deck.py:DeckController.{name}": ["deck.page_add", "deck.settings", "deck.reset"]
        for name in (
            "_commit", "_import", "_key_clear", "_key_move", "_key_set", "_layout", "_page_add", "_page_delete",
            "_page_move", "_page_rename", "_reset", "_set_settings",
        )
    },
    "console/diagnostics.py:Diagnostics._ensure_token": ["telemetry.rotate_token", "telemetry.scrape_config"],
    "console/diagnostics.py:Diagnostics._set_enabled": [
        "telemetry.set", "telemetry.set.off_while_the_enable_is_saving",
    ],
    "console/diagnostics.py:Diagnostics.act": ["telemetry.rotate_token"],
    "console/diagnostics.py:Diagnostics.scrape_config": ["telemetry.scrape_config"],
    "console/director.py:DirectorPanel._change_prefs": ["auto.persona", "auto.location"],
    "console/director.py:DirectorPanel._store_persona": ["auto.persona"],
    "console/director.py:DirectorPanel._store_property": ["auto.location"],
    "console/director.py:DirectorPanel._clear_property": ["auto.location"],
    "console/director.py:DirectorPanel._queue_persona": ["auto.persona_save"],
    "console/director.py:DirectorPanel._save_persona": ["auto.persona_save"],
    "console/director.py:DirectorPanel._remove_persona": "a persona file is removed with an unlink: no fsync to stall",
    "console/echocheck.py:EchoController._finish": ["echo.check"],
    "console/echocheck.py:EchoController._save_results": ["echo.check"],
    "console/eyeline.py:EyeLineController._save": ["eyeline.enable", "eyeline.reminder", "eyeline.calibration.clear"],
    "console/eyeline.py:EyeLineController.enable": ["eyeline.enable"],
    "console/eyeline.py:EyeLineController.set_reminder": ["eyeline.reminder"],
    "console/eyeline.py:EyeLineController.clear_calibration": ["eyeline.calibration.clear"],
    "console/eyeline.py:EyeLineController._fail": BACKGROUND,
    "console/eyeline.py:EyeLineController._observe": BACKGROUND,
    "console/framing.py:FramingController._save_mode": ["framing.mode", "framing.shot", "framing.handoff"],
    "console/framing.py:FramingController.set_mode": ["framing.mode"],
    "console/framing.py:FramingController.set_shot": ["framing.shot"],
    "console/framing.py:FramingController.set_subjects": ["framing.subjects"],
    "console/framing.py:FramingController.set_handoff": ["framing.handoff"],
    "console/framing.py:FramingController._fail": BACKGROUND,
    "console/hotkeys.py:HotkeysController._commit": [
        "hotkeys.add", "hotkeys.update", "hotkeys.remove", "hotkeys.clear", "hotkeys.import", "hotkeys.enable",
    ],
    "console/hotkeys.py:HotkeysController._add": ["hotkeys.add"],
    "console/hotkeys.py:HotkeysController._update": ["hotkeys.update"],
    "console/hotkeys.py:HotkeysController._remove": ["hotkeys.remove"],
    "console/hotkeys.py:HotkeysController._clear": ["hotkeys.clear"],
    "console/hotkeys.py:HotkeysController._import": ["hotkeys.import"],
    "console/hotkeys.py:HotkeysController._enable": ["hotkeys.enable"],
    "console/hotkeys.py:DemoHotkeys._extra": "the demo's scripted key presses and reset: its store is in memory",
    "console/lightauto.py:AutomationPanel._act": ["lighting.auto.follow"],
    "console/lightauto.py:attach_demo": "demo wiring at startup",
    "console/lighting.py:PresetLibrary.save": ["lighting.presets.save"],
    "console/lighting.py:PresetLibrary.delete": ["lighting.presets.delete"],
    "console/lighting.py:LightingController._save_preset": ["lighting.presets.save"],
    "console/lighting.py:LightingController.act": ["lighting.presets.save", "lighting.presets.delete"],
    "console/lighting.py:DemoLighting._demo_reset.restore": "the demo's reset (e2e only): an ephemeral library",
    "console/meetings.py:MeetingsController._save": ["meetings.enable", "meetings.settings"],
    "console/meetings.py:MeetingsController._enable": ["meetings.enable"],
    "console/meetings.py:MeetingsController._set_settings": ["meetings.settings"],
    "console/meetings.py:MeetingsController._ics_add": ["meetings.ics.add"],
    "console/meetings.py:MeetingsController._ics_remove": ["meetings.ics.remove"],
    "console/meetings.py:MeetingsController._connect": "the Microsoft sign-in: only a status, saved through _save",
    "console/meetings.py:MeetingsController._mark_prepped": BACKGROUND,
    "console/meetings.py:DemoMeetings._extra": "the demo's scripted feed and reset (e2e only)",
    "console/native.py:NativeVideo._blur": "native.py: its own state file, os.replace, no fsync, after the change",
    "console/native.py:NativeVideo._keepalive": "same: the engine's own state file, set first",
    "console/native.py:NativeVideo._look_action": "same: the engine's own state file, set first",
    "console/native.py:NativeVideo._source_action": "same: the engine's own state file, set first",
    "console/native.py:NativeVideo._start_ramp.work": "same: the engine's own state file, set first",
    "console/native.py:NativeVideo.stop": "same: the engine's own state file, set first",
    "console/network.py:NetworkController._act_set": ["network.set"],
    "console/network.py:NetworkController._save": ["network.set"],
    "console/outputs.py:Outputs._act_result": "own fix: !166 (#163)",
    "console/outputs.py:Outputs._record": "own fix: !166 (#163)",
    "console/safejoin.py:SafeJoinController._act_set": ["safejoin.set"],
    "console/safejoin.py:SafeJoinController._save": ["safejoin.set"],
    "console/slots.py:SlotsController._act": ["slots.mode", "slots.fade", "slots.starter", "slots.apply"],
    "console/slots.py:SlotsController._seed_parts": ["slots.apply"],
    "console/slots.py:SlotsController._save_current": ["slots.profile"],
    "console/slots.py:DemoSlots._demo": "the demo's clock and reset (e2e only)",
    "console/video.py:LookLibrary.save": ["video.looks.save"],
    "console/video.py:LookLibrary.delete": ["video.looks.delete"],
    "console/video.py:DemoVideo.act": ["video.looks.save", "video.looks.delete"],
    "lightplan.py:Store.save": ["lighting.auto.follow"],
    "slots.py:Store._edit": ["slots.mode", "slots.fade", "slots.starter"],
    **{
        f"slots.py:Store.{name}": ["slots.mode", "slots.fade", "slots.starter"]
        for name in ("clear_profile", "fill_starter", "import_document", "reset", "set_fade", "set_mode", "set_profile")
    },
}  # fmt: skip

# Writers (LatestWriter(...) constructions) per source file, and the cases that make each reach the disk.
WRITERS: dict[str, list[str]] = {
    "console/call.py": ["call.set"],
    "console/deck.py": ["deck.page_add"],
    "console/diagnostics.py": ["telemetry.set"],
    "console/director.py": ["auto.persona"],
    "console/echocheck.py": ["echo.check"],
    "console/eyeline.py": ["eyeline.enable"],
    "console/framing.py": ["framing.mode"],
    "console/hotkeys.py": ["hotkeys.add"],
    "console/lighting.py": ["lighting.presets.save"],
    "console/meetings.py": ["meetings.enable"],
    "console/network.py": ["network.set"],
    "console/safejoin.py": ["safejoin.set"],
    "console/video.py": ["video.looks.save"],
    "lightplan.py": ["lighting.auto.follow"],
    "slots.py": ["slots.mode"],
}  # fmt: skip


def _calls_by_function(path: Path) -> list[tuple[str, str]]:
    """(qualified function name, called name) for every call in the file."""
    found: list[tuple[str, str]] = []

    def visit(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, [*stack, child.name])
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
                if name:
                    found.append((".".join(stack) or "<module>", name))
            visit(child, stack)

    visit(ast.parse(path.read_text()), [])
    return found


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for pattern in SCANNED:
        files.extend(sorted(SRC.glob(pattern)))
    return files


def _sites(names_for: Callable[[str], set[str]]) -> set[str]:
    out: set[str] = set()
    for path in _scanned_files():
        rel = path.relative_to(SRC).as_posix()
        wanted = names_for(rel)
        out.update(f"{rel}:{func}" for func, name in _calls_by_function(path) if name in wanted)
    return out


def test_every_direct_write_is_a_writer_or_has_a_reason() -> None:
    """A new function that writes a file straight from the Console fails here until it is routed through a writer
    (and listed under SAVE_SITES with its case) or listed with the reason it is not an action path."""
    found = _sites(lambda rel: WRITE_CALLS)
    assert not found - set(WRITE_SITES), (
        "these functions write a file directly and are neither a LatestWriter's save nor exempt: "
        f"{sorted(found - set(WRITE_SITES))}. Save through streamstack.saving.LatestWriter and add a case."
    )
    assert not set(WRITE_SITES) - found, f"stale WRITE_SITES entries: {sorted(set(WRITE_SITES) - found)}"


def test_every_saving_function_has_a_case() -> None:
    found = _sites(lambda rel: SAVE_CALLS | STORE_CALLS.get(rel, set()))
    # the modules' own writer plumbing is listed too, so a new action calling it is seen at once
    unknown = found - set(SAVE_SITES)
    assert not unknown, (
        f"these functions save a change and have no case in {Path(__file__).name}: {sorted(unknown)}. "
        "Add a case that stalls the write and asserts state() shows the change, then list it in SAVE_SITES."
    )
    assert not set(SAVE_SITES) - found, f"stale SAVE_SITES entries: {sorted(set(SAVE_SITES) - found)}"
    missing = {c for ids in SAVE_SITES.values() if isinstance(ids, list) for c in ids} - set(CASES)
    assert not missing, f"SAVE_SITES names cases that do not exist: {sorted(missing)}"


def test_every_writer_is_listed_and_has_a_case_that_reaches_it() -> None:
    made = {
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if path.name != "saving.py" and any(name == "LatestWriter" for _, name in _calls_by_function(path))
    }
    assert made == set(WRITERS), f"LatestWriter used in {sorted(made)}, but the gate lists {sorted(WRITERS)}"
    missing = {c for ids in WRITERS.values() for c in ids} - set(CASES)
    assert not missing, f"WRITERS names cases that do not exist: {sorted(missing)}"


def test_every_writer_is_declared_reached_by_its_cases() -> None:
    for site, ids in WRITERS.items():
        assert all(site in CASE_SITES[case_id] for case_id in ids), f"{ids} must declare the writer in {site}"


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_the_change_is_visible_while_the_write_is_stuck(
    case_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disk: Disk
) -> None:
    run_case(case_id, tmp_path, monkeypatch, disk)
