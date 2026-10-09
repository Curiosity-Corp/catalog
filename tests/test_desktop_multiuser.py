"""Regression checks for multi-user desktop convergence, LightDM, pinned VTs, and display setup."""

import os
import re
import subprocess
import tempfile
from pathlib import Path

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = ROLE / "tasks"
FILES = ROLE / "files/minimal-desktop"

# Task files that run once per item of dev_users. They must address the account
# only through dw_user: ansible-pull passes dev_user as an extra var, which beats
# every other precedence level and would collapse every iteration onto it.
PER_USER_FILES = (
    "user-preflight.yml",
    "user-home-ownership.yml",
    "user-systemd.yml",
    "minimal-desktop-openbox.yml",
    "minimal-desktop-picom.yml",
    "minimal-desktop-tint2.yml",
    "minimal-desktop-dunst.yml",
    "minimal-desktop-rofi.yml",
    "minimal-desktop-gtk-theme.yml",
    "minimal-desktop-firefox.yml",
    "minimal-desktop-locking-user.yml",
)
PRIMARY_USER_REFERENCE = re.compile(r"\bdev_user(_home)?\b")


def _tasks(name: str) -> list[dict]:
    return yaml.safe_load((TASKS / name).read_text())


def _named(tasks: list[dict], fragment: str) -> dict:
    return next(task for task in tasks if fragment in task.get("name", ""))


