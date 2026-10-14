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


def test_default_buttons_are_distinct_and_lightbar_toggles() -> None:
    buttons = {
        DEFAULTS["logitech_g604_copy_button"],
        DEFAULTS["logitech_g604_paste_button"],
        DEFAULTS["logitech_g604_lightbar_button"],
    }
    assert len(buttons) == 3
    # 6/7 are the wheel tilt; 8/9 are browser back/forward and must stay free.
    assert DEFAULTS["logitech_g604_copy_button"] == 6
    assert DEFAULTS["logitech_g604_paste_button"] == 7
    assert DEFAULTS["logitech_g604_lightbar_button"] not in (8, 9)
    assert DEFAULTS["logitech_g604_lightbar_command"] == "litra toggle"


def test_xbindkeys_template_maps_tilt_to_copy_paste_and_thumb_to_litra() -> None:
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
        '"litra toggle"',
        "b:10",
    ]


def test_tasks_and_autostart_wire_the_launcher() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/logitech-g604.yml").read_text())
    names = [task["name"] for task in tasks]
    assert "Install the G604 profile launcher" in names
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
            echo "Virtual core pointer"
            [ -e {present} ] && echo "Logitech G604"
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
    }
    launcher = subprocess.Popen(["sh", str(SCRIPT)], env=env)
    try:
        time.sleep(0.8)
        assert not started.exists(), "no G604 present, nothing may be bound"

        present.touch()
        _wait_for(started)
        assert f"-n -f {conf}" in started.read_text()

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
