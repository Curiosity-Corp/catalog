"""The Logitech G604 button profile and the double-click interval."""

import os
import shutil
import stat
import subprocess
import textwrap
import time
from pathlib import Path

import jinja2
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
SCRIPT = ROLE / "files/curiosity-g604"


def _environment() -> jinja2.Environment:
    from ansible.plugins.filter.core import FilterModule
    from ansible.plugins.test.core import TestModule

    env = jinja2.Environment()
    env.filters.update(FilterModule().filters())
    env.tests.update(TestModule().tests())
    return env


def _derived(name: str, profile: str) -> str:
    return (
        _environment()
        .from_string(DEFAULTS[name])
        .render(hardware_profiles=DEFAULTS["hardware_profiles"], hardware_profile=profile)
        .strip()
    )


def test_only_deskmeet_opts_in_to_the_g604_profile() -> None:
    for name in DEFAULTS["hardware_profiles"]:
        expected = "True" if name == "deskmeet-b760" else "False"
        assert _derived("logitech_g604_enabled", name) == expected, name
    assert _derived("logitech_g604_enabled", "missing-profile") == "False"


def test_double_click_defaults_and_deskmeet_override() -> None:
    assert _derived("minimal_desktop_double_click_ms", "generic") == "500"
    assert _derived("minimal_desktop_double_click_ms", "deskmeet-b760") == "200"
    assert _derived("minimal_desktop_idle_lock_minutes", "generic") == "10"
    assert _derived("minimal_desktop_idle_lock_minutes", "deskmeet-b760") == "60"


def test_default_buttons() -> None:
    # 6/7 are the wheel tilt; the lightbar is thumb button 3, keycode 12
    # (thumb buttons 1-3 are keycodes 10-12 from the G604's keyboard device).
    assert DEFAULTS["logitech_g604_copy_button"] == 6
    assert DEFAULTS["logitech_g604_paste_button"] == 7
    assert DEFAULTS["logitech_g604_lightbar_keycode"] == 12
    assert DEFAULTS["logitech_g604_lightbar_command"] == "litra toggle"


def test_xbindkeys_template_maps_wheel_tilt_to_copy_and_paste() -> None:
    template = (ROLE / "templates/curiosity-g604-xbindkeys.rc.j2").read_text()
    rendered = jinja2.Template(template).render(
        **{key: value for key, value in DEFAULTS.items() if key.startswith("logitech_g604_")}
    )
    bindings = [line.strip() for line in rendered.splitlines() if line.strip() and not line.startswith("#")]
    assert bindings == [
        '"xdotool key --clearmodifiers ctrl+Insert"',
        "b:6",
        '"xdotool key --clearmodifiers shift+Insert"',
        "b:7",
    ]


def test_tasks_and_autostart_wire_the_launcher() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/logitech-g604.yml").read_text())
    names = [task["name"] for task in tasks]
    assert "Install the G604 profile launcher" in names
    assert "Render the G604 thumb button settings" in names
    modules = {key for task in tasks for key in task if key.startswith("ansible.builtin.")}
    assert {"ansible.builtin.assert", "ansible.builtin.apt", "ansible.builtin.template", "ansible.builtin.copy"} <= modules
    main = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    include = next(t for t in main if t.get("ansible.builtin.include_tasks") == "logitech-g604.yml")
    assert "logitech_g604_enabled | bool" in include["when"]
    autostart = (ROLE / "files/minimal-desktop/autostart").read_text()
    assert "curiosity-g604 &" in autostart


def test_openbox_and_gtk_use_the_double_click_variable() -> None:
    openbox = (ROLE / "tasks/minimal-desktop-openbox.yml").read_text()
    assert "{{ minimal_desktop_double_click_ms }}" in openbox
    gtk = (ROLE / "tasks/minimal-desktop-gtk-theme.yml").read_text()
    assert gtk.count("gtk-double-click-time={{ minimal_desktop_double_click_ms }}") == 2
    assert "<doubleClickTime>500</doubleClickTime>" in (ROLE / "files/minimal-desktop/rc.xml").read_text()


def test_launcher_is_valid_posix_sh() -> None:
    assert os.access(SCRIPT, os.X_OK)
    subprocess.run(["sh", "-n", str(SCRIPT)], check=True)


