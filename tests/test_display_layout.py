"""Declared display layout, touch mapping and hot-plug watcher (Curiosity-Corp/catalog-internal#24).

dm313red's second monitor stayed dark for a whole boot: it asserted hot-plug
after X started, the helper only ever lit one output, and nothing re-applied
anything when it connected. The layout is now declared per host, a touch panel
is mapped to its screen by device name, and a session watcher re-applies both
on hot-plug.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles" / "developer_workstation"
FILES = ROLE / "files" / "minimal-desktop"
HELPER = FILES / "curiosity-display-setup"
HOTPLUG = FILES / "curiosity-display-hotplug"
TASKS = yaml.safe_load((ROLE / "tasks" / "minimal-desktop-display-setup.yml").read_text())
DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())

# dm313red as X saw it on 2026-11-22: two Sceptre Z27 on the A380's DP-2 and
# DP-3, the Wisecoco on HDMI-2 (once its video cable is in).
DM313RED = """\
Screen 0: minimum 320 x 200, current 3840 x 2160, maximum 16384 x 16384
DP-1 disconnected (normal left inverted right x axis y axis)
HDMI-1 disconnected (normal left inverted right x axis y axis)
DP-2 connected primary 3840x2160+0+0 (normal left inverted right x axis y axis) 600mm x 340mm
   3840x2160     60.00 +  69.98*   59.94    30.00
   2560x1440     59.95
DP-3 connected (normal left inverted right x axis y axis) 600mm x 340mm
   3840x2160     60.00 +  59.94    30.00
   1920x1080     60.00
HDMI-2 connected (normal left inverted right x axis y axis) 344mm x 194mm
   1920x1080     60.00 +  50.00
HDMI-3 disconnected (normal left inverted right x axis y axis)
"""

LAYOUT = """\
# Managed by Ansible.
DP-2 3840x2160 60 0x0 1
DP-3 3840x2160 60 3840x0 0
HDMI-2 auto - 0x2160 0
"""

XINPUT_LIST = (
    "⎡ Virtual core pointer                    \tid=2\t[master pointer  (3)]\n"
    "⎜   ↳ Virtual core XTEST pointer              \tid=4\t[slave  pointer  (2)]\n"
    "⎜   ↳ PIXCIR Inc. PIXC65 Touch                \tid=21\t[slave  pointer  (2)]\n"
    "⎜   ↳ PIXCIR Inc. PIXC65 Touch Mouse          \tid=22\t[slave  pointer  (2)]\n"
    "⎜   ↳ PIXCIR Inc. PIXC65 Touch                \tid=23\t[slave  pointer  (2)]\n"
    "⎣ Virtual core keyboard                   \tid=3\t[master keyboard (2)]\n"
    "    ↳ PIXCIR Inc. PIXC65 Touch                \tid=24\t[slave  keyboard (3)]\n"
)


def _run(query: str, layout: str | None, *, outputs: str | None = None, touch: str | None = None,
         xinput: str = XINPUT_LIST, after: str | None = None) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the helper against fake xrandr/xinput. `after` is what --query
    answers once the helper has issued a modeset (the lit layout)."""
    with tempfile.TemporaryDirectory(prefix="display-layout-") as temp_dir:
        root = Path(temp_dir)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (root / "query").write_text(query)
        (root / "after").write_text(after if after is not None else query)
        (root / "xinput-list").write_text(xinput)
        (bin_dir / "xrandr").write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--query" ]; then\n'
            '  if [ -e "$FAKE_ROOT/calls" ]; then cat "$FAKE_ROOT/after"; else cat "$FAKE_ROOT/query"; fi; exit 0\n'
            "fi\n"
            '[ "$1" = "--prop" ] && exit 0\n'
            'printf "xrandr %s\\n" "$*" >>"$FAKE_ROOT/calls"\n'
        )
        # id 21 and 22 are absolute (the touch screen and a pen); 23 is the
        # same-named relative mouse interface and must never be mapped.
        (bin_dir / "xinput").write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  list) if [ "$2" = "--long" ]; then\n'
            '          case "$3" in 21|22) echo "  Mode: absolute" ;; *) echo "  Mode: relative" ;; esac\n'
            '        else cat "$FAKE_ROOT/xinput-list"; fi ;;\n'
            '  map-to-output) printf "xinput %s\\n" "$*" >>"$FAKE_ROOT/calls" ;;\n'
            "esac\n"
        )
        for fake in bin_dir.iterdir():
            fake.chmod(0o755)
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "FAKE_ROOT": str(root),
            "CURIOSITY_DISPLAY_OUTPUTS": str(root / "display-outputs"),
            "CURIOSITY_DISPLAY_LAYOUT": str(root / "display-layout"),
            "CURIOSITY_TOUCH_OUTPUTS": str(root / "touch-outputs"),
            "CURIOSITY_DISPLAY_BROADCAST_RGB": str(root / "none"),
            "CURIOSITY_DISPLAY_GUARD_CONF": str(root / "none"),
        }
        for name, content in (("display-layout", layout), ("display-outputs", outputs), ("touch-outputs", touch)):
            if content is not None:
                (root / name).write_text(content)
        result = subprocess.run(["sh", str(HELPER)], text=True, capture_output=True, env=env)
        calls_file = root / "calls"
        calls = calls_file.read_text().splitlines() if calls_file.exists() else []
    return result, calls


