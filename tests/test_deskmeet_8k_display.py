"""DeskMeet 8K display: patched i915 (RGB 4:4:4 over a PCON), Broadcast RGB and 2x scaling."""

import hashlib
import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import jinja2
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = ROLE / "tasks"
FILES = ROLE / "files"
I915 = FILES / "i915-pcon-frl-444"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
PROFILES = DEFAULTS["hardware_profiles"]
BUILD_SCRIPT = I915 / "i915-pcon-frl-444-build"


def _tasks(name: str) -> list[dict]:
    return yaml.safe_load((TASKS / name).read_text())


def _flatten(tasks: list[dict]) -> list[dict]:
    flat = []
    for task in tasks:
        flat.append(task)
        for key in ("block", "rescue", "always"):
            flat.extend(_flatten(task.get(key, [])))
    return flat


def _named(tasks: list[dict], fragment: str) -> dict:
    return next(task for task in _flatten(tasks) if fragment in task.get("name", ""))


def _derived(variable: str, profiles: dict, hardware_profile: str) -> str:
    from ansible.plugins.filter.core import FilterModule

    env = jinja2.Environment()
    env.filters.update(FilterModule().filters())
    rendered = env.from_string(DEFAULTS[variable]).render(
        hardware_profiles=profiles, hardware_profile=hardware_profile
    )
    return rendered.strip()


# --- hardware profile opt-ins -------------------------------------------------


def test_only_the_deskmeet_b760_profile_opts_in() -> None:
    deskmeet = PROFILES["deskmeet-b760"]
    assert deskmeet["i915_pcon_frl_444"] is True
    assert deskmeet["display_scale"] == 2
    assert deskmeet["broadcast_rgb"] == "Full"
    for name, profile in PROFILES.items():
        if name == "deskmeet-b760":
            continue
        for key in ("i915_pcon_frl_444", "display_scale", "broadcast_rgb"):
            assert key not in profile, f"{name} must not set {key}"


def test_derived_variables_follow_the_profile_and_default_off() -> None:
    assert _derived("i915_pcon_frl_444_enabled", PROFILES, "deskmeet-b760") == "True"
    assert _derived("minimal_desktop_display_scale", PROFILES, "deskmeet-b760") == "2"
    assert _derived("minimal_desktop_display_broadcast_rgb", PROFILES, "deskmeet-b760") == "Full"
    for name in set(PROFILES) - {"deskmeet-b760"} | {"no-such-profile"}:
        assert _derived("i915_pcon_frl_444_enabled", PROFILES, name) == "False", name
        assert _derived("minimal_desktop_display_scale", PROFILES, name) == "1", name
        assert _derived("minimal_desktop_display_broadcast_rgb", PROFILES, name) == "", name


def test_the_i915_tasks_are_included_for_hardware_workstations() -> None:
    include = _named(_tasks("main.yml"), "Include the patched i915 module")
    assert include["ansible.builtin.include_tasks"] == "i915-pcon-frl-444.yml"
    assert include["when"] == "workstation_profile in ['desktop', 'thin-client', 'byod-kiosk']"


# --- build script, hooks and Ansible tasks ------------------------------------


@pytest.mark.parametrize("name", ["i915-pcon-frl-444-build", "zz-i915-pcon-frl-444", "zz-i915-pcon-frl-444.postrm"])
def test_scripts_are_executable_and_valid_shell(name: str) -> None:
    path = I915 / name
    assert os.access(path, os.X_OK)
    shell = "bash" if name.endswith("build") else "sh"
    assert subprocess.run([shell, "-n", str(path)], capture_output=True).returncode == 0
    assert "Managed by Ansible" in path.read_text()
    if shutil.which("shellcheck"):
        result = subprocess.run(["shellcheck", "-s", shell, str(path)], text=True, capture_output=True)
        assert result.returncode == 0, result.stdout


