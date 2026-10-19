"""The StreamStack host wiring: loopback cameras, udev, settings and the install.

The application itself (CLI, panel menu, auto mode, presets) is vendored from
the StreamStack project at roles/developer_workstation/files/streamstack and
tested there; these tests cover how the role installs and configures it.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
TASKS = yaml.safe_load((ROLE / "tasks/streamstack.yml").read_text())
APP = ROLE / "files/streamstack"


def _environment() -> jinja2.Environment:
    from ansible.plugins.filter.core import FilterModule
    from ansible.plugins.test.core import TestModule

    env = jinja2.Environment()
    env.filters.update(FilterModule().filters())
    env.tests.update(TestModule().tests())
    return env


def _task(name: str) -> dict:
    return next(t for t in TASKS if t["name"] == name)


def _render(text: str, **extra) -> str:
    context = {k: v for k, v in DEFAULTS.items() if k.startswith("streamstack_")}
    context.update(extra)
    return _environment().from_string(text).render(**context)


def test_only_deskmeet_opts_in_by_profile() -> None:
    for name in DEFAULTS["hardware_profiles"]:
        expected = "True" if name == "deskmeet-b760" else "False"
        rendered = _render(
            DEFAULTS["streamstack_enabled"],
            hardware_profiles=DEFAULTS["hardware_profiles"],
            hardware_profile=name,
        ).strip()
        assert rendered == expected, name


def test_main_includes_streamstack_for_desktops_only() -> None:
    main = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    (task,) = [
        t for t in main
        if isinstance(t.get("ansible.builtin.include_tasks"), dict)
        and t["ansible.builtin.include_tasks"]["file"] == "streamstack.yml"
    ]
    # `apply` carries the tag into the included tasks, so --tags streamstack works.
    assert "streamstack" in task["tags"]
    assert "streamstack" in task["ansible.builtin.include_tasks"]["apply"]["tags"]
    assert "streamstack_enabled | bool" in task["when"]
    assert "workstation_profile == 'desktop'" in task["when"]


def test_module_install_covers_the_running_kernel_before_a_reboot() -> None:
    """The flavour metapackage tracks the newest kernel; the running one needs its own."""
    block = next(t for t in TASKS if t.get("name") == "Install the v4l2loopback kernel module")
    names = block["block"][0]["ansible.builtin.apt"]["name"]
    assert "linux-modules-v4l2loopback-generic" in names
    assert "linux-main-modules-v4l2loopback-{{ ansible_facts['kernel'] }}" in names
    assert block["rescue"][0]["ansible.builtin.apt"]["name"] == "v4l2loopback-dkms"


def test_loopback_options_make_two_browser_visible_cameras() -> None:
    content = _render(_task("Configure the StreamStack loopback cameras")["ansible.builtin.copy"]["content"])
    (options,) = [line for line in content.splitlines() if line.startswith("options")]
    assert options == (
        "options v4l2loopback devices=2 video_nr=42,43 "
        'card_label="Canon EOS 50D,StreamStack Camera" exclusive_caps=1,1 max_buffers=2'
    )


def test_udev_rules_cover_both_nodes_and_the_capture_card() -> None:
    task = _task("Grant the console user the StreamStack cameras")
    dest = task["ansible.builtin.copy"]["dest"]
    number = int(re.search(r"/(\d+)-", dest).group(1))
    assert number <= 70, "uaccess rules must sort before 73-seat-late"
    rules = _render(task["ansible.builtin.copy"]["content"])
    active = [line for line in rules.splitlines() if line and not line.startswith("#")]
    for node in ("video42", "video43"):
        assert any(f'KERNEL=="{node}"' in line and 'TAG+="uaccess"' in line for line in active)
    assert not any("ATTR{name}" in line for line in active)
    assert any('ATTRS{idProduct}=="0066"' in line and "streamstack/camlink" in line for line in active)


def test_settings_reject_labels_modprobe_cannot_carry() -> None:
    checks = _task("Validate the StreamStack settings")["ansible.builtin.assert"]["that"]
    assert "streamstack_camera_label is not search('[,\"\\n]')" in checks
    assert "streamstack_canon_bridge in ['gphoto2', 'command', 'none']" in checks


LOOPBACK = ROLE / "files/curiosity-streamstack-loopback"


def test_loopback_reload_never_touches_a_busy_module() -> None:
    script = LOOPBACK.read_text()
    assert script.startswith("#!/usr/bin/env bash")
    # A busy (or externally owned) module only gains missing nodes; it is
    # never unloaded under a call.
    guard = script.index('"$external" == true || "$(cat /sys/module/v4l2loopback/refcnt)" != "0"')
    assert guard < script.index("modprobe -r v4l2loopback")
    busy_branch = script[guard:script.index("modprobe -r v4l2loopback")]
    assert "add_missing" in busy_branch and "exit 0" in busy_branch
    assert "v4l2loopback-ctl add" in script
    task = _task("Bring up the StreamStack loopback cameras")
    assert task["ansible.builtin.command"] == "/usr/local/sbin/curiosity-streamstack-loopback"
    unit = (ROLE / "files/curiosity-streamstack-loopback.service").read_text()
    assert "ExecStart=/usr/local/sbin/curiosity-streamstack-loopback" in unit
    assert "After=systemd-modules-load.service" in unit


def _env(**extra) -> str:
    return _render(_task("Render the StreamStack settings")["ansible.builtin.copy"]["content"], **extra)


def test_external_camera_mode_adopts_the_node_another_tool_owns() -> None:
    for name in ("Configure the StreamStack loopback cameras", "Find other v4l2loopback options on this host"):
        assert "not (streamstack_camera_external | bool)" in str(_task(name)["when"]), name
    removal = _task("Leave the loopback options to the tool that owns the camera")
    assert removal["ansible.builtin.file"]["state"] == "absent"
    assert removal["when"] == "streamstack_camera_external | bool"
    assert "STREAMSTACK_CAMERA_EXTERNAL=false" in _env()
    assert "STREAMSTACK_CANON_BRIDGE=gphoto2" in _env()
    external = _env(streamstack_camera_external=True, streamstack_canon_bridge="command",
                    streamstack_canon_bridge_command="tool")
    assert "STREAMSTACK_CAMERA_EXTERNAL=true" in external
    assert "STREAMSTACK_CANON_BRIDGE=none" in external
    autostart = _task("Decide whether the Canon bridge starts at login")["ansible.builtin.set_fact"]
    assert "streamstack_camera_external" in autostart["_streamstack_bridge_autostart"]


def test_loopback_script_adds_only_the_program_node_in_external_mode(tmp_path: Path) -> None:
    # Run the bring-up against a fake sysfs: in external mode it must add the
    # program node and never load, unload or re-create the camera node.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    for tool in ("modprobe", "v4l2loopback-ctl", "cat"):
        stub = bin_dir / tool
        if tool == "cat":
            stub.write_text('#!/bin/sh\ncase "$1" in */video10/name) echo "Canon EOS 50D Webcam";; '
                            '*/refcnt) echo 1;; *) exit 1;; esac\n')
        else:
            stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> {log}\n')
        stub.chmod(0o755)
    env_file = tmp_path / "streamstack.env"
    env_file.write_text(_env(streamstack_camera_external=True, streamstack_camera_video_nr=10,
                             streamstack_camera_label="Canon EOS 50D Webcam",
                             streamstack_program_video_nr=99999))
    script = LOOPBACK.read_text().replace("/sys/module/v4l2loopback", str(tmp_path))
    patched = tmp_path / "loopback"
    patched.write_text(script)
    result = subprocess.run(["bash", str(patched)], capture_output=True, text=True, check=False, timeout=30,
                            env=dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
                                     STREAMSTACK_ENV_FILE=str(env_file)))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "added"
    calls = log.read_text().splitlines()
    assert calls == ["v4l2loopback-ctl add -n StreamStack Camera -x 1 -b 2 99999"]


def test_user_defaults_are_seeded_not_overwritten() -> None:
    task = _task("Seed StreamStack OBS and Easy Effects defaults for each user")
    assert task["ansible.builtin.command"]["argv"] == ["/usr/local/bin/streamstack", "setup", "--quiet"]
    assert task["become_user"] == "{{ item.item.name }}"
    assert "--force" not in task["ansible.builtin.command"]["argv"]


def test_panel_menu_is_installed_and_the_tray_icon_is_opt_in() -> None:
    assert DEFAULTS["streamstack_tray_autostart"] is False
    menu = _task("List StreamStack in the application menu")["ansible.builtin.copy"]
    assert menu["src"] == "streamstack/packaging/applications/streamstack.desktop"
    assert "Exec=streamstack-tray --menu" in (ROLE / "files" / menu["src"]).read_text()
    autostart = _task("Start the StreamStack panel icon at login")
    assert autostart["when"] == "streamstack_tray_autostart | bool"
    assert (ROLE / "files" / autostart["ansible.builtin.copy"]["src"]).is_file()
    for package in ("python3-gi", "gir1.2-ayatanaappindicator3-0.1", "python3-websocket"):
        assert package in DEFAULTS["streamstack_packages"]


def test_every_vendored_file_the_role_installs_exists() -> None:
    def sources(task: dict) -> list[str]:
        copy = task.get("ansible.builtin.copy", {})
        src = copy.get("src")
        if not src or "{{" not in src:
            return [src] if src else []
        return [re.sub(r"\{\{ item(\.src)? \}\}", str(item["src"] if isinstance(item, dict) else item), src)
                for item in task["loop"]]

    for task in TASKS:
        for src in sources(task):
            assert (ROLE / "files" / src).exists(), f"{task['name']}: {src}"


def test_the_package_installs_self_contained_and_runs(tmp_path: Path) -> None:
    # Mirror the role's layout: package under lib/streamstack, launchers in bin.
    import shutil

    shutil.copytree(APP / "src/streamstack", tmp_path / "lib/streamstack/streamstack")
    (tmp_path / "bin").mkdir()
    for name in ("streamstack", "streamstack-auto", "streamstack-tray"):
        shutil.copy2(APP / "packaging/bin" / name, tmp_path / "bin" / name)
    env_file = tmp_path / "streamstack.env"
    env_file.write_text(_env(streamstack_program_label="Studio Cam"))
    env = dict(os.environ, HOME=str(tmp_path), STREAMSTACK_ENV_FILE=str(env_file), NO_COLOR="1")
    env.pop("PYTHONPATH", None)
    version = re.search(r'__version__ = "([^"]+)"', (APP / "src/streamstack/__init__.py").read_text()).group(1)
    run = lambda *args: subprocess.run([sys.executable, "-I", str(tmp_path / "bin/streamstack"), *args],  # noqa: E731
                                       capture_output=True, text=True, env=env, check=False, timeout=60)
    assert run("--version").stdout.strip() == f"streamstack {version}"
    settings = run("call-settings")
    assert settings.returncode == 0, settings.stderr
    assert "Studio Cam" in settings.stdout
    assert run("setup", "--quiet").returncode == 0
    assert (tmp_path / ".config/obs-studio/basic/scenes/StreamStack.json").is_file()


def test_the_settings_file_is_the_one_the_app_reads() -> None:
    task = _task("Render the StreamStack settings")["ansible.builtin.copy"]
    config = (APP / "src/streamstack/config.py").read_text()
    assert task["dest"] == "/etc/streamstack/streamstack.env"
    assert '"/etc/streamstack/streamstack.env"' in config
    assert "/etc/streamstack/streamstack.env" in (ROLE / "files/curiosity-streamstack-loopback").read_text()
    rendered = {line.split("=", 1)[0] for line in _env().splitlines() if "=" in line and not line.startswith("#")}
    known = set(re.findall(r'"(STREAMSTACK_[A-Z_]+)":', config))
    assert rendered <= known, rendered - known
    assert "STREAMSTACK_MICROPHONE=''" in _env()


def test_auto_mode_is_off_until_turned_on_and_installed_for_everyone() -> None:
    assert DEFAULTS["streamstack_auto_autostart"] is False
    assert DEFAULTS["streamstack_auto_backend"] == "claude-cli"
    assert DEFAULTS["streamstack_auto_ai_interval"] >= 60
    assert _task("Leave StreamStack auto mode to `streamstack auto on`")["when"] == \
        "not (streamstack_auto_autostart | bool)"
    unit = (APP / "packaging/systemd/user/streamstack-auto.service").read_text()
    assert "ExecStart=streamstack-auto run" in unit
    env = _env()
    for key in ("BACKEND", "MODEL", "INTERVAL", "AI_INTERVAL", "VIDEO", "AUDIO", "NOTIFY"):
        assert f"STREAMSTACK_AUTO_{key}=" in env
