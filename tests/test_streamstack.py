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
    task = _task("Seed StreamStack defaults for each user")
    assert task["ansible.builtin.command"]["argv"] == ["/usr/local/bin/streamstack", "setup", "--quiet"]
    assert task["become_user"] == "{{ item.item.name }}"
    assert "--force" not in task["ansible.builtin.command"]["argv"]


def test_panel_menu_is_installed_and_the_tray_icon_is_opt_in() -> None:
    assert DEFAULTS["streamstack_tray_autostart"] is False
    menu = _task("List StreamStack in the application menu")["ansible.builtin.copy"]
    assert menu["src"] == "streamstack/packaging/applications/streamstack.desktop"
    entry = (ROLE / "files" / menu["src"]).read_text()
    assert "Exec=streamstack-console" in entry  # the Console; the old menu stays as a desktop action
    assert "Exec=streamstack-tray --menu" in entry
    autostart = _task("Start the StreamStack panel icon at login")
    assert autostart["when"] == "streamstack_tray_autostart | bool"
    assert (ROLE / "files" / autostart["ansible.builtin.copy"]["src"]).is_file()
    for package in ("python3-gi", "gir1.2-ayatanaappindicator3-0.1"):
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
    for name in ("streamstack", "streamstack-auto", "streamstack-console", "streamstack-tray"):
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
    assert not (tmp_path / ".config/obs-studio").exists(), "StreamStack no longer seeds OBS"


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


def test_lights_follow_the_camera_at_login_for_every_desktop_user() -> None:
    assert DEFAULTS["streamstack_lights_autostart"] is True
    unit = (APP / "src/streamstack/data/systemd/streamstack-lights.service").read_text()
    assert "ExecStart=@STREAMSTACK@ lights run" in unit
    assert "WantedBy=graphical-session.target" in unit
    install = _task("Install the StreamStack lights user service")["ansible.builtin.copy"]
    assert install["dest"] == "/etc/systemd/user/streamstack-lights.service"
    assert "replace('@STREAMSTACK@', '/usr/local/bin/streamstack')" in install["content"]
    link = _task("Start StreamStack camera follow and the light schedule at login")
    assert link["ansible.builtin.file"]["src"] == "/etc/systemd/user/streamstack-lights.service"
    assert link["when"] == "streamstack_lights_autostart | bool"
    wants = _task("Ensure the global user graphical-session.target.wants directory exists")
    assert "streamstack_lights_autostart" in wants["when"]
    assert _task("Leave StreamStack camera follow to `streamstack lights enable`")["when"] == \
        "not (streamstack_lights_autostart | bool)"


def _location_ok(**values) -> bool:
    checks = _task("Validate the StreamStack settings")["ansible.builtin.assert"]["that"]
    rules = [c for c in checks if "streamstack_latitude" in c or "streamstack_longitude" in c or "streamstack_property" in c]
    assert len(rules) == 4
    return all(_render("{{ " + rule + " }}", **values).strip() == "True" for rule in rules)


def test_the_property_location_reaches_the_settings_and_is_validated() -> None:
    assert DEFAULTS["streamstack_latitude"] == DEFAULTS["streamstack_longitude"] == ""
    env = _env(streamstack_property="Main office", streamstack_latitude="40.7128", streamstack_longitude="-74.006")
    assert "STREAMSTACK_PROPERTY='Main office'" in env
    assert "STREAMSTACK_LATITUDE=40.7128" in env
    assert "STREAMSTACK_LONGITUDE=-74.006" in env
    assert "STREAMSTACK_LATITUDE=\n" in _env()
    assert _location_ok()
    assert _location_ok(streamstack_latitude="40.7128", streamstack_longitude="-74.006")
    assert _location_ok(streamstack_latitude=-33.9, streamstack_longitude=151.2)
    assert not _location_ok(streamstack_latitude="40.7")  # half a location
    assert not _location_ok(streamstack_latitude="91", streamstack_longitude="0")
    assert not _location_ok(streamstack_latitude="0", streamstack_longitude="-181")
    assert not _location_ok(streamstack_latitude="40N", streamstack_longitude="74W")
    assert not _location_ok(streamstack_property="Bob's place")