def test_the_patch_touches_only_intel_dp_and_applies_with_p1() -> None:
    patch = (I915 / "i915-pcon-frl-444.patch").read_text()
    files = {line.split()[1] for line in patch.splitlines() if line.startswith(("--- ", "+++ "))}
    assert files == {
        "a/drivers/gpu/drm/i915/display/intel_dp.c",
        "b/drivers/gpu/drm/i915/display/intel_dp.c",
    }


def test_build_script_fails_loud_and_keeps_the_stock_module() -> None:
    script = BUILD_SCRIPT.read_text()
    # Nothing is installed before the patch, build and ABI checks pass.
    assert script.index("--dry-run") < script.index("make -C") < script.index("modversions differ")
    assert script.index("modversions differ") < script.index('mv -f -- "$updates_dir/.i915.ko.new"')
    assert script.index("vermagic differs") < script.index('mv -f -- "$updates_dir/.i915.ko.new"')
    assert "logger -t i915-pcon-frl-444 -p user.err" in script
    assert 'trap \'on_error $? $LINENO\' ERR' in script
    # No debugfs force-DSC and no live reload from the build.
    for forbidden in ("i915_dsc", "debugfs", "rmmod", "modprobe i915", "systemctl", "lightdm"):
        assert forbidden not in script.replace("--dump-modversions", ""), forbidden


def _run_build(*args: str, modules_root: Path, patch: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BUILD_SCRIPT), *args],
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ["PATH"],
            "I915_PCON_MODULES_ROOT": str(modules_root),
            "I915_PCON_PATCH": str(patch),
        },
    )


def test_build_script_check_mode_tracks_the_patch_hash(tmp_path: Path) -> None:
    patch = tmp_path / "i915.patch"
    patch.write_text("one\n")
    updates = tmp_path / "modules/6.1.0-test/updates"
    updates.mkdir(parents=True)
    (updates / "i915.ko").write_bytes(b"module")

    # No marker yet: a build is needed.
    assert _run_build("--check", "6.1.0-test", modules_root=tmp_path / "modules", patch=patch).returncode == 3

    digest = hashlib.sha256(patch.read_bytes()).hexdigest()
    marker = updates / "i915.ko.pcon-frl-444"
    marker.write_text(f"patch_sha256={digest}\nkernel=6.1.0-test\n")
    assert _run_build("--check", "6.1.0-test", modules_root=tmp_path / "modules", patch=patch).returncode == 0

    # A refreshed patch or a missing module needs a rebuild.
    patch.write_text("two\n")
    assert _run_build("--check", "6.1.0-test", modules_root=tmp_path / "modules", patch=patch).returncode == 3
    patch.write_text("one\n")
    (updates / "i915.ko").unlink()
    assert _run_build("--check", "6.1.0-test", modules_root=tmp_path / "modules", patch=patch).returncode == 3


def test_build_script_rejects_bad_input_without_touching_anything(tmp_path: Path) -> None:
    patch = tmp_path / "i915.patch"
    patch.write_text("x\n")
    missing_kernel = _run_build("--destdir", str(tmp_path / "out"), "9.9.9-none", modules_root=tmp_path, patch=patch)
    assert missing_kernel.returncode == 1
    assert "not installed" in missing_kernel.stderr
    bad_name = _run_build("--check", "../etc", modules_root=tmp_path, patch=patch)
    assert bad_name.returncode == 1
    assert "invalid kernel release" in bad_name.stderr
    no_patch = _run_build("--check", "6.1.0-test", modules_root=tmp_path, patch=tmp_path / "absent.patch")
    assert no_patch.returncode == 1
    assert "patch not found" in no_patch.stderr
    assert not (tmp_path / "out").exists()


def test_postinst_hook_never_fails_the_kernel_install() -> None:
    hook = I915 / "zz-i915-pcon-frl-444"
    text = hook.read_text()
    assert text.rstrip().endswith("exit 0")
    assert "systemd-run" in text and "--no-block" in text
    # Without a kernel argument, or without the build script, it is a no-op.
    assert subprocess.run(["sh", str(hook)], capture_output=True).returncode == 0
    postrm = subprocess.run(["sh", str(I915 / "zz-i915-pcon-frl-444.postrm")], capture_output=True)
    assert postrm.returncode == 0


