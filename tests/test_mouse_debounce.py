"""Mouse button debounce: state machine, derived defaults and role wiring (catalog#143).

The machine is exercised with fake event timelines (seconds on one clock), so no
device, uinput or python-evdev is needed.
"""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
SCRIPT = ROLE / "files/curiosity-mouse-debounce"
UNIT = ROLE / "templates/curiosity-mouse-debounce@.service.j2"
RULES = ROLE / "templates/90-curiosity-mouse-debounce.rules.j2"
TASKS = ROLE / "tasks/mouse-debounce.yml"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
PROFILES = DEFAULTS["hardware_profiles"]


def _load_module():
    loader = importlib.machinery.SourceFileLoader("curiosity_mouse_debounce", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module  # dataclasses resolve annotations through sys.modules
    loader.exec_module(module)
    return module


md = _load_module()

LEFT = md.BUTTON_NAMES["BTN_LEFT"]
RIGHT = md.BUTTON_NAMES["BTN_RIGHT"]
REL_X, REL_Y, REL_WHEEL, REL_WHEEL_HI_RES = 0, 1, 8, 0x0B
EV_REL = 2
MS = 0.001


def ev(ts: float, type_: int, code: int, value: int) -> "md.Event":
    return md.Event(ts, type_, code, value)


def packet(ts: float, *events: tuple[int, int, int]) -> list["md.Event"]:
    """A device packet: the events at time ts, closed by SYN_REPORT."""
    return [*(ev(ts, t, c, v) for t, c, v in events), ev(ts, md.EV_SYN, md.SYN_REPORT, 0)]


def press(ts: float, code: int = LEFT) -> list["md.Event"]:
    return packet(ts, (md.EV_KEY, code, 1))


def release(ts: float, code: int = LEFT) -> list["md.Event"]:
    return packet(ts, (md.EV_KEY, code, 0))


def key_events(events: list["md.Event"], code: int = LEFT) -> list[tuple[int, float]]:
    """(value, ts) of every key event for `code` in the output."""
    return [(e.value, e.ts) for e in events if e.type == md.EV_KEY and e.code == code]


class Timeline:
    """Feed device packets in time order, advancing the timer between them.

    Mirrors the service loop: before each packet the clock passes, expire() runs.
    """

    def __init__(self, window_ms: int = 60, buttons: frozenset[int] | None = None) -> None:
        self.machine = md.Debouncer(window=window_ms * MS, buttons=buttons or frozenset({LEFT}))
        self.out: list["md.Event"] = []

    def send(self, *packets: list["md.Event"]) -> "Timeline":
        for pkt in packets:
            when = pkt[-1].ts
            # The loop wakes at the deadline whenever it falls before the next event.
            while (due := self.machine.deadline()) is not None and due < when:
                self.out.extend(self.machine.expire(due))
            for event in pkt:
                self.out.extend(self.machine.feed(event))
        return self

    def settle(self, until: float) -> "Timeline":
        while (due := self.machine.deadline()) is not None and due <= until:
            self.out.extend(self.machine.expire(due))
        return self


def test_a_clean_click_is_passed_with_the_release_delayed_by_the_window() -> None:
    t = Timeline(60).send(press(1.000), release(1.100)).settle(2.0)
    assert key_events(t.out) == [(1, 1.000), (0, pytest.approx(1.160))]
    assert t.machine.bounces == []


def test_a_press_is_never_delayed() -> None:
    machine = md.Debouncer(window=0.06, buttons=frozenset({LEFT}))
    out = []
    for event in press(5.0):
        out.extend(machine.feed(event))
    assert key_events(out) == [(1, 5.0)]
    assert out[-1].type == md.EV_SYN


@pytest.mark.parametrize("gap_ms", [3, 10, 25, 45, 59])
def test_a_bounce_inside_the_window_is_dropped_and_the_button_stays_down(gap_ms: int) -> None:
    # Physical bounce: press, release, press again gap_ms later, release.
    t = Timeline(60).send(press(1.000), release(1.030), press(1.030 + gap_ms * MS), release(1.100)).settle(2.0)
    values = [v for v, _ in key_events(t.out)]
    assert values == [1, 0], "one click, not two"
    assert len(t.machine.bounces) == 1
    assert t.machine.bounces[0].gap == pytest.approx(gap_ms * MS)
    # The surviving release is the second one, a window after it.
    assert key_events(t.out)[1][1] == pytest.approx(1.160)


def test_a_real_double_click_with_a_150_ms_gap_passes() -> None:
    t = Timeline(60).send(press(1.000), release(1.060), press(1.210), release(1.270)).settle(2.0)
    assert [v for v, _ in key_events(t.out)] == [1, 0, 1, 0]
    assert t.machine.bounces == []


def test_a_press_just_after_the_window_is_a_new_click() -> None:
    t = Timeline(60).send(press(1.000), release(1.050), press(1.111), release(1.200)).settle(2.0)
    assert [v for v, _ in key_events(t.out)] == [1, 0, 1, 0]
    # The first release goes out at its deadline, before the second press.
    times = [ts for _, ts in key_events(t.out)]
    assert times == sorted(times)


def test_a_glitch_during_a_drag_keeps_the_button_held() -> None:
    # Hold, a 4 ms contact loss (release then press), the drag continues, real release.
    t = Timeline(60).send(
        press(1.000),
        packet(1.100, (EV_REL, REL_X, 5)),
        release(1.200),
        press(1.204),
        packet(1.300, (EV_REL, REL_X, 7)),
        release(2.000),
    ).settle(3.0)
    assert [v for v, _ in key_events(t.out)] == [1, 0]
    assert key_events(t.out)[1][1] == pytest.approx(2.060)
    assert sum(1 for e in t.out if e.type == EV_REL) == 2


def test_two_bounces_in_a_row_are_both_absorbed() -> None:
    t = Timeline(60).send(
        press(1.000), release(1.040), press(1.045), release(1.050), press(1.052), release(1.200)
    ).settle(2.0)
    assert [v for v, _ in key_events(t.out)] == [1, 0]
    assert len(t.machine.bounces) == 2


def test_motion_and_wheel_pass_through_untouched_and_in_order() -> None:
    t = Timeline(60)
    motion = [
        packet(1.0, (EV_REL, REL_X, 3), (EV_REL, REL_Y, -2)),
        packet(1.001, (EV_REL, REL_WHEEL, 1), (EV_REL, REL_WHEEL_HI_RES, 120)),
        packet(1.002, (md.EV_MSC, md.MSC_SCAN, 7)),
    ]
    t.send(*motion)
    assert t.out == [e for pkt in motion for e in pkt]


def test_other_buttons_are_not_debounced() -> None:
    t = Timeline(60).send(press(1.0, RIGHT), release(1.001, RIGHT), press(1.002, RIGHT))
    assert key_events(t.out, RIGHT) == [(1, 1.0), (0, 1.001), (1, 1.002)]
    assert t.machine.deadline() is None


def test_motion_in_the_same_packet_as_a_held_release_is_kept_without_the_release() -> None:
    t = Timeline(60).send(press(1.0), packet(1.1, (EV_REL, REL_X, 4), (md.EV_KEY, LEFT, 0)))
    assert [(e.type, e.code, e.value) for e in t.out[2:]] == [(EV_REL, REL_X, 4), (md.EV_SYN, md.SYN_REPORT, 0)]
    assert key_events(t.out) == [(1, 1.0)]  # the release is still held


def test_a_packet_holding_only_a_dropped_press_writes_nothing() -> None:
    t = Timeline(60).send(press(1.0), release(1.01))
    before = list(t.out)
    t.send(press(1.02))
    assert t.out == before, "no empty SYN frame for a swallowed press"


def test_the_scan_code_of_a_dropped_press_is_dropped_with_it() -> None:
    scan = (md.EV_MSC, md.MSC_SCAN, 0x90001)
    t = Timeline(60).send(
        packet(1.0, scan, (md.EV_KEY, LEFT, 1)),
        packet(1.01, scan, (md.EV_KEY, LEFT, 0)),
        packet(1.02, scan, (md.EV_KEY, LEFT, 1)),
    )
    scans = [e for e in t.out if e.type == md.EV_MSC]
    assert len(scans) == 1, "only the first press keeps its MSC_SCAN"
    t.settle(2.0)
    # The held release re-emits its own scan code just before the key event.
    tail = [(e.type, e.code) for e in t.out[-3:]]
    assert tail == [(md.EV_MSC, md.MSC_SCAN), (md.EV_KEY, LEFT), (md.EV_SYN, md.SYN_REPORT)]


def test_the_timer_deadline_tracks_the_earliest_held_release() -> None:
    t = Timeline(60, frozenset({LEFT, RIGHT}))
    assert t.machine.deadline() is None
    t.send(press(1.0), press(1.0, RIGHT), release(1.1, RIGHT), release(1.12))
    assert t.machine.deadline() == pytest.approx(1.16)
    t.machine.expire(1.17)
    assert t.machine.deadline() == pytest.approx(1.18)


def test_expire_before_the_deadline_does_nothing() -> None:
    t = Timeline(60).send(press(1.0), release(1.1))
    assert t.machine.expire(1.159) == []
    assert key_events(t.machine.expire(1.161)) == [(0, pytest.approx(1.16))]


def test_flush_releases_what_is_held_when_the_service_stops() -> None:
    t = Timeline(60).send(press(1.0), release(1.1))
    assert key_events(t.machine.flush(1.12)) == [(0, 1.12)]
    assert t.machine.deadline() is None


def test_a_stray_release_for_a_button_never_seen_down_passes() -> None:
    t = Timeline(60).send(release(1.0))
    assert key_events(t.out) == [(0, 1.0)]


def test_autorepeat_is_swallowed_only_while_a_release_is_held() -> None:
    t = Timeline(60).send(press(1.0), packet(1.01, (md.EV_KEY, LEFT, 2)))
    assert [v for v, _ in key_events(t.out)] == [1, 2]
    t.send(release(1.02), packet(1.03, (md.EV_KEY, LEFT, 2)))
    assert [v for v, _ in key_events(t.out)] == [1, 2]


def test_syn_dropped_discards_the_damaged_packet_and_requests_a_resync() -> None:
    machine = md.Debouncer(window=0.06, buttons=frozenset({LEFT}))
    out = []
    out += machine.feed(ev(1.0, md.EV_SYN, md.SYN_DROPPED, 0))
    out += machine.feed(ev(1.0, EV_REL, REL_X, 9))
    out += machine.feed(ev(1.0, md.EV_SYN, md.SYN_REPORT, 0))
    assert out == []
    assert machine.resync_needed


def test_resync_presses_a_key_that_went_down_in_the_lost_events() -> None:
    machine = md.Debouncer(window=0.06, buttons=frozenset({LEFT}))
    out = machine.resync({LEFT, RIGHT}, 2.0)
    assert sorted(e.code for e in out if e.type == md.EV_KEY) == [LEFT, RIGHT]
    assert machine.resync({LEFT, RIGHT}, 2.1) == []


def test_resync_releases_other_keys_at_once_and_debounced_ones_through_the_hold() -> None:
    machine = md.Debouncer(window=0.06, buttons=frozenset({LEFT}))
    machine.resync({LEFT, RIGHT}, 2.0)
    out = machine.resync(set(), 3.0)
    assert key_events(out, RIGHT) == [(0, 3.0)]
    assert key_events(out, LEFT) == []
    assert key_events(machine.expire(3.06), LEFT) == [(0, pytest.approx(3.06))]


def test_the_window_survives_a_late_loop_because_event_time_decides() -> None:
    """A starved loop reads a 200 ms-apart press and release in one batch: still a new click."""
    machine = md.Debouncer(window=0.06, buttons=frozenset({LEFT}))
    out = []
    for event in [*press(1.0), *release(1.05), *press(1.3), *release(1.4)]:
        out.extend(machine.feed(event))
    out.extend(machine.expire(1.5))
    assert [v for v, _ in key_events(out)] == [1, 0, 1, 0]
    assert machine.bounces == []


def test_the_realistic_evidence_distribution_is_filtered() -> None:
    """67 phantom presses with the recorded gaps (a 372-press log) all vanish with the default window."""
    gaps_ms = [3] * 14 + [15] * 8 + [25] * 22 + [35] * 7 + [45] * 3 + [55] * 3  # <= 59 ms
    t = Timeline(60)
    now = 1.0
    for gap in gaps_ms:
        t.send(press(now), release(now + 0.03), press(now + 0.03 + gap * MS), release(now + 0.06 + gap * MS))
        now += 1.0
    t.settle(now + 1)
    values = [v for v, _ in key_events(t.out)]
    assert values == [1, 0] * len(gaps_ms), "each physical click is exactly one click"
    assert len(t.machine.bounces) == len(gaps_ms)


def test_parse_buttons_accepts_names_and_numbers() -> None:
    assert md.parse_buttons("BTN_LEFT") == {LEFT}
    assert md.parse_buttons("btn_left, BTN_RIGHT,274") == {LEFT, RIGHT, 274}
    for bad in ("", " , ", "banana", "BTN_"):
        with pytest.raises(ValueError):
            md.parse_buttons(bad)


def test_parse_buttons_looks_other_names_up_in_evdev(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    fake = types.ModuleType("evdev")
    fake.ecodes = types.SimpleNamespace(BTN_STYLUS=331)
    monkeypatch.setitem(sys.modules, "evdev", fake)
    assert md.parse_buttons("BTN_STYLUS") == {331}
    with pytest.raises(ValueError, match="unknown button"):
        md.parse_buttons("BTN_NOPE")
    monkeypatch.setitem(sys.modules, "evdev", None)  # import fails
    with pytest.raises(ValueError, match="not installed"):
        md.parse_buttons("BTN_STYLUS")


def test_serialise_packs_a_whole_packet_in_one_buffer() -> None:
    events = press(1.0)
    assert len(md.serialise(events)) == len(events) * md.INPUT_EVENT.size == 48


def test_summary_line_reports_count_and_gaps() -> None:
    summary = md.Summary(started=0.0, bounces=[md.Bounce(LEFT, 0.004), md.Bounce(LEFT, 0.030)], presses=12)
    line = summary.line(60.0)
    assert "suppressed 2 bounce(s)" in line
    assert "BTN_LEFT" in line and "12 press(es) passed" in line
    assert "min 4.0 ms" in line and "max 30.0 ms" in line
    assert md.Summary(started=0.0).line(60.0) is None


def test_main_rejects_a_nonsense_window(capsys: pytest.CaptureFixture[str]) -> None:
    assert md.main(["--window-ms", "0", "/dev/input/event0"]) == 2
    assert md.main(["--window-ms", "5000", "/dev/input/event0"]) == 2
    assert md.main(["--buttons", "nonsense", "/dev/input/event0"]) == 2
    assert "must be 1 to 1000" in capsys.readouterr().out


# -- role wiring ---------------------------------------------------------------


def _environment() -> jinja2.Environment:
    from ansible.plugins.filter.core import FilterModule
    from ansible.plugins.test.core import TestModule

    env = jinja2.Environment()
    env.filters.update(FilterModule().filters())
    env.tests.update(TestModule().tests())
    return env


def _derived(name: str, profile: str) -> object:
    rendered = (
        _environment()
        .from_string(DEFAULTS[name])
        .render(hardware_profiles=PROFILES, hardware_profile=profile)
        .strip()
    )
    return yaml.safe_load(rendered)


def test_only_deskmeet_enables_the_debounce_and_others_get_zero() -> None:
    for name in PROFILES:
        expected = PROFILES[name].get("mouse_debounce_ms", 0)
        assert _derived("developer_workstation_mouse_debounce_ms", name) == expected, name
    assert _derived("developer_workstation_mouse_debounce_ms", "deskmeet-b760") == 80
    assert _derived("developer_workstation_mouse_debounce_ms", "generic") == 0
    assert _derived("developer_workstation_mouse_debounce_ms", "missing-profile") == 0


def test_buttons_and_devices_default_to_the_g604_left_button() -> None:
    assert _derived("developer_workstation_mouse_debounce_buttons", "generic") == ["BTN_LEFT"]
    assert _derived("developer_workstation_mouse_debounce_devices", "generic") == [
        {"name": "Logitech G604", "vendor": "046d"}
    ]


def test_a_profile_can_override_buttons_and_devices() -> None:
    profiles = {
        "x": {
            "mouse_debounce_ms": 40,
            "mouse_debounce_buttons": ["BTN_LEFT", "BTN_RIGHT"],
            "mouse_debounce_devices": [{"name": "Some Mouse"}],
        }
    }
    env = _environment()
    for name, expected in (
        ("developer_workstation_mouse_debounce_ms", 40),
        ("developer_workstation_mouse_debounce_buttons", ["BTN_LEFT", "BTN_RIGHT"]),
        ("developer_workstation_mouse_debounce_devices", [{"name": "Some Mouse"}]),
    ):
        rendered = env.from_string(DEFAULTS[name]).render(hardware_profiles=profiles, hardware_profile="x")
        assert yaml.safe_load(rendered) == expected


def _render(template: Path, **variables: object) -> str:
    return _environment().from_string(template.read_text()).render(**variables)


def test_the_udev_rule_starts_one_instance_per_matching_mouse_and_never_the_clone() -> None:
    text = _render(
        RULES,
        developer_workstation_mouse_debounce_devices=[
            {"name": "Logitech G604", "vendor": "046d"},
            {"name": "Some Mouse"},
        ],
    )
    rules = [line for line in text.splitlines() if line.startswith("SUBSYSTEM")]
    assert len(rules) == 2
    assert 'ATTRS{name}=="Logitech G604"' in rules[0] and 'ATTRS{id/vendor}=="046d"' in rules[0]
    assert 'ATTRS{id/vendor}' not in rules[1]
    for rule in rules:
        assert 'ENV{ID_INPUT_MOUSE}=="1"' in rule
        assert 'KERNEL=="event*"' in rule
        assert 'ATTRS{phys}!="curiosity-mouse-debounce"' in rule
        assert 'ENV{SYSTEMD_WANTS}+="curiosity-mouse-debounce@%k.service"' in rule
        assert 'TAG+="systemd"' in rule


def test_the_unit_follows_the_device_and_is_hardened() -> None:
    text = _render(
        UNIT,
        developer_workstation_mouse_debounce_ms=80,
        developer_workstation_mouse_debounce_buttons=["BTN_LEFT"],
    )
    unit = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            unit.setdefault(key, []).append(value)
    assert unit["BindsTo"] == ["dev-input-%i.device"]
    assert unit["ExecStart"] == [
        "/usr/bin/python3 -I /usr/local/sbin/curiosity-mouse-debounce --window-ms 80 --buttons BTN_LEFT /dev/input/%i"
    ]
    assert unit["Restart"] == ["on-failure"]
    assert unit["RestartPreventExitStatus"] == ["2"]
    assert unit["SyslogIdentifier"] == ["curiosity-mouse-debounce"]
    for key, value in {
        "ProtectSystem": "strict",
        "ProtectHome": "yes",
        "PrivateTmp": "yes",
        "NoNewPrivileges": "yes",
        "DevicePolicy": "closed",
        "PrivateNetwork": "yes",
        "MemoryDenyWriteExecute": "yes",
        "CapabilityBoundingSet": "CAP_SYS_NICE",
    }.items():
        assert unit[key] == [value], key
    allowed = unit["DeviceAllow"]
    assert "/dev/uinput rw" in allowed and "char-input rw" in allowed
    assert "WantedBy" not in text, "instances are started by udev, never enabled"


def test_the_tasks_gate_on_the_milliseconds_and_remove_everything_when_off() -> None:
    tasks = yaml.safe_load(TASKS.read_text())
    flat = yaml.safe_dump(tasks)
    assert "developer_workstation_mouse_debounce_ms" in flat
    assert "python3-evdev" in flat
    for artefact in (
        "/etc/udev/rules.d/90-curiosity-mouse-debounce.rules",
        "/etc/systemd/system/curiosity-mouse-debounce@.service",
        "/usr/local/sbin/curiosity-mouse-debounce",
    ):
        assert flat.count(artefact) >= 2, f"{artefact} is installed and removed"
    for task in tasks:
        for key in ("when",):
            conditions = task.get(key)
            if conditions is not None:
                assert "regex_search" not in yaml.safe_dump(conditions)


def test_main_includes_the_debounce_tasks_for_desktop_profiles() -> None:
    main = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    include = next(
        t
        for t in main
        if isinstance(t.get("ansible.builtin.include_tasks"), dict)
        and t["ansible.builtin.include_tasks"].get("file") == "mouse-debounce.yml"
    )
    assert "mouse-debounce" in include["tags"]
    assert "workstation_profile in ['desktop', 'thin-client', 'byod-kiosk']" in include["when"]