def _includes(tasks: list[dict]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for task in tasks:
        include = task.get("ansible.builtin.include_tasks")
        if include is None:
            continue
        file_name = include["file"] if isinstance(include, dict) else include
        found[file_name] = task
    return found


def test_dev_users_defaults_to_the_primary_account() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["dev_users"] == [
        {"name": "{{ dev_user }}", "home": "{{ dev_user_home }}"}
    ]
    assert defaults["workstation_user_linger"] == []


def test_per_user_files_never_reference_the_primary_account() -> None:
    for name in PER_USER_FILES:
        text = (TASKS / name).read_text()
        assert not PRIMARY_USER_REFERENCE.search(text), f"{name} references dev_user"
        assert "dw_user" in text, f"{name} does not use dw_user"


def test_per_user_files_are_looped_over_dev_users_with_dw_user() -> None:
    included: dict[str, dict] = {}
    for caller in ("main.yml", "minimal-desktop.yml", "minimal-desktop-locking.yml"):
        included.update(_includes(_tasks(caller)))
    for name in PER_USER_FILES:
        task = included[name]
        assert task["loop"] == "{{ dev_users }}", name
        assert task["loop_control"]["loop_var"] == "dw_user", name


def test_dev_users_does_not_restrict_account_name_format() -> None:
    # Directory-backed accounts (for example first.last or user@domain) are valid
    # primary accounts on hosts that never set dev_users.
    validate = _named(_tasks("main.yml"), "Validate the managed user list")
    assert not any("match" in check for check in validate["ansible.builtin.assert"]["that"]
                   if "attribute='name'" in check)


def test_primary_account_must_be_listed_in_dev_users() -> None:
    validate = _named(_tasks("main.yml"), "Validate the managed user list")
    assert "dev_user in (dev_users | map(attribute='name') | list)" in validate[
        "ansible.builtin.assert"
    ]["that"]


def test_linger_is_opt_in_and_idempotent() -> None:
    main = _named(_tasks("main.yml"), "Enable systemd lingering")
    assert main["when"] == "workstation_user_linger | length > 0"
    tasks = _tasks("user-linger.yml")
    enable = _named(tasks, "Enable lingering")
    assert enable["when"] == "not item.stat.exists"
    assert enable["ansible.builtin.command"]["argv"][:2] == ["loginctl", "enable-linger"]
    assert all("failed_when" not in task for task in tasks)


def test_lightdm_greeter_vt_wrapper_and_service() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["minimal_desktop_greeter"] == "gtk"
    assert defaults["minimal_desktop_lightdm_minimum_vt"] == 8
    assert defaults["minimal_desktop_lightdm_vtswitch_wrapper"] is False
    assert defaults["minimal_desktop_lightdm_remove_dropins"] == []

    tasks = _tasks("minimal-desktop-lightdm.yml")
    assert _named(tasks, "Configure LightDM GTK greeter")["when"] == (
        "minimal_desktop_greeter == 'gtk'"
    )
    slick = _named(tasks, "Configure LightDM slick greeter")
    assert slick["ansible.builtin.copy"]["dest"] == "/etc/lightdm/slick-greeter.conf"
    assert _named(tasks, "Install the slick greeter")["ansible.builtin.apt"]["name"] == (
        "slick-greeter"
    )
    assert "user-session=openbox" in _named(tasks, "Openbox session")[
        "ansible.builtin.copy"
    ]["content"]
    assert "minimum-vt={{ minimal_desktop_lightdm_minimum_vt | int }}" in _named(
        tasks, "VT, leaving lower VTs"
    )["ansible.builtin.copy"]["content"]
    assert "xserver-command=/usr/local/bin/lightdm-xserver-wrapper -core" in _named(
        tasks, "Point LightDM at the X server wrapper"
    )["ansible.builtin.copy"]["content"]
    service = tasks[-1]["ansible.builtin.systemd_service"]
    assert service == {"name": "lightdm", "enabled": True, "state": "started"}
    assert all("failed_when" not in task for task in tasks)

    # A hand-installed wrapper at the same path must survive a default pull.
    find = _named(tasks, "Find a role-managed LightDM X server wrapper")["ansible.builtin.find"]
    assert find["contains"] == "Managed by Ansible"
    assert "Managed by Ansible" in (FILES / "lightdm-xserver-wrapper").read_text()
    assert not any(
        task.get("ansible.builtin.file", {}).get("path") == "/usr/local/bin/lightdm-xserver-wrapper"
        for task in tasks
    )


def test_lightdm_xserver_wrapper_strips_only_novtswitch() -> None:
    script = (FILES / "lightdm-xserver-wrapper").read_text()
    assert subprocess.run(["sh", "-n"], input=script, text=True).returncode == 0
    with tempfile.TemporaryDirectory(prefix="lightdm-wrapper-") as temp_dir:
        fake_x = Path(temp_dir) / "X"
        fake_x.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        fake_x.chmod(0o755)
        wrapper = Path(temp_dir) / "wrapper"
        wrapper.write_text(script.replace("/usr/bin/X", str(fake_x)))
        wrapper.chmod(0o755)
        result = subprocess.run(
            [str(wrapper), "-core", ":0", "-novtswitch", "vt8", "-seat", "seat0"],
            text=True,
            capture_output=True,
            check=True,
        )
    assert result.stdout.split() == ["-core", ":0", "vt8", "-seat", "seat0"]


def _render_pinned_profile(sessions: list[dict]) -> str:
    template = (ROLE / "templates/curiosity-pinned-x.sh.j2").read_text()
    environment = jinja2.Environment(trim_blocks=True, keep_trailing_newline=True)
    return environment.from_string(template).render(
        minimal_desktop_pinned_vt_sessions=sessions
    )


def _run_pinned_profile(profile: str, *, user: str, tty: str, env: dict[str, str]) -> str:
    with tempfile.TemporaryDirectory(prefix="pinned-vt-") as temp_dir:
        bin_dir = Path(temp_dir) / "bin"
        bin_dir.mkdir()
        for name, body in {
            "tty": f'echo "{tty}"',
            "id": f'echo "{user}"',
            "startx": 'echo "startx $*"',
        }.items():
            command = bin_dir / name
            command.write_text(f"#!/bin/sh\n{body}\n")
            command.chmod(0o755)
        profile_path = Path(temp_dir) / "profile.sh"
        profile_path.write_text(profile)
        result = subprocess.run(
            ["sh", "-c", f'. "{profile_path}"; echo fell-through'],
            text=True,
            capture_output=True,
            check=True,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin", **env},
        )
    return result.stdout


def test_pinned_vt_profile_hook_only_fires_for_the_pinned_user_on_their_vt() -> None:
    profile = _render_pinned_profile(
        [{"user": "user-a", "vt": 9}, {"user": "user-b", "vt": 7}]
    )
    assert subprocess.run(["sh", "-n"], input=profile, text=True).returncode == 0

    started = _run_pinned_profile(profile, user="user-a", tty="/dev/tty9", env={})
    assert started == "startx /usr/bin/openbox-session -- vt9 -keeptty\n"
    other_vt = _run_pinned_profile(profile, user="user-a", tty="/dev/tty3", env={})
    assert other_vt == "fell-through\n"
    other_user = _run_pinned_profile(profile, user="user-b", tty="/dev/tty9", env={})
    assert other_user == "fell-through\n"
    second_user = _run_pinned_profile(profile, user="user-b", tty="/dev/tty7", env={})
    assert second_user == "startx /usr/bin/openbox-session -- vt7 -keeptty\n"
    in_x = _run_pinned_profile(profile, user="user-a", tty="/dev/tty9", env={"DISPLAY": ":0"})
    assert in_x == "fell-through\n"
    over_ssh = _run_pinned_profile(
        profile, user="user-a", tty="/dev/tty9", env={"SSH_CONNECTION": "a b c d"}
    )
    assert over_ssh == "fell-through\n"


def test_pinned_vt_tasks_enforce_vt_ordering_and_user_membership() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["minimal_desktop_pinned_vt_sessions"] == []

    tasks = _tasks("minimal-desktop-pinned-vt.yml")
    checks = " ".join(_named(tasks, "Validate pinned VT sessions")["ansible.builtin.assert"]["that"])
    assert "select('lt', minimal_desktop_lightdm_minimum_vt | int)" in checks
    assert "difference(dev_users | map(attribute='name') | list)" in checks
    assert _named(tasks, "Install xinit")["ansible.builtin.apt"]["name"] == "xinit"
    dropin = _named(tasks, "Prefill the pinned user name")["ansible.builtin.copy"]
    assert "getty@tty{{ item.vt | int }}.service.d" in dropin["dest"]
    assert "--login-options '-p -- {{ item.user }}'" in dropin["content"]
    assert "--autologin" not in dropin["content"]
    assert "ExecStart=-/usr/sbin/agetty " in dropin["content"]
    enable = _named(tasks, "Enable getty on the pinned VTs")["ansible.builtin.systemd_service"]
    assert enable["name"] == "getty@tty{{ item.vt | int }}.service"
    assert enable["enabled"] is True
    assert all("failed_when" not in task for task in tasks)

    desktop = [task.get("name", "") for task in _tasks("minimal-desktop.yml")]
    assert desktop.index("Deploy Openbox configuration") < desktop.index(
        "Configure pinned per-user VT sessions"
    )


XRANDR_SAMPLE = """\
Screen 0: minimum 320 x 200, current 3840 x 1080, maximum 16384 x 16384
eDP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 344mm x 193mm
   1920x1080     60.01*+  59.97    59.96    48.01
   1600x900      60.00
HDMI-1 connected 1920x1080+1920+0 (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080     60.00*+  50.00
   1920x1080i    60.00    50.00
DP-1 connected (normal left inverted right x axis y axis)
   3840x2160     60.00 +  30.00
   2560x1440    144.00    59.95
   1920x1080    240.00
DP-2 disconnected (normal left inverted right x axis y axis)
"""


def _run_display_setup(
    outputs: str | None, xrandr_query: str = XRANDR_SAMPLE
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    with tempfile.TemporaryDirectory(prefix="display-setup-") as temp_dir:
        root = Path(temp_dir)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (root / "query").write_text(xrandr_query)
        fake = bin_dir / "xrandr"
        fake.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = "--query" ]; then cat "$FAKE_ROOT/query"; exit 0; fi\n'
            'printf "%s\\n" "$*" >>"$FAKE_ROOT/calls"\n'
        )
        fake.chmod(0o755)
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "FAKE_ROOT": str(root),
            "CURIOSITY_DISPLAY_OUTPUTS": str(root / "display-outputs"),
        }
        if outputs is not None:
            (root / "display-outputs").write_text(outputs)
        result = subprocess.run(
            ["sh", str(FILES / "curiosity-display-setup")],
            text=True,
            capture_output=True,
            env=env,
        )
        calls_file = root / "calls"
        calls = calls_file.read_text().splitlines() if calls_file.exists() else []
    return result, calls