def test_call_users_join_the_video_group_without_creating_accounts() -> None:
    assert DEFAULTS["streamstack_video_group_users"] == ["mrh", "seantech"]
    task = _task("Let the call users open the StreamStack cameras")
    user = task["ansible.builtin.user"]
    assert user["groups"] == "video"
    assert user["append"] is True, "append keeps every other group of the account"
    # Only accounts that exist: ansible.builtin.user would create the others.
    assert "select('in', getent_passwd.keys())" in task["loop"]
    lookup = _task("Look up the accounts on this host")["ansible.builtin.getent"]
    assert lookup["database"] == "passwd"


def test_obs_and_easy_effects_are_retired_not_installed() -> None:
    for retired in ("obs-studio", "easyeffects", "obs-backgroundremoval"):
        assert retired in DEFAULTS["streamstack_retired_packages"]
        assert retired not in DEFAULTS["streamstack_packages"]
    for package in ("lsp-plugins-lv2", "calf-plugins", "python3-websocket"):
        assert package not in DEFAULTS["streamstack_packages"]
    assert "zam-plugins" in DEFAULTS["streamstack_packages"]  # the voice chain's gate, de-esser, compressor, limiter
    removal = _task("Remove OBS Studio, Easy Effects and the OBS background plugin")["ansible.builtin.apt"]
    assert removal["state"] == "absent"
    assert removal["name"] == "{{ streamstack_retired_packages }}"
    cleanup = _task("Remove the OBS background plugin links and the Easy Effects autostart")
    assert "/etc/xdg/autostart/streamstack-easyeffects.desktop" in cleanup["loop"]
    assert not any("easyeffects.desktop" in str(t.get("ansible.builtin.copy", "")) for t in TASKS)
    assert not any("streamstack_easyeffects_autostart" in str(t) for t in TASKS)
    assert not any(key.startswith("streamstack_easyeffects") for key in DEFAULTS)


def test_the_console_runs_headless_for_the_studio_account_only() -> None:
    assert DEFAULTS["streamstack_console_users"] == ["mrh"]
    unit = _environment().from_string((ROLE / "templates/streamstack-console.service.j2").read_text()).render(
        **{k: v for k, v in DEFAULTS.items() if k.startswith("streamstack_")}
    )
    assert "ExecStart=/usr/local/bin/streamstack console --no-window" in unit
    assert "ConditionUser=|mrh" in unit
    assert "WantedBy=graphical-session.target" in unit
    assert "/dev/video43" in unit
    link = _task("Start the StreamStack Console (video engine and voice chain) at login")
    assert link["ansible.builtin.file"]["src"] == "/etc/systemd/user/streamstack-console.service"
    assert link["when"] == "streamstack_console_users | length > 0"
    assert _task("Leave the StreamStack Console to `streamstack console`")["when"] == \
        "streamstack_console_users | length == 0"
    # the headless Console must be something the installed app can do
    assert '"--no-window"' in (APP / "src/streamstack/cli.py").read_text()


def test_the_voice_chain_is_not_enabled_beside_the_console() -> None:
    # The Console starts the chain host itself; a second supervisor would restart it against the first.
    install = _task("Install the StreamStack voice chain and deck user services")
    assert install["loop"] == ["streamstack-audio-chain", "streamstack-deck"]
    assert not any("wants" in str(t.get("ansible.builtin.file", {}).get("path", "")) and "audio-chain" in str(t) for t in TASKS)
    for item in install["loop"]:
        assert (APP / f"packaging/systemd/user/{item}.service").is_file()


def test_the_processed_voice_becomes_the_default_microphone_and_the_sink_is_left_alone() -> None:
    task = _task("Make StreamStack Mic the default microphone for each Console account")
    script = task["ansible.builtin.shell"]["cmd"]
    assert "pactl set-default-source streamstack_mic" in script
    assert "set-default-sink" not in script
    assert 'STREAMSTACK_STREAM_AUDIO": "streamstack_mic"' in (APP / "src/streamstack/config.py").read_text()
