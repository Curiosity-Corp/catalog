"""Regression checks for the minimal desktop's tint2 layouts, volume helper and XDG autostart."""

import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import jinja2
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = ROLE / "tasks"
FILES = ROLE / "files/minimal-desktop"
LAYOUT_DIR = FILES / "tint2"
LAYOUTS = ("bottom", "top")

# Options tint2 17.0.1 accepts, taken from the two layouts that run on Ubuntu
# 26.04 (the owner's tint2conf-generated bottom panel and the top panel with the
# volume executor). A new option must be checked against a running tint2 first.
TINT2_17_OPTIONS = frozenset(
    """
    autohide autohide_height autohide_hide_timeout autohide_show_timeout
    background_color background_color_hover background_color_pressed
    border_color border_color_hover border_color_pressed border_sides border_width
    clock_background_id clock_dwheel_command clock_font_color clock_lclick_command
    clock_mclick_command clock_padding clock_rclick_command clock_tooltip
    clock_tooltip_timezone clock_uwheel_command
    disable_transparency
    execp execp_command execp_dwheel_command execp_font execp_font_color
    execp_has_icon execp_interval execp_lclick_command execp_padding
    execp_rclick_command execp_tooltip execp_uwheel_command
    font_shadow
    launcher_background_id launcher_icon_size launcher_icon_theme
    launcher_icon_theme_override launcher_item_app launcher_padding launcher_tooltip
    mouse_effects mouse_hover_icon_asb mouse_left mouse_middle mouse_pressed_icon_asb
    mouse_right mouse_scroll_down mouse_scroll_up
    panel_background_id panel_dock panel_items panel_layer panel_margin panel_monitor
    panel_padding panel_position panel_shrink panel_size panel_window_name
    rounded strut_policy
    systray_background_id systray_icon_asb systray_icon_size systray_monitor
    systray_name_filter systray_padding systray_sort
    task_active_background_id task_active_font_color task_align task_background_id
    task_centered task_font task_font_color task_icon task_icon_asb
    task_iconified_background_id task_maximum_size task_padding task_text
    task_tooltip task_urgent_background_id
    taskbar_active_background_id taskbar_always_show_all_desktop_tasks
    taskbar_background_id taskbar_distribute_size taskbar_hide_different_desktop
    taskbar_hide_different_monitor taskbar_hide_if_empty taskbar_hide_inactive_tasks
    taskbar_mode taskbar_name taskbar_name_active_background_id
    taskbar_name_active_font_color taskbar_name_background_id taskbar_name_font
    taskbar_name_font_color taskbar_name_padding taskbar_padding taskbar_sort_order
    time1_font time1_format time1_timezone time2_font time2_format time2_timezone
    tooltip_background_id tooltip_font tooltip_font_color tooltip_hide_timeout
    tooltip_padding tooltip_show_timeout
    urgent_nb_of_blink wm_menu
    """.split()
)
# tint2 17 rejects these ("invalid option"); the retired tint2rc used them.
TINT2_17_REJECTED_OPTIONS = ("taskbar_name_active_color", "taskbar_name_color", "task_icon_size")
BACKGROUND_REFERENCES = re.compile(r"^[a-z_]*background_id$")
HIDDEN_XDG_IDS = {"nm-applet", "blueman", "pasystray", "picom", "light-locker", "xfce4-screensaver"}


def _tasks(name: str) -> list[dict]:
    return yaml.safe_load((TASKS / name).read_text())


def _named(tasks: list[dict], fragment: str) -> dict:
    return next(task for task in tasks if fragment in task.get("name", ""))


def _defaults() -> dict:
    return yaml.safe_load((ROLE / "defaults/main.yml").read_text())


def _options(path: Path) -> list[tuple[str, str]]:
    """Return the (key, value) pairs of a tint2rc in file order."""

    pairs: list[tuple[str, str]] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        assert separator, f"{path.name}: not a key = value line: {line!r}"
        pairs.append((key.strip(), value.strip()))
    return pairs


def _layout_files() -> list[Path]:
    return sorted(LAYOUT_DIR.glob("*.tint2rc"))


def test_the_broken_single_tint2rc_is_gone_and_both_layouts_ship() -> None:
    assert not (FILES / "tint2rc").exists()
    assert [path.stem for path in _layout_files()] == sorted(LAYOUTS)


@pytest.mark.parametrize("path", _layout_files(), ids=lambda path: path.stem)
def test_background_ids_refer_to_blocks_defined_earlier(path: Path) -> None:
    # tint2 17 segfaults when panel_background_id points at a background that
    # has not been defined yet: every `rounded =` line opens a background block.
    blocks = 0
    for key, value in _options(path):
        if key == "rounded":
            blocks += 1
        elif BACKGROUND_REFERENCES.match(key):
            assert int(value) <= blocks, (
                f"{path.name}: {key} = {value} but only {blocks} background "
                "block(s) are defined above it"
            )
    assert blocks >= 1