LIT = (
    "DP-2 connected primary 3840x2160+0+0 (normal) 600mm x 340mm\n   3840x2160     60.00*+\n"
    "DP-3 connected 3840x2160+3840+0 (normal) 600mm x 340mm\n   3840x2160     60.00*+\n"
    "HDMI-2 connected 1920x1080+0+2160 (normal) 344mm x 194mm\n   1920x1080     60.00*+\n"
)


def test_scripts_are_valid_posix_shell_and_executable() -> None:
    for script in (HELPER, HOTPLUG):
        assert subprocess.run(["sh", "-n", str(script)]).returncode == 0
        assert os.access(script, os.X_OK)
        assert "Managed by Ansible" in script.read_text()


def test_layout_lights_every_declared_output_in_one_call() -> None:
    result, calls = _run(DM313RED, LAYOUT)
    assert result.returncode == 0, result.stderr
    assert calls == [
        "xrandr --output DP-2 --mode 3840x2160 --rate 60.00 --pos 0x0 --primary"
        " --output DP-3 --mode 3840x2160 --rate 60.00 --pos 3840x0"
        " --output HDMI-2 --auto --pos 0x2160"
    ]
    assert result.stderr == ""


def test_layout_pins_the_declared_rate_not_the_highest() -> None:
    # The single-output helper drove DP-2 at 69.98 Hz, above the panel's EDID
    # pixel clock; a declared 60 picks the listed 60.00, never 69.98 or 59.94.
    _, calls = _run(DM313RED, "DP-2 3840x2160 60 - 1\n")
    assert calls == ["xrandr --output DP-2 --mode 3840x2160 --rate 60.00 --primary"]


def test_declared_outputs_that_are_not_connected_are_turned_off_and_others_left_alone() -> None:
    query = DM313RED.replace("HDMI-2 connected", "HDMI-2 disconnected")
    _, calls = _run(query, LAYOUT)
    assert calls == [
        "xrandr --output DP-2 --mode 3840x2160 --rate 60.00 --pos 0x0 --primary"
        " --output DP-3 --mode 3840x2160 --rate 60.00 --pos 3840x0"
        " --output HDMI-2 --off"
    ]
    assert "HDMI-1" not in calls[0] and "DP-1" not in calls[0]


def test_a_mode_the_output_does_not_list_falls_back_to_auto_with_a_warning() -> None:
    result, calls = _run(DM313RED, "DP-3 7680x4320 60 - 1\n")
    assert calls == ["xrandr --output DP-3 --auto --primary"]
    assert "DP-3 does not list 7680x4320" in result.stderr


def test_without_any_declared_output_connected_the_single_output_logic_runs() -> None:
    result, calls = _run(DM313RED, "DP-4 7680x4320 60 0x0 1\n", outputs="DP-3\n")
    assert result.returncode == 0, result.stderr
    assert calls == [
        "xrandr --output DP-3 --mode 3840x2160 --rate 60.00 --primary --output DP-2 --off --output HDMI-2 --off"
    ]


def test_without_a_layout_file_nothing_changes() -> None:
    _, with_none = _run(DM313RED, None, outputs="DP-2\n")
    assert with_none == [
        "xrandr --output DP-2 --mode 3840x2160 --rate 69.98 --primary --output DP-3 --off --output HDMI-2 --off"
    ]


def test_invalid_layout_lines_are_reported_and_skipped() -> None:
    result, calls = _run(DM313RED, "DP-2;rm 3840x2160 60 0x0 1\nDP-3 big - - 0\nDP-2 auto - - 1\n")
    assert result.returncode == 0
    assert calls == ["xrandr --output DP-2 --auto --primary"]
    assert result.stderr.count("ignoring invalid layout line") == 2