def test_ansible_tasks_install_gate_and_stay_non_fatal() -> None:
    tasks = _tasks("i915-pcon-frl-444.yml")
    block = next(task for task in tasks if task.get("block"))
    assert block["when"] == "i915_pcon_frl_444_enabled | bool"
    flat = _flatten(tasks)
    destinations = {
        task["ansible.builtin.copy"]["dest"]
        for task in flat
        if "ansible.builtin.copy" in task
    }
    assert "/usr/local/share/i915-pcon-frl-444/i915-pcon-frl-444.patch" in destinations
    assert "/etc/kernel/postinst.d/zz-i915-pcon-frl-444" in destinations
    assert "/etc/kernel/postrm.d/zz-i915-pcon-frl-444" in destinations
    loop_dests = _named(tasks, "Install the i915 PCON 4:4:4 build script")["loop"]
    assert {item["dest"] for item in loop_dests} == {"i915-pcon-frl-444-build", "i915-reload"}

    check = _named(tasks, "Check whether the patched i915 is current")
    assert check["changed_when"] is False
    assert check["check_mode"] is False
    build = _named(tasks, "Run the i915 PCON 4:4:4 build")
    assert build["async"] and build["poll"]
    # A build failure is reported, not fatal, and Ansible never reloads i915.
    parent = next(task for task in flat if "rescue" in task)
    assert any("ATTENTION" in str(task) for task in parent["rescue"])
    text = (TASKS / "i915-pcon-frl-444.yml").read_text()
    for forbidden in ("i915-reload \"", "rmmod", "modprobe:", "community.general.modprobe", "reboot:"):
        assert forbidden not in text, forbidden
    # Disabling removes the hooks and the build script.
    removal = _named(tasks, "Remove the i915 PCON 4:4:4 hooks")
    assert removal["when"] == "not (i915_pcon_frl_444_enabled | bool)"
    assert "/etc/kernel/postinst.d/zz-i915-pcon-frl-444" in removal["loop"]


# --- Broadcast RGB ------------------------------------------------------------

DISPLAY_SETUP = FILES / "minimal-desktop/curiosity-display-setup"
XRANDR_QUERY = """\
Screen 0: minimum 16 x 16, current 7680 x 4320, maximum 32767 x 32767
DP-3 connected primary 7680x4320+0+0 (normal left inverted right x axis y axis) 1600mm x 900mm
   7680x4320     30.00*+
HDMI-1 disconnected (normal left inverted right x axis y axis)
"""
XRANDR_PROP = """\
Screen 0: minimum 16 x 16, current 7680 x 4320, maximum 32767 x 32767
DP-3 connected primary 7680x4320+0+0 (normal left inverted right x axis y axis) 1600mm x 900mm
\tBroadcast RGB: Automatic
\t\tsupported: Automatic, Full, Limited 16:235
   7680x4320     30.00*+
HDMI-1 disconnected (normal left inverted right x axis y axis)
\tBroadcast RGB: Automatic
"""
XRANDR_PROP_ABSENT = XRANDR_PROP.replace("\tBroadcast RGB: Automatic\n\t\tsupported: Automatic, Full, Limited 16:235\n", "", 1)