@pytest.mark.parametrize("path", _layout_files(), ids=lambda path: path.stem)
def test_only_tint2_17_options_are_used(path: Path) -> None:
    keys = {key for key, _ in _options(path)}
    assert not keys & set(TINT2_17_REJECTED_OPTIONS)
    assert keys <= TINT2_17_OPTIONS, f"{path.name}: unknown options {sorted(keys - TINT2_17_OPTIONS)}"
    assert not set(TINT2_17_REJECTED_OPTIONS) & TINT2_17_OPTIONS
    assert "Managed by Ansible" in path.read_text()


def test_layout_shapes() -> None:
    bottom = dict(_options(LAYOUT_DIR / "bottom.tint2rc"))
    top = dict(_options(LAYOUT_DIR / "top.tint2rc"))
    assert bottom["panel_items"] == "TSC" and bottom["panel_size"] == "100% 32"
    assert bottom["panel_position"].startswith("bottom")
    assert top["panel_items"] == "LTSEC" and top["panel_size"] == "100% 36"
    assert top["panel_position"].startswith("top")
    assert top["execp_command"] == "/usr/local/bin/tint2-volume-status"
    # Only orage-less, battery-less panels ship; zenity backs the calendar.
    assert bottom["clock_rclick_command"] == ""
    assert "battery" not in (LAYOUT_DIR / "bottom.tint2rc").read_text().replace("battery item", "")
    for path in _layout_files():
        for key, value in _options(path):
            if key == "launcher_item_app":
                assert value.startswith("/usr/share/applications/"), value


def test_launcher_items_and_helpers_come_from_installed_packages() -> None:
    packages = _named(_tasks("minimal-desktop-packages.yml"), "Install minimal desktop packages")[
        "ansible.builtin.apt"
    ]["name"]
    assert {"python3-xdg", "lxpolkit", "pulseaudio-utils", "zenity", "arandr", "pcmanfm", "lxterminal", "pavucontrol"} <= set(packages)
    # policykit-1-gnome is absent from Debian trixie, so it must not be required.
    assert "policykit-1-gnome" not in packages
    top = (LAYOUT_DIR / "top.tint2rc").read_text()
    for desktop_file in ("pcmanfm.desktop", "lxterminal.desktop", "arandr.desktop", "org.pulseaudio.pavucontrol.desktop"):
        assert f"/usr/share/applications/{desktop_file}" in top


def test_layout_default_and_validation() -> None:
    assert _defaults()["minimal_desktop_tint2_layout"] == "bottom"
    tasks = _tasks("minimal-desktop-tint2.yml")
    validate = _named(tasks, "Validate the tint2 panel layout")
    condition = validate["ansible.builtin.assert"]["that"]
    assert "Unknown tint2 layout" in validate["ansible.builtin.assert"]["fail_msg"]
    assert "bottom, top" in validate["ansible.builtin.assert"]["fail_msg"]

    environment = jinja2.Environment()
    accepts = environment.compile_expression(condition[0])
    for layout in LAYOUTS:
        assert accepts(_minimal_desktop_tint2_layout=layout) is True
    for layout in ("", "side", "Bottom", None):
        assert accepts(_minimal_desktop_tint2_layout=layout) is False
    assert {path.stem for path in _layout_files()} == set(LAYOUTS)


def test_layout_is_selected_per_user_and_deployed_with_user_ownership() -> None:
    tasks = _tasks("minimal-desktop-tint2.yml")
    select = _named(tasks, "Select the tint2 panel layout")["ansible.builtin.set_fact"]
    template = jinja2.Environment().from_string(select["_minimal_desktop_tint2_layout"])
    default_layout = _defaults()["minimal_desktop_tint2_layout"]
    plain_user = {"name": "a", "home": "/home/a"}
    top_user = {"name": "b", "home": "/home/b", "tint2_layout": "top"}
    assert template.render(dw_user=plain_user, minimal_desktop_tint2_layout=default_layout) == "bottom"
    assert template.render(dw_user=top_user, minimal_desktop_tint2_layout=default_layout) == "top"
    assert template.render(dw_user=plain_user, minimal_desktop_tint2_layout="top") == "top"

    deploy = _named(tasks, "Deploy the selected tint2 panel layout")["ansible.builtin.copy"]
    assert deploy["src"] == "minimal-desktop/tint2/{{ _minimal_desktop_tint2_layout }}.tint2rc"
    assert deploy["dest"] == "{{ dw_user.home }}/.config/tint2/tint2rc"
    assert deploy["owner"] == "{{ dw_user.name }}"
    assert deploy["group"] == "{{ dw_user.name }}"
    assert deploy["mode"] == "0644"
    assert not any("tint2rc" == task.get("ansible.builtin.copy", {}).get("src") for task in tasks)
    assert "28px" not in (TASKS / "minimal-desktop-tint2.yml").read_text()