def test_a_failing_layout_modeset_never_blocks_the_login() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "xrandr").write_text(
            '#!/bin/sh\n[ "$1" = "--query" ] && { cat "$Q"; exit 0; }\necho "BadMatch" >&2\nexit 1\n'
        )
        (root / "xrandr").chmod(0o755)
        (root / "q").write_text(DM313RED)
        (root / "layout").write_text(LAYOUT)
        result = subprocess.run(
            ["sh", str(HELPER)], text=True, capture_output=True,
            env={"PATH": f"{root}:/usr/bin:/bin", "Q": str(root / "q"),
                 "CURIOSITY_DISPLAY_LAYOUT": str(root / "layout"),
                 "CURIOSITY_TOUCH_OUTPUTS": str(root / "none"),
                 "CURIOSITY_DISPLAY_GUARD_CONF": str(root / "none"),
                 "CURIOSITY_DISPLAY_BROADCAST_RGB": str(root / "none")},
        )
    assert result.returncode == 0
    assert "xrandr failed for the declared layout: BadMatch" in result.stderr


def test_touch_panel_is_mapped_by_name_to_its_lit_output_only_when_absolute() -> None:
    result, calls = _run(DM313RED, LAYOUT, touch="HDMI-2 PIXCIR Inc. PIXC65 Touch\n", after=LIT)
    assert result.returncode == 0, result.stderr
    # Exact name: not "... Touch Mouse" (22), not the relative interface (23),
    # not the keyboard (24).
    assert calls[1:] == ["xinput map-to-output 21 HDMI-2"]


def test_touch_panel_waits_for_its_output_to_be_lit() -> None:
    dark = LIT.replace("HDMI-2 connected 1920x1080+0+2160", "HDMI-2 disconnected")
    _, calls = _run(DM313RED, LAYOUT, touch="HDMI-2 PIXCIR Inc. PIXC65 Touch\n", after=dark)
    assert not [c for c in calls if c.startswith("xinput")]


def test_a_missing_touch_device_is_not_an_error() -> None:
    result, calls = _run(DM313RED, LAYOUT, touch="HDMI-2 Some Other Panel\n", after=LIT)
    assert result.returncode == 0 and result.stderr == ""
    assert not [c for c in calls if c.startswith("xinput")]


def _hotplug_env(root: Path, setup_log: Path) -> dict[str, str]:
    drm = root / "drm"
    for connector, status in (("card1-DP-2", "connected"), ("card1-DP-3", "disconnected")):
        (drm / connector).mkdir(parents=True)
        (drm / connector / "status").write_text(status + "\n")
    (root / "input").write_text('I: Bus=0003\nN: Name="Logitech G604"\n\n')
    (root / "touch").write_text("HDMI-2 PIXCIR Inc. PIXC65 Touch\n")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    (bin_dir / "xset").write_text('#!/bin/sh\n[ ! -e "$FAKE_ROOT/x-gone" ]\n')
    (bin_dir / "setup").write_text(f'#!/bin/sh\necho run >>"{setup_log}"\n')
    for fake in bin_dir.iterdir():
        fake.chmod(0o755)
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "FAKE_ROOT": str(root),
        "DISPLAY": ":0",
        "XDG_RUNTIME_DIR": str(root),
        "CURIOSITY_DISPLAY_SETUP": str(bin_dir / "setup"),
        "CURIOSITY_DRM_ROOT": str(drm),
        "CURIOSITY_INPUT_DEVICES": str(root / "input"),
        "CURIOSITY_TOUCH_OUTPUTS": str(root / "touch"),
        "CURIOSITY_DISPLAY_HOTPLUG_INTERVAL": "0.1",
    }


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_hotplug_watcher_reapplies_once_per_settled_change_and_exits_with_x() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        log = root / "setup.log"
        env = _hotplug_env(root, log)
        watcher = subprocess.Popen(["sh", str(HOTPLUG)], env=env, stderr=subprocess.PIPE, text=True)
        try:
            runs = lambda: len(log.read_text().splitlines()) if log.exists() else 0  # noqa: E731
            time.sleep(0.5)
            assert runs() == 0, "no change, no modeset"
            # The second monitor asserts hot-plug late.
            (root / "drm" / "card1-DP-3" / "status").write_text("connected\n")
            assert _wait_for(lambda: runs() == 1)
            time.sleep(0.5)
            assert runs() == 1, "a settled state is applied once, not on every poll"
            # The touch panel's USB-C is plugged in.
            with (root / "input").open("a") as devices:
                devices.write('I: Bus=0003 Vendor=0416 Product=1102\nN: Name="PIXCIR Inc. PIXC65 Touch"\n\n')
            assert _wait_for(lambda: runs() == 2)
            (root / "x-gone").write_text("")
            assert watcher.wait(timeout=5) == 0
            assert not (root / "curiosity-display-hotplug-_0.pid").exists()
        finally:
            if watcher.poll() is None:
                watcher.kill()
                watcher.wait()