def test_display_setup_is_valid_posix_shell() -> None:
    script = (FILES / "curiosity-display-setup").read_text()
    assert subprocess.run(["sh", "-n"], input=script, text=True).returncode == 0
    assert os.access(FILES / "curiosity-display-setup", os.X_OK)


def test_display_setup_prefers_listed_output_at_best_mode_and_turns_others_off() -> None:
    result, calls = _run_display_setup("# preferred first\nDP-2\nDP-1\nHDMI-1\n")
    assert result.returncode == 0, result.stderr
    assert calls == [
        "--output DP-1 --mode 3840x2160 --rate 60.00 --primary "
        "--output eDP-1 --off --output HDMI-1 --off"
    ]


def test_display_setup_picks_highest_refresh_for_the_largest_mode() -> None:
    query = (
        "eDP-1 connected primary 1920x1080+0+0 (normal) 344mm x 193mm\n"
        "   1920x1080     60.01*+  144.00 +  59.97\n"
        "   1920x1080i   200.00\n"
        "   1280x720     240.00\n"
    )
    result, calls = _run_display_setup(None, query)
    assert result.returncode == 0, result.stderr
    assert calls == ["--output eDP-1 --mode 1920x1080 --rate 144.00 --primary"]


def test_display_setup_without_a_match_keeps_other_outputs_on() -> None:
    for outputs in (None, "", "# nothing\nDP-9\n"):
        result, calls = _run_display_setup(outputs)
        assert result.returncode == 0, result.stderr
        assert calls == ["--output eDP-1 --mode 1920x1080 --rate 60.01 --primary"]