def test_volume_helper_is_installed_root_owned_and_executable() -> None:
    install = _named(_tasks("minimal-desktop.yml"), "Install the tint2 volume status helper")[
        "ansible.builtin.copy"
    ]
    assert install["src"] == "minimal-desktop/tint2-volume-status"
    assert install["dest"] == "/usr/local/bin/tint2-volume-status"
    assert (install["owner"], install["group"], install["mode"]) == ("root", "root", "0755")
    helper = FILES / "tint2-volume-status"
    assert "Managed by Ansible" in helper.read_text()
    assert helper.stat().st_mode & stat.S_IXUSR
    assert subprocess.run(["sh", "-n", str(helper)], capture_output=True).returncode == 0
    top = dict(_options(LAYOUT_DIR / "top.tint2rc"))
    assert top["execp_command"] == install["dest"]


def _run_volume_helper(pactl_mute: str, pactl_volume: str) -> str:
    with tempfile.TemporaryDirectory(prefix="tint2-volume-") as temp_dir:
        bin_dir = Path(temp_dir)
        pactl = bin_dir / "pactl"
        pactl.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            f"  get-sink-mute) printf '%s\\n' '{pactl_mute}' ;;\n"
            f"  get-sink-volume) printf '%s\\n' '{pactl_volume}' ;;\n"
            "esac\n"
        )
        pactl.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
        result = subprocess.run(
            ["sh", str(FILES / "tint2-volume-status")], text=True, capture_output=True, env=environment, timeout=10
        )
        assert result.returncode == 0
        return result.stdout


def test_volume_helper_reports_volume_and_mute() -> None:
    volume = "Volume: front-left: 32768 /  50% / -18.06 dB,   front-right: 32768 /  50% / -18.06 dB"
    assert _run_volume_helper("Mute: no", volume) == "Sound 50%"
    full = "Volume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB"
    assert _run_volume_helper("Mute: no", full) == "Sound 100%"
    quiet = "Volume: mono: 3277 /   5% / -78.00 dB"
    assert _run_volume_helper("Mute: no", quiet) == "Sound 5%"
    assert _run_volume_helper("Mute: yes", volume) == "Muted"
    assert _run_volume_helper("Mute: no", "") == "Sound unavailable"


def test_xdg_autostart_overrides_are_configurable_and_cover_the_required_ids() -> None:
    hidden = _defaults()["minimal_desktop_xdg_autostart_hidden"]
    assert set(hidden) == HIDDEN_XDG_IDS
    assert len(hidden) == len(set(hidden))


def test_xdg_autostart_overrides_are_deployed_per_user_and_hidden() -> None:
    tasks = _tasks("minimal-desktop-xdg-autostart.yml")
    directory = _named(tasks, "Ensure the XDG autostart directory exists")["ansible.builtin.file"]
    assert directory["path"] == "{{ dw_user.home }}/.config/autostart"
    assert directory["state"] == "directory"
    assert directory["owner"] == directory["group"] == "{{ dw_user.name }}"

    hide = _named(tasks, "Hide XDG autostart entries")
    assert hide["loop"] == "{{ minimal_desktop_xdg_autostart_hidden }}"
    copy = hide["ansible.builtin.copy"]
    assert copy["dest"] == "{{ dw_user.home }}/.config/autostart/{{ item }}.desktop"
    assert copy["owner"] == copy["group"] == "{{ dw_user.name }}"
    assert copy["mode"] == "0644"
    for hidden_id in sorted(HIDDEN_XDG_IDS):
        rendered = jinja2.Environment().from_string(copy["content"]).render(item=hidden_id)
        lines = rendered.splitlines()
        assert "# Managed by Ansible" in lines[0]
        assert lines[1:] == [
            "[Desktop Entry]",
            "Type=Application",
            f"Name={hidden_id} (started by the Openbox autostart)",
            "Hidden=true",
        ]

    validate = _named(tasks, "Validate the hidden XDG autostart ids")["ansible.builtin.assert"]
    environment = jinja2.Environment()
    environment.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None  # Ansible's test
    accepts = environment.compile_expression(validate["that"][-1])
    assert accepts(minimal_desktop_xdg_autostart_hidden=["nm-applet", "org.example.App"]) is True
    assert accepts(minimal_desktop_xdg_autostart_hidden=["../evil"]) is False