def _run_display_setup(broadcast: str | None, prop: str = XRANDR_PROP) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    with tempfile.TemporaryDirectory(prefix="display-rgb-") as temp_dir:
        root = Path(temp_dir)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (root / "query").write_text(XRANDR_QUERY)
        (root / "prop").write_text(prop)
        fake = bin_dir / "xrandr"
        fake.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--query" ]; then cat "$FAKE_ROOT/query"; exit 0; fi\n'
            'if [ "$1" = "--prop" ]; then cat "$FAKE_ROOT/prop"; exit 0; fi\n'
            'printf "%s\\n" "$*" >>"$FAKE_ROOT/calls"\n'
        )
        fake.chmod(0o755)
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "FAKE_ROOT": str(root),
            "CURIOSITY_DISPLAY_OUTPUTS": str(root / "display-outputs"),
            "CURIOSITY_DISPLAY_BROADCAST_RGB": str(root / "display-broadcast-rgb"),
        }
        (root / "display-outputs").write_text("DP-3\n")
        if broadcast is not None:
            (root / "display-broadcast-rgb").write_text(broadcast)
        result = subprocess.run(["sh", str(DISPLAY_SETUP)], text=True, capture_output=True, env=env)
        calls_file = root / "calls"
        calls = calls_file.read_text().splitlines() if calls_file.exists() else []
    return result, calls


def test_display_setup_sets_broadcast_rgb_full_on_the_chosen_output() -> None:
    result, calls = _run_display_setup("# Managed by Ansible.\nFull\n")
    assert result.returncode == 0, result.stderr
    assert calls == [
        "--output DP-3 --mode 7680x4320 --rate 30.00 --primary",
        "--output DP-3 --set Broadcast RGB Full",
    ]


def test_display_setup_accepts_the_limited_value_with_a_space() -> None:
    result, calls = _run_display_setup("Limited 16:235\n")
    assert result.returncode == 0, result.stderr
    assert calls[-1] == "--output DP-3 --set Broadcast RGB Limited 16:235"


def test_display_setup_leaves_broadcast_rgb_alone_without_config_or_property() -> None:
    result, calls = _run_display_setup(None)
    assert result.returncode == 0 and len(calls) == 1, calls
    result, calls = _run_display_setup("")
    assert result.returncode == 0 and len(calls) == 1, calls
    result, calls = _run_display_setup("Full\n", XRANDR_PROP_ABSENT)
    assert result.returncode == 0 and len(calls) == 1, calls
    assert result.stderr == ""


def test_display_setup_rejects_an_invalid_broadcast_value_without_failing() -> None:
    result, calls = _run_display_setup("Full; reboot\n")
    assert result.returncode == 0
    assert "invalid Broadcast RGB value" in result.stderr
    assert len(calls) == 1


def test_display_setup_tasks_manage_the_broadcast_config() -> None:
    tasks = _tasks("minimal-desktop-display-setup.yml")
    record = _named(tasks, "Record the output range")
    assert record["ansible.builtin.copy"]["dest"] == "/etc/curiosity/display-broadcast-rgb"
    assert "Managed by Ansible" in record["ansible.builtin.copy"]["content"]
    assert "minimal_desktop_display_broadcast_rgb | length > 0" in record["when"]
    assert "minimal_desktop_display_broadcast_rgb | length == 0" in _named(tasks, "Remove the output range")["when"]
    find = _named(tasks, "Find role-managed display setup files")["ansible.builtin.find"]
    assert "display-broadcast-rgb" in find["patterns"]


# --- 2x desktop scaling -------------------------------------------------------


def test_autostart_merges_xresources_before_anything_draws() -> None:
    autostart = (FILES / "minimal-desktop/autostart").read_text()
    assert 'xrdb -merge "$HOME/.Xresources"' in autostart
    assert autostart.index("xrdb -merge") < autostart.index("xsetroot -cursor_name") < autostart.index("tint2 -c")


