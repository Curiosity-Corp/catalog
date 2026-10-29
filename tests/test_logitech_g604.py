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



def _include_file(task: dict) -> str | None:
    """The file an include_tasks loads, in its plain or `file:`/`apply:` form."""
    include = task.get("ansible.builtin.include_tasks")
    return include.get("file") if isinstance(include, dict) else include

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
    include = next(t for t in main if _include_file(t) == "logitech-g604.yml")
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
        "DISPLAY": ":99",
        "XDG_SESSION_TYPE": "x11",
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


def test_launcher_leaves_no_stale_watcher_when_the_mouse_comes_back(tmp_path: Path) -> None:
    """A mouse that drops out and returns must end with exactly one watcher (catalog#99).

    The watcher used to be killed without its `xinput test | while read` pipeline, so the
    old pipeline kept running and every later press fired once per stale watcher.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    present = tmp_path / "present"
    watchers = tmp_path / "watchers"
    toggled = tmp_path / "toggled"
    (bin_dir / "xinput").write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            case "$1" in
              float) exit 0;;
              test) echo $$ >> {watchers}; printf 'key press   12 \\n'; exec sleep 30;;
              list)
                if [ "$2" = "--name-only" ]; then
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
    (bin_dir / "xbindkeys").write_text("#!/bin/sh\ntrap 'exit 0' TERM\nwhile :; do sleep 0.1; done\n")
    for tool in ("xinput", "xbindkeys"):
        (bin_dir / tool).chmod(0o755)
    conf = tmp_path / "g604.rc"
    conf.write_text("# test\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CURIOSITY_G604_CONF": str(conf),
        "CURIOSITY_G604_INTERVAL": "0.2",
        "CURIOSITY_G604_LOCK": str(tmp_path / "lock"),
        "CURIOSITY_G604_ENV": "/dev/null",
        "DISPLAY": ":99",
        "XDG_SESSION_TYPE": "x11",
        "CURIOSITY_G604_LIGHTBAR_COMMAND": f"echo toggled >> {toggled}",
    }
    launcher = subprocess.Popen(["sh", str(SCRIPT)], env=env)
    try:
        present.touch()
        _wait_for(watchers)
        first = int(watchers.read_text().split()[0])

        present.unlink()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _alive(first):
            time.sleep(0.05)
        assert not _alive(first), "the old xinput test outlived its watcher"

        present.touch()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(watchers.read_text().split()) < 2:
            time.sleep(0.05)
        time.sleep(0.5)
        pids = [int(p) for p in watchers.read_text().split()]
        assert len(pids) == 2
        assert [p for p in pids if _alive(p)] == [pids[1]]
        # one press per watcher start: nothing fired twice
        assert toggled.read_text().splitlines() == ["toggled", "toggled"]
    finally:
        launcher.terminate()
        launcher.wait(timeout=5)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"


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
    assert DEFAULTS["logitech_g604_dpi"] == 3200
    assert DEFAULTS["logitech_g604_screenshot_keycode"] == 11
    assert DEFAULTS["logitech_g604_screenshot_command"] == "flameshot gui"
    script = SCRIPT.read_text()
    assert "curiosity-mic-mute" in script and "openbox-zone toggle browser" in script
    assert "alt+Left" in script and "alt+Right" in script
    assert '"$desktop_prev_keycode") $desktop_command prev' in script
    assert '"$desktop_next_keycode") $desktop_command next' in script
    assert not (ROLE / "files/curiosity-vt").exists()


def test_launcher_autostarts_in_any_openbox_session_but_not_gnome() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/logitech-g604.yml").read_text())
    names = [task["name"] for task in tasks]
    assert "Autostart the G604 profile launcher in Openbox sessions" in names
    entry = (ROLE / "files/curiosity-g604.desktop").read_text()
    assert "Exec=/usr/local/bin/curiosity-g604" in entry
    assert "OnlyShowIn=Openbox;" in entry


@pytest.mark.parametrize("env", [{"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}, {"DISPLAY": ""}])
def test_launcher_leaves_quietly_without_an_x11_session(tmp_path: Path, env: dict) -> None:
    result = subprocess.run(
        ["sh", str(SCRIPT)],
        env={"PATH": "/usr/bin:/bin", "CURIOSITY_G604_ENV": "/dev/null",
             "CURIOSITY_G604_LOCK": str(tmp_path / "lock"), **env},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert not (tmp_path / "lock").exists()


def test_onboard_button_macros_can_be_switched_off() -> None:
    assert DEFAULTS["logitech_g604_onboard_buttons"] is True
    task = (ROLE / "tasks/logitech-g604.yml").read_text()
    assert "logitech_g604_onboard_buttons | bool | lower" in task


SOLAAR_GUARD = ROLE / "files/curiosity-solaar-g604-dpi"


def _solaar_config(dpi: int, sensitive: dict | None = None) -> str:
    g604 = {
        "_NAME": "G604 Wireless Gaming Mouse",
        "_wpid": "4085",
        "dpi": dpi,
        "onboard_profiles": 1,
        "report_rate": 1,
    }
    if sensitive is not None:
        g604["_sensitive"] = sensitive
    other = {"_NAME": "ERGO K860 Split Keyboard", "fn-swap": True}
    return yaml.safe_dump(["1.1.19-1", other, g604], sort_keys=False)


def _guard(config: Path, dpi: str = "3200") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", "-I", str(SOLAAR_GUARD), str(config), dpi], capture_output=True, text=True, check=False
    )


def test_solaar_guard_stops_solaar_overriding_the_dpi(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(_solaar_config(1600, {"hires-scroll-mode": "ignore"}))
    config.chmod(0o664)
    result = _guard(config)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CHANGED"
    data = yaml.safe_load(config.read_text())
    assert data[0] == "1.1.19-1"
    assert data[1] == {"_NAME": "ERGO K860 Split Keyboard", "fn-swap": True}
    g604 = data[2]
    assert g604["dpi"] == 3200
    assert g604["_sensitive"] == {"hires-scroll-mode": "ignore", "dpi": "ignore"}
    assert g604["onboard_profiles"] == 1 and g604["report_rate"] == 1
    assert stat.S_IMODE(config.stat().st_mode) == 0o664
    # Idempotent.
    again = _guard(config)
    assert again.stdout.strip() == "UNCHANGED"


def test_solaar_guard_adds_the_sensitive_map_when_missing(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(_solaar_config(3200))
    assert _guard(config).stdout.strip() == "CHANGED"
    assert yaml.safe_load(config.read_text())[2]["_sensitive"] == {"dpi": "ignore"}


def test_solaar_guard_leaves_missing_configs_and_rejects_bad_ones(tmp_path: Path) -> None:
    assert _guard(tmp_path / "absent.yaml").stdout.strip() == "ABSENT"
    no_g604 = tmp_path / "other.yaml"
    no_g604.write_text(yaml.safe_dump(["1.1.19-1", {"_NAME": "MX Keys"}]))
    assert _guard(no_g604).stdout.strip() == "ABSENT"
    broken = tmp_path / "broken.yaml"
    broken.write_text("- [unclosed\n")
    result = _guard(broken)
    assert result.returncode == 1 and "cannot read Solaar config" in result.stderr
    assert _guard(no_g604, "fast").returncode == 2


def test_solaar_autostart_runs_the_guard_before_solaar() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/logitech-g604.yml").read_text())
    by_name = {t["name"]: t for t in tasks}
    install = by_name["Install the Solaar G604 DPI guard"]["ansible.builtin.copy"]
    assert install["src"] == "curiosity-solaar-g604-dpi"
    assert install["dest"] == "/usr/local/bin/curiosity-solaar-g604-dpi"
    autostart = by_name["Start Solaar through the G604 DPI guard at login"]
    copy = autostart["ansible.builtin.copy"]
    # Same file name as the packaged entry, so XDG autostart uses this one instead.
    assert copy["dest"] == "{{ dw_user.home }}/.config/autostart/solaar.desktop"
    assert autostart["loop"] == "{{ dev_users }}"
    exec_line = next(line for line in copy["content"].splitlines() if line.startswith("Exec="))
    # No quotes, `;` or `$`: the Desktop Entry spec reserves them in Exec.
    assert exec_line == "Exec=curiosity-solaar-g604-dpi --launch {{ logitech_g604_dpi | int }}"


@pytest.mark.skipif(shutil.which("desktop-file-validate") is None, reason="desktop-file-utils not installed")
def test_solaar_autostart_is_a_valid_desktop_entry(tmp_path: Path) -> None:
    tasks = yaml.safe_load((ROLE / "tasks/logitech-g604.yml").read_text())
    task = next(t for t in tasks if t["name"] == "Start Solaar through the G604 DPI guard at login")
    entry = tmp_path / "solaar.desktop"
    entry.write_text(jinja2.Template(task["ansible.builtin.copy"]["content"]).render(logitech_g604_dpi=3200))
    result = subprocess.run(["desktop-file-validate", str(entry)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def _launch(tmp_path: Path) -> tuple[subprocess.CompletedProcess, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    started = tmp_path / "solaar-started"
    fake = bin_dir / "solaar"
    fake.write_text(f'#!/bin/sh\necho "$@" > {started}\n')
    fake.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "XDG_CONFIG_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path)}
    result = subprocess.run(
        ["python3", "-I", str(SOLAAR_GUARD), "--launch", "3200"], capture_output=True, text=True, env=env, check=False
    )
    return result, started


def test_solaar_launch_fixes_the_xdg_config_then_starts_solaar(tmp_path: Path) -> None:
    config = tmp_path / "xdg/solaar/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(_solaar_config(1600))
    result, started = _launch(tmp_path)
    assert result.returncode == 0, result.stderr
    assert started.read_text().strip() == "--window=hide"
    g604 = yaml.safe_load(config.read_text())[2]
    assert g604["dpi"] == 3200 and g604["_sensitive"] == {"dpi": "ignore"}


def test_solaar_launch_still_starts_solaar_on_a_broken_config(tmp_path: Path) -> None:
    config = tmp_path / "xdg/solaar/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("- [unclosed\n")
    result, started = _launch(tmp_path)
    assert result.returncode == 0
    assert "starting Solaar without the DPI fix" in result.stderr
    assert started.read_text().strip() == "--window=hide"


def test_pointer_accel_speed_is_deskmeet_only_and_empty_elsewhere() -> None:
    assert _derived("logitech_g604_accel_speed", "deskmeet-b760") == "0.6"
    for name in set(DEFAULTS["hardware_profiles"]) - {"deskmeet-b760"} | {"no-such-profile"}:
        assert _derived("logitech_g604_accel_speed", name) == "", name
    tasks = (ROLE / "tasks/logitech-g604.yml").read_text()
    assert "CURIOSITY_G604_ACCEL_SPEED='{{ logitech_g604_accel_speed }}'" in tasks
    assert "logitech_g604_accel_speed | float >= -1" in tasks


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock is required")
@pytest.mark.parametrize("speed", ["0.6", ""])
def test_launcher_sets_the_accel_speed_once_per_attach(tmp_path: Path, speed: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    present = tmp_path / "present"
    setprop = tmp_path / "setprop"
    (bin_dir / "xinput").write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            case "$1" in
              set-prop)
                # Like real xinput: the bare name matches the pointer and the
                # floated keyboard node, so only "pointer:<name>" resolves.
                [ "$2" = "pointer:Logitech G604" ] || exit 1
                echo "$*" >> {setprop}; exit 0;;
              test) exec sleep 30;;
              list)
                if [ "$2" = "--name-only" ]; then
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
    (bin_dir / "xbindkeys").write_text("#!/bin/sh\nexec sleep 30\n")
    for tool in ("xinput", "xbindkeys"):
        (bin_dir / tool).chmod(0o755)
    conf = tmp_path / "g604.rc"
    conf.write_text("# test\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CURIOSITY_G604_CONF": str(conf),
        "CURIOSITY_G604_INTERVAL": "0.2",
        "CURIOSITY_G604_LOCK": str(tmp_path / "lock"),
        "CURIOSITY_G604_ENV": "/dev/null",
        "CURIOSITY_G604_ACCEL_SPEED": speed,
        "DISPLAY": ":99",
        "XDG_SESSION_TYPE": "x11",
    }
    launcher = subprocess.Popen(["sh", str(SCRIPT)], env=env)
    try:
        present.touch()
        if speed:
            _wait_for(setprop)
            time.sleep(1.0)  # several polls while attached: still one call
            assert setprop.read_text().splitlines() == ["set-prop pointer:Logitech G604 libinput Accel Speed 0.6"]
            present.unlink()
            time.sleep(0.6)
            present.touch()
            deadline = time.time() + 5
            while len(setprop.read_text().splitlines()) < 2 and time.time() < deadline:
                time.sleep(0.1)
            assert len(setprop.read_text().splitlines()) == 2, "a replugged mouse gets the speed again"
        else:
            time.sleep(1.0)
            assert not setprop.exists(), "no speed configured, the pointer is left alone"
    finally:
        launcher.terminate()
        launcher.wait(timeout=5)