def test_xdg_autostart_is_looped_over_dev_users_and_never_references_the_primary_account() -> None:
    text = (TASKS / "minimal-desktop-xdg-autostart.yml").read_text()
    assert re.search(r"\bdev_user(_home)?\b", text) is None
    includes = {
        task["ansible.builtin.include_tasks"]: task for task in _tasks("minimal-desktop.yml") if "ansible.builtin.include_tasks" in task
    }
    task = includes["minimal-desktop-xdg-autostart.yml"]
    assert task["loop"] == "{{ dev_users }}"
    assert task["loop_control"]["loop_var"] == "dw_user"


def _run_autostart(*, live_panel_display: str | None) -> tuple[subprocess.CompletedProcess[str], list[str], bool]:
    """Run the autostart with stub commands and an optional pre-existing panel."""

    autostart = (FILES / "autostart").read_text()
    with tempfile.TemporaryDirectory(prefix="panel-autostart-") as temp_dir:
        root = Path(temp_dir)
        bin_dir = root / "bin"
        proc_dir = root / "proc"
        bin_dir.mkdir()
        log = root / "log"
        log.write_text("")
        previous_panel = subprocess.Popen(["sleep", "30"])
        try:
            if live_panel_display is not None:
                (proc_dir / str(previous_panel.pid)).mkdir(parents=True)
                (proc_dir / str(previous_panel.pid) / "environ").write_bytes(
                    f"DISPLAY={live_panel_display}\0".encode()
                )
            stubs = {
                "pgrep": '#!/bin/sh\ncase "$*" in\n  *tint2*) echo "$HARNESS_PANEL_PID" ;;\n  *) exit 1 ;;\nesac\n',
                "tint2": '#!/bin/sh\nprintf "tint2 %s\\n" "$*" >>"$HARNESS_LOG"\n',
            }
            for name in (
                "xsetroot", "feh", "picom", "dunst", "nm-applet", "pasystray",
                "blueman-applet", "flameshot", "xscreensaver",
            ):
                stubs[name] = f'#!/bin/sh\nprintf "{name}\\n" >>"$HARNESS_LOG"\n'
            for name, content in stubs.items():
                script = bin_dir / name
                script.write_text(content)
                script.chmod(0o755)
            # The PATH holds only the stubs and the plain utilities the script
            # uses, so a real focuspass-screenlock or tint2 on the host is
            # never started or signalled.
            for tool in ("id", "tr", "sed", "head", "sleep", "stat", "readlink"):
                found = shutil.which(tool)
                assert found is not None, tool
                (bin_dir / tool).symlink_to(found)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": str(bin_dir),
                    "DISPLAY": ":test",
                    "HOME": str(root / "home"),
                    "FOCUSPASS_PROC_ROOT": str(proc_dir),
                    "HARNESS_LOG": str(log),
                    "HARNESS_PANEL_PID": str(previous_panel.pid),
                }
            )
            result = subprocess.run(["/bin/sh", "-c", autostart], text=True, capture_output=True, env=environment, timeout=20)
            previous_panel.poll()
            still_alive = previous_panel.returncode is None
            # Background stubs may still be flushing their log lines.
            subprocess.run(["sleep", "0.3"], check=False)
            return result, log.read_text().splitlines(), still_alive
        finally:
            if previous_panel.poll() is None:
                previous_panel.kill()
            previous_panel.wait()


def test_autostart_launches_tint2_with_the_deployed_config() -> None:
    result, log, _ = _run_autostart(live_panel_display=None)
    assert result.returncode == 0, result.stderr
    assert any(line.startswith("tint2 -c ") and line.endswith("/home/.config/tint2/tint2rc") for line in log), log


def test_autostart_replaces_the_panel_on_this_display_only() -> None:
    same_display, _, alive = _run_autostart(live_panel_display=":test")
    assert same_display.returncode == 0, same_display.stderr
    assert alive is False
    other_display, _, alive = _run_autostart(live_panel_display=":other")
    assert other_display.returncode == 0, other_display.stderr
    assert alive is True


def test_autostart_leaves_the_polkit_agent_to_xdg_autostart() -> None:
    autostart = (FILES / "autostart").read_text()
    commands = [line.strip() for line in autostart.splitlines() if not line.lstrip().startswith("#")]
    assert not any(command.startswith(("lxpolkit", "/usr/lib/policykit-1-gnome")) for command in commands)
    assert "pkill" not in autostart
    assert "lxpolkit" in autostart  # the reasoning stays documented next to the applets