def test_display_scale_is_wired_per_user() -> None:
    include = next(
        task for task in _tasks("minimal-desktop.yml")
        if task.get("ansible.builtin.include_tasks") == "minimal-desktop-display-scale.yml"
    )
    assert include["loop"] == "{{ dev_users }}"
    assert include["loop_control"]["loop_var"] == "dw_user"
    text = (TASKS / "minimal-desktop-display-scale.yml").read_text()
    assert "dev_user" not in text.replace("dev_users", "")
    assert "dw_user" in text


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")
def test_scaled_desktop_matches_the_hand_made_files_and_is_idempotent(tmp_path: Path) -> None:
    user = os.environ.get("USER") or "root"
    homes = {"top": tmp_path / "top", "bottom": tmp_path / "bottom", "plain": tmp_path / "plain"}
    for home in homes.values():
        home.mkdir()
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        textwrap.dedent(
            f"""\
            - hosts: localhost
              gather_facts: false
              vars:
                minimal_desktop_tint2_layout: bottom
              tasks:
                - ansible.builtin.include_role:
                    name: developer_workstation
                    tasks_from: "{{{{ item.file }}}}"
                  vars:
                    minimal_desktop_display_scale: "{{{{ item.scale }}}}"
                    dw_user: "{{{{ item.user }}}}"
                  loop:
                    - {{file: minimal-desktop-display-scale.yml, scale: 2, user: {{name: {user}, home: {homes['top']}, tint2_layout: top}}}}
                    - {{file: minimal-desktop-tint2.yml, scale: 2, user: {{name: {user}, home: {homes['top']}, tint2_layout: top}}}}
                    - {{file: minimal-desktop-display-scale.yml, scale: 2, user: {{name: {user}, home: {homes['bottom']}}}}}
                    - {{file: minimal-desktop-tint2.yml, scale: 2, user: {{name: {user}, home: {homes['bottom']}}}}}
                    - {{file: minimal-desktop-display-scale.yml, scale: 1, user: {{name: {user}, home: {homes['plain']}}}}}
                    - {{file: minimal-desktop-tint2.yml, scale: 1, user: {{name: {user}, home: {homes['plain']}}}}}
            """
        )
    )
    env = {**os.environ, "ANSIBLE_ROLES_PATH": str(ROOT / "roles"), "ANSIBLE_LOCALHOST_WARNING": "False"}

    def converge() -> str:
        result = subprocess.run(
            [shutil.which("ansible-playbook"), "-i", "localhost,", str(playbook)],
            text=True,
            capture_output=True,
            env=env,
            cwd=tmp_path,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    converge()
    assert "changed=0" in converge()

    xresources = (homes["top"] / ".Xresources").read_text().splitlines()
    assert xresources[1:] == [
        "Xft.dpi: 192",
        "Xcursor.size: 48",
        "Xft.antialias: 1",
        "Xft.hinting: 1",
        "Xft.hintstyle: hintslight",
        "Xft.rgba: none",
        "Xft.lcdfilter: lcddefault",
    ]
    assert "Managed by Ansible" in xresources[0]

    def options(path: Path) -> dict[str, str]:
        found: dict[str, str] = {}
        for line in path.read_text().splitlines():
            if " = " in line and not line.startswith("#"):
                key, value = line.split(" = ", 1)
                found.setdefault(key, value)
        return found

    layouts = ROLE / "files/minimal-desktop/tint2"
    top = options(homes["top"] / ".config/tint2/tint2rc")
    assert top["panel_size"] == "100% 72" and top["launcher_icon_size"] == "50"
    bottom = options(homes["bottom"] / ".config/tint2/tint2rc")
    assert bottom["panel_size"] == "100% 64" and "launcher_icon_size" not in bottom

    # Everything else in the layout is untouched, and scale 1 changes nothing.
    for home, layout in ((homes["top"], "top"), (homes["bottom"], "bottom")):
        original = (layouts / f"{layout}.tint2rc").read_text().splitlines()
        scaled = (home / ".config/tint2/tint2rc").read_text().splitlines()
        assert len(scaled) == len(original)
        changed = [(a, b) for a, b in zip(original, scaled) if a != b]
        assert all(a.startswith(("panel_size", "launcher_icon_size")) for a, _ in changed)
    assert (homes["plain"] / ".config/tint2/tint2rc").read_bytes() == (layouts / "bottom.tint2rc").read_bytes()
    assert not (homes["plain"] / ".Xresources").exists()