def test_a_new_watcher_on_the_same_display_replaces_the_old_one() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        env = _hotplug_env(root, root / "setup.log")
        first = subprocess.Popen(["sh", str(HOTPLUG)], env=env)
        second = None
        try:
            pid_file = root / "curiosity-display-hotplug-_0.pid"
            assert _wait_for(lambda: pid_file.exists() and pid_file.read_text().strip() == str(first.pid))
            second = subprocess.Popen(["sh", str(HOTPLUG)], env=env)
            assert first.wait(timeout=5) is not None
            assert _wait_for(lambda: pid_file.exists() and pid_file.read_text().strip() == str(second.pid))
            assert second.poll() is None
        finally:
            for proc in (first, second):
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait()


def _task(name: str) -> dict:
    return next(t for t in TASKS if t.get("name") == name)


def test_defaults_keep_existing_hosts_unchanged() -> None:
    assert DEFAULTS["minimal_desktop_display_layout"] == []
    assert DEFAULTS["minimal_desktop_touch_outputs"] == []


def test_layout_touch_and_watcher_files_are_written_only_when_declared() -> None:
    layout = _task("Record the declared display layout")
    assert layout["ansible.builtin.copy"]["dest"] == "/etc/curiosity/display-layout"
    assert "minimal_desktop_display_layout | length > 0" in layout["when"]
    assert "minimal_desktop_display_layout | length == 0" in _task("Remove the declared display layout when it is empty")["when"]
    touch = _task("Record the touch panel outputs")
    assert touch["ansible.builtin.copy"]["dest"] == "/etc/curiosity/touch-outputs"
    assert "{{ item.output }} {{ item.device }}" in touch["ansible.builtin.copy"]["content"]
    watcher = _task("Install the display hot-plug watcher")
    assert watcher["ansible.builtin.copy"]["dest"] == "/usr/local/bin/curiosity-display-hotplug"
    assert watcher["ansible.builtin.copy"]["mode"] == "0755"
    removal = _task("Remove the display hot-plug watcher when nothing is declared")
    assert removal["when"].startswith("not (")
    find = _task("Find role-managed display setup files")["ansible.builtin.find"]
    assert {"display-layout", "touch-outputs"} <= set(find["patterns"])


def test_the_layout_cannot_be_combined_with_a_guard_pin() -> None:
    that = _task("Validate the declared display layout and touch panels")["ansible.builtin.assert"]["that"]
    assert "minimal_desktop_display_layout | length == 0 or minimal_desktop_display_guard | length == 0" in that


def test_a_host_without_a_guard_pin_stops_and_removes_the_running_guard() -> None:
    lookup = _task("Look up the installed display guard service")
    assert lookup["register"] == "_display_guard_unit"
    stop = _task("Stop and disable the display guard when it is not configured")["ansible.builtin.systemd"]
    assert stop == {"name": "curiosity-display-guard.service", "state": "stopped", "enabled": False}
    removed = _task("Remove the display guard when it is not configured")
    assert {
        "/etc/systemd/system/curiosity-display-guard.service",
        "/etc/systemd/system/curiosity-display-guard@.service",
        "/usr/local/sbin/curiosity-display-guard",
    } <= set(removed["loop"])
    assert "Reload systemd" in removed["notify"]
    names = [t.get("name") for t in TASKS]
    assert names.index(lookup["name"]) < names.index(stop and "Stop and disable the display guard when it is not configured")
    assert names.index("Stop and disable the display guard when it is not configured") < names.index(removed["name"])


def test_autostart_starts_the_watcher_after_the_helper() -> None:
    autostart = (FILES / "autostart").read_text()
    assert "command -v curiosity-display-hotplug" in autostart
    assert autostart.index("curiosity-display-setup ||") < autostart.index("curiosity-display-hotplug &")
