"""curiosity-display-guard: the TV-wake path, rate limiting and the never-4K rules."""

import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
GUARD = ROLE / "files/minimal-desktop/curiosity-display-guard"
HELPER = ROLE / "files/minimal-desktop/curiosity-display-setup"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
TASKS = yaml.safe_load((ROLE / "tasks/minimal-desktop-display-setup.yml").read_text())


os.environ["CURIOSITY_LIB"] = str(ROLE / "files")


def _load() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("curiosity_display_guard", str(GUARD))
    spec = importlib.util.spec_from_loader("curiosity_display_guard", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["curiosity_display_guard"] = module
    loader.exec_module(module)
    return module


guard = _load()


class FakeX:
    """A tiny stand-in for X + the helper: records every command the guard runs."""

    def __init__(self, active: tuple[str, str] | None, listed_8k60: bool = True, recovers: bool = True) -> None:
        self.active = active  # (mode, rate) or None
        self.listed_8k60 = listed_8k60
        self.recovers = recovers
        self.calls: list[list[str]] = []

    def render(self) -> str:
        rates = "60.00 " if self.listed_8k60 else ""
        if self.active:
            mode, rate = self.active
            head = f"DP-4 connected primary {mode}+0+0 (normal left inverted right x axis y axis) 1872mm x 1053mm\n"
        else:
            head = "DP-4 connected (normal left inverted right x axis y axis)\n"
        lines = [head]
        marker = {"60": "60.00*", "30": "30.00*"}
        if self.active and self.active[0] == "7680x4320":
            lines.append(f"   7680x4320     {marker.get(self.active[1], '60.00')}   30.00\n" if self.listed_8k60
                         else "   7680x4320     30.00*\n")
        else:
            lines.append(f"   7680x4320     {rates}30.00\n")
        is_4k = bool(self.active and self.active[0] == "3840x2160")
        lines.append("   3840x2160     60.00*+\n" if is_4k else "   3840x2160     60.00 +\n")
        lines.append("DP-3 connected (normal left inverted right x axis y axis)\n   7680x4320     30.00\n")
        return "Screen 0: minimum 320 x 200, current 7680 x 4320, maximum 16384 x 16384\n" + "".join(lines)

    def run(self, cmd, cfg, timeout=60):
        self.calls.append(list(cmd))
        if cmd[0] == "xrandr" and cmd[1:] in ([], ["--query"]):
            return 0, self.render()
        if cmd[:2] == ["xrandr", "--output"] and "--off" in cmd:
            self.active = None
            return 0, ""
        if cmd[0] == guard.HELPER:
            if self.listed_8k60 and self.recovers:
                self.active = ("7680x4320", "60")
            return 0, ""
        if cmd[:2] == ["xrandr", "--output"] and "--mode" in cmd:
            if cmd[cmd.index("--mode") + 1] == "7680x4320" and self.listed_8k60 and self.recovers:
                self.active = ("7680x4320", "60")
            return 0, ""
        if cmd[:2] == ["xset", "q"]:
            return 0, "DPMS is Disabled"
        return 0, ""

    def offs(self) -> int:
        return len([c for c in self.calls if "--off" in c])

    def applied_modes(self) -> set[str]:
        modes = set()
        for call in self.calls:
            if "--mode" in call:
                modes.add(call[call.index("--mode") + 1])
        return modes


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CURIOSITY_DISPLAY_GUARD_NOSLEEP", "1")
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(guard, "SYSFS_DRM", str(tmp_path / "drm"))
    monkeypatch.setattr(guard, "DEBUGFS_DRI", str(tmp_path / "dri"))
    monkeypatch.setattr(guard, "DEV_DIR", str(tmp_path / "dev"))
    (tmp_path / "dev").mkdir()
    return tmp_path


def _cfg() -> "guard.Config":
    return guard.Config(output="DP-4", mode="7680x4320", rate=60.0, tv_host="")


def _kernel_connector(root: Path, link: bytes | None = bytes([0x77, 0x77, 0x01])) -> Path:
    conn = root / "drm/card2-DP-4"
    conn.mkdir(parents=True)
    (conn / "status").write_text("connected\n")
    (conn / "enabled").write_text("enabled\n")
    (conn / "dpms").write_text("On\n")
    (conn / "drm_dp_aux3").mkdir()
    if link is not None:
        aux = root / "dev/drm_dp_aux3"
        aux.write_bytes(b"\0" * 0x202 + link)
    return conn


def _retrain_node(root: Path) -> Path:
    node = root / "dri/0000:03:00.0/DP-4/i915_dp_force_link_retrain"
    node.parent.mkdir(parents=True)
    node.write_text("0")
    return node


# --- health ---------------------------------------------------------------------


def test_healthy_8k60_is_ok(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _kernel_connector(env)
    x = FakeX(("7680x4320", "60"))
    monkeypatch.setattr(guard, "run", x.run)
    result = guard.assess(_cfg())
    assert result.ok, result.summary()
    assert result.facts["link"] == "locked"


def test_connected_but_mode_lost_is_broken(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _kernel_connector(env)
    monkeypatch.setattr(guard, "run", FakeX(None).run)
    assert guard.assess(_cfg()).problems == ["no-mode"]


def test_x_up_but_link_dead_is_broken(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # X says 8K60 is active; DisplayPort lane status says the link is gone.
    _kernel_connector(env, link=bytes([0x00, 0x00, 0x00]))
    monkeypatch.setattr(guard, "run", FakeX(("7680x4320", "60")).run)
    result = guard.assess(_cfg())
    assert "link-lost" in result.problems and not result.ok


def test_wrong_mode_and_other_output_are_broken(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "run", FakeX(("7680x4320", "30")).run)
    assert "wrong-rate" in guard.assess(_cfg()).problems
    monkeypatch.setattr(guard, "run", FakeX(("3840x2160", "60")).run)
    assert "wrong-mode" in guard.assess(_cfg()).problems


# --- the TV-wake repair path ----------------------------------------------------


def test_tv_wake_forces_link_retrain_and_reapplies_8k60_never_4k(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # After a TV wake everything LOOKS fine (the DP side stayed trained), and the
    # forced repair must still retrain the link and re-apply 7680x4320@60.
    _kernel_connector(env)
    node = _retrain_node(env)
    x = FakeX(("7680x4320", "60"))
    monkeypatch.setattr(guard, "run", x.run)
    assert guard.repair(_cfg(), "tv-wake") == 0
    assert node.read_text() == "1"
    assert x.offs() == 1  # a real modeset: output off, then the helper re-applies
    assert [guard.HELPER] in x.calls
    assert x.applied_modes() <= {"7680x4320"}
    assert x.active == ("7680x4320", "60")


def test_hotplug_with_lost_mode_reapplies_8k60(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _kernel_connector(env)
    x = FakeX(None)
    monkeypatch.setattr(guard, "run", x.run)
    assert guard.repair(_cfg(), "hotplug") == 0
    assert x.active == ("7680x4320", "60")
    assert not any("3840x2160" in " ".join(c) for c in x.calls)


def test_a_healthy_hotplug_does_nothing(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Our own modesets raise hotplug uevents; they must not start another repair.
    _kernel_connector(env)
    x = FakeX(("7680x4320", "60"))
    monkeypatch.setattr(guard, "run", x.run)
    assert guard.repair(_cfg(), "hotplug") == 0
    assert x.offs() == 0 and [guard.HELPER] not in x.calls


def test_hotplug_right_after_our_modeset_is_ignored(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    x = FakeX(None)
    monkeypatch.setattr(guard, "run", x.run)
    guard.STATE_DIR.mkdir(parents=True)
    guard.save_state({"last_modeset": time.time()})
    assert guard.repair(_cfg(), "hotplug") == 0
    assert x.calls == []


def test_mode_missing_never_turns_the_output_off_or_falls_back_to_4k(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The TV just woke and lists no 8K60 mode: do not blank it and never apply 4K.
    x = FakeX(("7680x4320", "30"), listed_8k60=False)
    monkeypatch.setattr(guard, "run", x.run)
    assert guard.repair(_cfg(), "tv-wake") == 1
    assert x.offs() == 0
    assert x.applied_modes() == set()
    assert not any("3840x2160" in " ".join(c) for c in x.calls)


# --- rate limiting: no modeset loops --------------------------------------------


def test_an_unrecoverable_output_is_modeset_a_bounded_number_of_times(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _kernel_connector(env)
    x = FakeX(None, recovers=False)
    monkeypatch.setattr(guard, "run", x.run)
    results = [guard.repair(_cfg(), "watchdog") for _ in range(40)]
    assert results[0] == 1
    assert set(results[1:]) <= {0, 2}  # coalesced into the min gap, or cooling down; never retried
    assert x.offs() <= guard.MAX_MODESETS_IN_WINDOW


def test_the_modeset_window_limit_holds_back_a_repair(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    x = FakeX(None)
    monkeypatch.setattr(guard, "run", x.run)
    guard.STATE_DIR.mkdir(parents=True)
    now = time.time()
    guard.save_state({"modesets": [now - 60] * guard.MAX_MODESETS_IN_WINDOW})
    assert guard.repair(_cfg(), "tv-wake") == 2
    assert x.offs() == 0


def test_the_last_nights_8k30_user_mode_loop_cannot_recur(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 2026-10-27 21:12-21:49: probes every 12.5 s. The guard never polls with a
    # probing call: its watchdog assessment uses `xrandr --query` only, and the
    # one probing step (`xrandr` with no arguments) is part of the bounded ladder.
    x = FakeX(("7680x4320", "60"))
    monkeypatch.setattr(guard, "run", x.run)
    for _ in range(50):
        guard.assess(_cfg())
    assert all(c == ["xrandr", "--query"] or c[:1] == ["xset"] for c in x.calls)


def test_concurrent_triggers_coalesce(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl

    x = FakeX(None)
    monkeypatch.setattr(guard, "run", x.run)
    guard.STATE_DIR.mkdir(parents=True)
    with open(guard.STATE_DIR / "lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert guard.repair(_cfg(), "hotplug") == 0
    assert x.calls == []


# --- a forced-off connector must never be left behind ---------------------------


def test_hotplug_restores_detect_even_if_the_modeset_stage_is_interrupted(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _kernel_connector(env)
    monkeypatch.setattr(guard, "run", FakeX(None).run)

    def killed(_seconds: float) -> None:
        raise SystemExit(143)  # SIGTERM while sleeping with the connector forced off

    monkeypatch.setattr(guard, "sleep", killed)
    with pytest.raises(SystemExit):
        guard.stage_hotplug(_cfg())
    assert (conn / "status").read_text() == "detect"
    assert not (guard.STATE_DIR / "forced-off").exists()


def test_an_oserror_on_the_detect_write_is_retried_and_logged_loudly(env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    conn = _kernel_connector(env)
    monkeypatch.setattr(guard, "run", FakeX(None).run)
    real_write = Path.write_text
    failures = {"detect": 2}

    def flaky(self: Path, data: str, *args: object, **kwargs: object) -> int:
        if self.name == "status" and data == "detect" and failures["detect"] > 0:
            failures["detect"] -= 1
            raise OSError(5, "I/O error")
        return real_write(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky)
    guard.stage_hotplug(_cfg())
    assert (conn / "status").read_text() == "detect"
    assert "CRITICAL: cannot restore connector status to detect" in capsys.readouterr().out


def test_a_permanently_failing_detect_write_is_logged_and_the_marker_kept(env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _kernel_connector(env)
    monkeypatch.setattr(guard, "run", FakeX(None).run)
    real_write = Path.write_text

    def broken(self: Path, data: str, *args: object, **kwargs: object) -> int:
        if self.name == "status" and data == "detect":
            raise OSError(5, "I/O error")
        return real_write(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", broken)
    guard.stage_hotplug(_cfg())
    assert "CRITICAL: connector may stay forced off" in capsys.readouterr().out
    assert (guard.STATE_DIR / "forced-off").exists()  # the next tick heals it


def test_guard_start_restores_a_connector_left_forced_off(env: Path) -> None:
    conn = _kernel_connector(env)
    (conn / "status").write_text("off")  # a previous run was killed mid-hotplug
    assert guard.heal_forced_off(_cfg(), force_check=True) is True
    assert (conn / "status").read_text() == "detect"


def test_watchdog_tick_heals_when_our_marker_says_it_was_forced_off(env: Path) -> None:
    conn = _kernel_connector(env)
    (conn / "status").write_text("off")
    guard.STATE_DIR.mkdir(parents=True)
    (guard.STATE_DIR / "forced-off").write_text("1")
    assert guard.heal_forced_off(_cfg()) is True
    assert (conn / "status").read_text() == "detect"
    assert not (guard.STATE_DIR / "forced-off").exists()
    # A healthy, connected output with no marker is left alone.
    (conn / "status").write_text("connected")
    assert guard.heal_forced_off(_cfg(), force_check=True) is False
    assert (conn / "status").read_text() == "connected"


def test_hotplug_echo_of_our_own_probe_does_not_retrigger_a_repair(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 2026-10-28 22:14: every reprobe raised a drm uevent, which started the next
    # hotplug repair about every 12 s. Inside the quiet window they are deferred.
    _kernel_connector(env)
    x = FakeX(None)
    monkeypatch.setattr(guard, "run", x.run)
    assert guard.repair(_cfg(), "hotplug") == 0
    assert x.active == ("7680x4320", "60")
    x2 = FakeX(("7680x4320", "60"))
    monkeypatch.setattr(guard, "run", x2.run)
    for _ in range(10):
        assert guard.repair(_cfg(), "hotplug") == 0
    assert x2.calls == []
    assert guard.load_state().get("deferred_at")


def _open_quiet_window() -> None:
    now = time.time()
    guard.STATE_DIR.mkdir(parents=True, exist_ok=True)
    guard.save_state({"last_repair_end": now - 5, "last_modeset": now - 30})


def _window_closes() -> None:
    state = guard.load_state()
    state["last_repair_end"] = time.time() - guard.HOTPLUG_QUIET - 1
    guard.save_state(state)


def _run_one_watch_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    class Stop(Exception):
        pass

    def stop(seconds: float) -> None:
        if seconds == 0.123:  # the watch loop's own end-of-tick sleep
            raise Stop

    monkeypatch.setattr(guard, "sleep", stop)
    monkeypatch.setattr(guard, "idle_ms", lambda: None)
    with pytest.raises(Stop):
        guard.watch(_cfg(), interval=0.123)


def test_a_hotplug_in_the_window_with_the_mode_wrong_at_the_end_repairs_exactly_once(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _kernel_connector(env)
    _open_quiet_window()
    x = FakeX(None)  # the TV changed mode / was replugged inside the window
    monkeypatch.setattr(guard, "run", x.run)
    assert guard.repair(_cfg(), "hotplug") == 0
    assert x.calls == []  # deferred, nothing touched yet
    _window_closes()
    _run_one_watch_tick(monkeypatch)
    assert x.active == ("7680x4320", "60")
    assert [guard.HELPER] in x.calls and x.calls.count([guard.HELPER]) == 1
    assert not any("3840x2160" in " ".join(c) for c in x.calls)
    # the deferred marker is spent: a second tick does not repair again
    before = len(x.calls)
    _run_one_watch_tick(monkeypatch)
    assert x.calls.count([guard.HELPER]) == 1
    assert not guard.load_state().get("deferred_at")
    assert len(x.calls) >= before


def test_a_hotplug_in_the_window_with_the_mode_correct_repairs_nothing_and_keeps_the_window_closed(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _kernel_connector(env)
    _open_quiet_window()
    x = FakeX(("7680x4320", "60"))
    monkeypatch.setattr(guard, "run", x.run)
    assert guard.repair(_cfg(), "hotplug") == 0
    _window_closes()
    closed_at = guard.load_state()["last_repair_end"]
    _run_one_watch_tick(monkeypatch)
    assert x.offs() == 0 and [guard.HELPER] not in x.calls
    assert guard.load_state()["last_repair_end"] == closed_at  # no new suppression window
    assert not guard.load_state().get("deferred_at")


# --- wake detection -------------------------------------------------------------


def test_tv_power_edge_off_to_on_is_a_wake_and_startup_is_not() -> None:
    d = guard.WakeDetector()
    assert d.feed("on", None, None, None) == []  # already on at start
    assert d.feed("off", None, None, None) == []
    assert d.feed("on", None, None, None) == ["tv-wake"]
    assert d.feed("standby", None, None, None) == []
    assert d.feed("on", None, None, None) == ["tv-wake"]
    assert d.feed("on", None, None, None) == []


def test_sink_edid_returning_and_dpms_unblank_are_wakes() -> None:
    d = guard.WakeDetector()
    assert d.feed(None, True, "On", None) == []
    assert d.feed(None, False, "Off", None) == []
    assert sorted(d.feed(None, True, "On", None)) == ["dpms-on", "sink-wake"]


def test_input_after_long_idle_reapplies_only_if_the_tv_could_have_slept() -> None:
    d = guard.WakeDetector()
    d.feed("on", None, None, 400_000)
    assert d.feed("on", None, None, 500) == []  # TV was on the whole time
    d = guard.WakeDetector()
    d.feed("on", None, None, 400_000)
    d.feed("off", None, None, 410_000)
    events = d.feed("off", None, None, 500)
    assert events == ["idle-wake"]


def test_tv_power_test_seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seam = tmp_path / "tv"
    monkeypatch.setenv("CURIOSITY_DISPLAY_GUARD_TV_FILE", str(seam))
    seam.write_text("standby\n")
    assert guard.tv_power("203.0.113.9") == "standby"
    seam.write_text("on\n")
    assert guard.tv_power("203.0.113.9") == "on"


def test_guard_locates_the_tv_through_the_shared_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str]] = []

    class Reply:
        def __enter__(self) -> "Reply":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"device": {"PowerState": "standby"}}'

    monkeypatch.setattr(guard.tvlocate, "resolve_tv_host", lambda host, mac: seen.append((host, mac)) or "192.168.1.5")
    monkeypatch.setattr(guard.urllib.request, "urlopen", lambda url, timeout: (seen.append((url, "")), Reply())[1])
    monkeypatch.delenv("CURIOSITY_DISPLAY_GUARD_TV_FILE", raising=False)
    assert guard.tv_power("192.168.1.250", "70:09:71:93:5a:20") == "standby"
    assert seen[0] == ("192.168.1.250", "70:09:71:93:5a:20")
    assert seen[1][0] == "http://192.168.1.5:8001/api/v2/"


def test_x_coming_up_triggers_one_startup_check() -> None:
    down = guard.Assessment(True, [], {"x": "unavailable"})
    up = guard.Assessment(False, ["no-mode"], {})
    assert guard.x_came_up(False, down) == (False, False)  # greeter/X not up yet
    assert guard.x_came_up(False, up) == (True, True)      # first answer after boot
    assert guard.x_came_up(True, up) == (True, False)      # steady state: no repeat
    assert guard.x_came_up(True, down) == (False, False)   # X died...
    assert guard.x_came_up(False, up) == (True, True)      # ...and came back: check again


def test_watch_queues_a_startup_repair_when_x_first_answers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, bool]] = []
    clock = [1000.0]
    monkeypatch.setattr(guard.time, "time", lambda: clock[0])
    monkeypatch.setattr(guard, "heal_forced_off", lambda *a, **k: False)
    monkeypatch.setattr(guard, "connector_dir", lambda output: None)
    monkeypatch.setattr(guard, "idle_ms", lambda: None)
    monkeypatch.setattr(guard, "assess", lambda cfg, deep=False: guard.Assessment(True, [], {}))

    def fake_repair(cfg: object, reason: str, force: bool = False) -> int:
        calls.append((reason, force))
        return 0

    def fake_sleep(seconds: float) -> None:
        clock[0] += 3
        if clock[0] > 1020:
            raise SystemExit(0)

    monkeypatch.setattr(guard, "repair", fake_repair)
    monkeypatch.setattr(guard, "sleep", fake_sleep)
    with pytest.raises(SystemExit):
        guard.watch(guard.Config(output="DP-4", mode="7680x4320", rate=60.0))
    assert calls == [("startup", False)]  # once, and not a forced modeset on a healthy boot


def test_startup_repair_runs_the_ladder_when_the_boot_came_up_broken(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path)
    monkeypatch.setenv("CURIOSITY_DISPLAY_GUARD_NOSLEEP", "1")
    stages: list[str] = []
    verdicts = iter([guard.Assessment(False, ["no-mode"], {"target_listed": 1})])
    healthy = guard.Assessment(True, [], {"mode": "7680x4320@60"})
    monkeypatch.setattr(guard, "assess", lambda cfg, deep=False: next(verdicts, healthy))
    monkeypatch.setattr(guard, "LADDER", [("reprobe", lambda c: stages.append("reprobe"), False),
                                          ("reapply", lambda c: stages.append("reapply"), True)])
    assert guard.repair(guard.Config(output="DP-4", mode="7680x4320", rate=60.0), "startup") == 0
    assert stages == ["reprobe"]  # a plain (unforced) start: cheapest stage first, then verified


def test_profile_pins_the_tv_mac() -> None:
    pin = DEFAULTS["hardware_profiles"]["deskmeet-b760"]["display_guard"]
    assert pin["tv_mac"] == "70:09:71:93:5A:20" and pin["tv_host"] != "192.168.1.250"


# --- the shared helper never falls back to 4K -----------------------------------

PROP = "Screen 0: minimum 16 x 16, current 7680 x 4320, maximum 32767 x 32767\n"


def _run_helper(query: str, conf: str | None, outputs: str = "DP-4\nDP-3\n") -> list[str]:
    with tempfile.TemporaryDirectory(prefix="guard-helper-") as temp_dir:
        root = Path(temp_dir)
        (root / "bin").mkdir()
        (root / "query").write_text(query)
        fake = root / "bin/xrandr"
        fake.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--query" ]; then cat "$FAKE_ROOT/query"; exit 0; fi\n'
            'printf "%s\\n" "$*" >>"$FAKE_ROOT/calls"\n'
        )
        fake.chmod(0o755)
        (root / "outputs").write_text(outputs)
        if conf is not None:
            (root / "guard.conf").write_text(conf)
        env = {
            "PATH": f"{root / 'bin'}:/usr/bin:/bin",
            "FAKE_ROOT": str(root),
            "CURIOSITY_DISPLAY_OUTPUTS": str(root / "outputs"),
            "CURIOSITY_DISPLAY_BROADCAST_RGB": str(root / "none"),
            "CURIOSITY_DISPLAY_GUARD_CONF": str(root / "guard.conf"),
            "CURIOSITY_DISPLAY_RETRY_SLEEP": "0",
        }
        result = subprocess.run(["sh", str(HELPER)], text=True, capture_output=True, env=env)
        assert result.returncode == 0, result.stderr
        calls = root / "calls"
        out = [l for l in (calls.read_text().splitlines() if calls.exists() else []) if l != ""]
        return out + [f"stderr: {result.stderr.strip()}"]


PIN = "output=DP-4\nmode=7680x4320\nrate=60\n"


def test_helper_applies_the_pinned_8k60_mode() -> None:
    query = PROP + (
        "DP-4 connected (normal left inverted right x axis y axis)\n"
        "   3840x2160     60.00 +\n   7680x4320     30.00    60.00\n"
        "DP-3 connected (normal left inverted right x axis y axis)\n   7680x4320     30.00\n"
    )
    calls = _run_helper(query, PIN)
    assert calls[0] == "--output DP-4 --mode 7680x4320 --rate 60.00 --primary --output DP-3 --off"


def test_helper_leaves_a_working_display_alone_when_the_pinned_mode_is_missing() -> None:
    query = PROP + (
        "DP-4 connected primary 3840x2160+0+0 (normal left inverted right x axis y axis)\n"
        "   3840x2160     60.00*+\n   7680x4320     30.00\n"
    )
    calls = _run_helper(query, PIN)
    assert calls[:-1] == []
    assert "pinned mode 7680x4320@60 is not listed" in calls[-1]


def test_helper_without_a_pin_keeps_its_old_behaviour() -> None:
    query = PROP + "DP-4 connected (normal left inverted right x axis y axis)\n   3840x2160     60.00 +\n"
    assert _run_helper(query, None)[0] == "--output DP-4 --mode 3840x2160 --rate 60.00 --primary"


# --- role wiring ----------------------------------------------------------------


def _task(name: str) -> dict:
    return next(t for t in TASKS if name in t.get("name", ""))


def test_scripts_are_executable_and_valid() -> None:
    assert GUARD.stat().st_mode & stat.S_IXUSR
    subprocess.run(["python3", "-m", "py_compile", str(GUARD)], check=True)
    subprocess.run(["sh", "-n", str(HELPER)], check=True)


def test_deskmeet_profile_pins_dp4_8k60_consistently_with_console_video() -> None:
    profile = DEFAULTS["hardware_profiles"]["deskmeet-b760"]
    pin = profile["display_guard"]
    assert profile["console_video"] == f"{pin['output']}:{pin['mode']}@{pin['rate']}"
    assert profile["display_outputs"][0] == pin["output"]
    assert pin["lane_count"] == 4 and pin["tv_host"]
    for name, other in DEFAULTS["hardware_profiles"].items():
        if name != "deskmeet-b760":
            assert "display_guard" not in other


def test_guard_files_units_and_triggers_are_installed() -> None:
    conf = _task("Pin the display output and mode")["ansible.builtin.copy"]
    assert "output={{ minimal_desktop_display_guard.output }}" in conf["content"]
    udev = _task("udev trigger")["ansible.builtin.copy"]["content"]
    assert 'ENV{HOTPLUG}=="1"' in udev and "curiosity-display-guard@hotplug.service" in udev
    sleep_hook = _task("resume hook")["ansible.builtin.copy"]
    assert sleep_hook["dest"] == "/usr/lib/systemd/system-sleep/curiosity-display-guard"
    assert "curiosity-display-guard@resume.service" in sleep_hook["content"]
    assert sleep_hook["mode"] == "0755" and "exit 0" in sleep_hook["content"]
    units = {i["name"]: i["content"] for i in _task("guard units")["ansible.builtin.copy"] and _task("guard units")["loop"]}
    assert "watch" in units["curiosity-display-guard.service"]
    assert "Restart=always" in units["curiosity-display-guard.service"]
    assert "repair --reason %i" in units["curiosity-display-guard@.service"]
    for task in TASKS:
        if "guard" in task.get("name", "").lower() and "Remove" not in task["name"] and "Validate" not in task["name"]:
            assert "minimal_desktop_display_guard_enabled" in str(task["when"])


def test_console_tuning_udev_cannot_amplify_a_probe_loop() -> None:
    text = (ROLE / "tasks/console-tuning.yml").read_text()
    assert "StartLimitBurst=3" in text and "StartLimitIntervalSec=60" in text