def test_display_setup_never_blocks_login_but_reports_problems() -> None:
    # LightDM stops the display when its display-setup-script fails.
    with tempfile.TemporaryDirectory(prefix="display-setup-") as temp_dir:
        bin_dir = Path(temp_dir) / "bin"
        bin_dir.mkdir()
        broken = bin_dir / "xrandr"
        broken.write_text("#!/bin/sh\necho 'Can not open display' >&2\nexit 1\n")
        broken.chmod(0o755)
        result = subprocess.run(
            ["sh", str(FILES / "curiosity-display-setup")],
            text=True,
            capture_output=True,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        )
    assert result.returncode == 0
    assert "cannot query the display" in result.stderr

    result, calls = _run_display_setup("DP-1\n", "DP-1 disconnected (normal)\n")
    assert result.returncode == 0
    assert calls == []
    assert "no connected output" in result.stderr

    result, calls = _run_display_setup("bad name;rm\nDP-1\n")
    assert result.returncode == 0
    assert "ignoring invalid output name" in result.stderr
    assert calls and calls[0].startswith("--output DP-1 ")


def test_display_setup_hooks_and_defaults() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["minimal_desktop_display_setup_enabled"] is False
    assert defaults["minimal_desktop_display_outputs"] == []

    tasks = _tasks("minimal-desktop-display-setup.yml")
    hook = _named(tasks, "Run the display setup helper before the LightDM greeter")
    assert "display-setup-script=/usr/local/bin/curiosity-display-setup" in hook[
        "ansible.builtin.copy"
    ]["content"]
    assert hook["when"] == "minimal_desktop_display_setup_enabled | bool"
    # Disabling removes only the role's own hook and marker-carrying files, so
    # a hand-installed helper at the same path survives a default pull.
    assert _named(tasks, "Remove the display setup LightDM hook")["when"] == (
        "not (minimal_desktop_display_setup_enabled | bool)"
    )
    find = _named(tasks, "Find role-managed display setup files")["ansible.builtin.find"]
    assert find["contains"] == "Managed by Ansible"
    assert "Managed by Ansible" in (FILES / "curiosity-display-setup").read_text()
    assert not any(
        task.get("ansible.builtin.file", {}).get("path") in (
            "/usr/local/bin/curiosity-display-setup", "/etc/curiosity/display-outputs"
        )
        for task in tasks
    )
    assert all("failed_when" not in task for task in tasks)

    autostart = (FILES / "autostart").read_text()
    assert "command -v curiosity-display-setup" in autostart
    assert autostart.index("curiosity-display-setup") < autostart.index("picom --config")