def test_launcher_watches_the_thumb_button_keycode() -> None:
    script = SCRIPT.read_text()
    assert "xinput float" in script
    assert '"$lightbar_keycode") sh -c "$lightbar_command"' in script
    assert "CURIOSITY_G604_LIGHTBAR_KEYCODE:-12" in script


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock is required")
def test_launcher_binds_only_while_the_mouse_is_present(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    present = tmp_path / "present"
    started = tmp_path / "started"
    stopped = tmp_path / "stopped"
    (bin_dir / "xinput").write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            case "$1" in
              float) echo "$2" >> {tmp_path / "floated"}; exit 0;;
              test) printf 'key press   12 \\n'; exec sleep 30;;
              list)
                if [ "$2" = "--name-only" ]; then
                  echo "Virtual core pointer"
                  [ -e {present} ] && echo "Logitech G604"
                else
                  [ -e {present} ] && printf '    Logitech G604   id=15\\t[slave  keyboard (3)]\\n'
                fi
                exit 0;;
            esac
            exit 0
            """
        )
    )
    (bin_dir / "xbindkeys").write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            echo "$$ $*" >> {started}
            trap 'echo stopped >> {stopped}; exit 0' TERM
            while :; do sleep 0.1; done
            """
        )
    )
    for tool in ("xinput", "xbindkeys"):
        (bin_dir / tool).chmod((bin_dir / tool).stat().st_mode | stat.S_IXUSR)
    conf = tmp_path / "g604.rc"
    conf.write_text("# test\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CURIOSITY_G604_CONF": str(conf),
        "CURIOSITY_G604_INTERVAL": "0.2",
        "CURIOSITY_G604_LOCK": str(tmp_path / "lock"),
        "CURIOSITY_G604_ENV": "/dev/null",
        "CURIOSITY_G604_LIGHTBAR_COMMAND": f"echo toggled >> {tmp_path / 'toggled'}",
    }
    launcher = subprocess.Popen(["sh", str(SCRIPT)], env=env)
    try:
        time.sleep(0.8)
        assert not started.exists(), "no G604 present, nothing may be bound"

        present.touch()
        _wait_for(started)
        assert f"-n -f {conf}" in started.read_text()
        _wait_for(tmp_path / "toggled")
        assert (tmp_path / "floated").read_text().split() == ["15"]

        # A second launcher on the same display must be a no-op.
        assert subprocess.run(["sh", str(SCRIPT)], env=env, timeout=5).returncode == 0

        present.unlink()
        _wait_for(stopped)
    finally:
        launcher.terminate()
        launcher.wait(timeout=5)


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path.name} was never created")


DESKTOP_SCRIPT = ROLE / "files/curiosity-desktop"


def _run_desktop(tmp_path: Path, count: int, current: int, direction: str) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    out = tmp_path / "switched"
    (bin_dir / "xdotool").write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            case "$1" in
              get_num_desktops) echo {count};;
              get_desktop) echo {current};;
              set_desktop) echo "$2" > {out};;
            esac
            """
        )
    )
    (bin_dir / "xdotool").chmod(0o755)
    out.unlink(missing_ok=True)
    subprocess.run(
        ["sh", str(DESKTOP_SCRIPT), direction],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        check=True,
    )
    return out.read_text().strip() if out.exists() else ""


@pytest.mark.parametrize(
    ("current", "direction", "target"),
    [
        (0, "next", "1"),
        (2, "next", "3"),
        (3, "next", "0"),
        (3, "prev", "2"),
        (1, "prev", "0"),
        (0, "prev", "3"),
    ],
)
def test_desktop_switcher_rotates_through_four_desktops(tmp_path: Path, current: int, direction: str, target: str) -> None:
    assert _run_desktop(tmp_path, 4, current, direction) == target


def test_desktop_switcher_does_nothing_with_a_single_desktop(tmp_path: Path) -> None:
    assert _run_desktop(tmp_path, 1, 0, "next") == ""


def test_desktop_switcher_rejects_bad_usage() -> None:
    assert subprocess.run(["sh", str(DESKTOP_SCRIPT), "sideways"], capture_output=True).returncode == 2


def test_top_buttons_rotate_the_openbox_desktops() -> None:
    assert DEFAULTS["logitech_g604_desktop_prev_keycode"] == 191  # F13
    assert DEFAULTS["logitech_g604_desktop_next_keycode"] == 192  # F14
    tasks = yaml.safe_load((ROLE / "tasks/logitech-g604.yml").read_text())
    names = [task["name"] for task in tasks]
    assert "Install the previous/next desktop switcher" in names
    assert "Reassign the G604 top and back buttons to F13/F14/KEY_4/KEY_5/KEY_6 in the onboard profile" in names
    assert DEFAULTS["logitech_g604_back_keycode"] == 15  # KEY_6
    assert DEFAULTS["logitech_g604_forward_keycode"] == 10  # KEY_1, thumb button 1
    assert DEFAULTS["logitech_g604_mic_keycode"] == 13  # KEY_4
    assert DEFAULTS["logitech_g604_zone_keycode"] == 14  # KEY_5
    assert "Install the microphone mute toggle" in names
    assert DEFAULTS["logitech_g604_dpi"] == 1600
    assert DEFAULTS["logitech_g604_screenshot_keycode"] == 11
    assert DEFAULTS["logitech_g604_screenshot_command"] == "flameshot gui"
    script = SCRIPT.read_text()
    assert "curiosity-mic-mute" in script and "openbox-zone toggle browser" in script
    assert "alt+Left" in script and "alt+Right" in script
    assert '"$desktop_prev_keycode") $desktop_command prev' in script
    assert '"$desktop_next_keycode") $desktop_command next' in script
    assert not (ROLE / "files/curiosity-vt").exists()
