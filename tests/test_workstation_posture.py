"""Regression checks for the minimal desktop and profile transitions."""

import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = ROLE / "tasks"


def _tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text())


def _named(tasks: list[dict], fragment: str) -> dict:
    return next(task for task in tasks if fragment in task.get("name", ""))


def _run_autostart_harness(
    *,
    initial_state: str,
    initial_status: str,
    focuspass_responsive: bool,
    rollback_status: str | None = None,
    focuspass_registers: bool = True,
    focuspass_exit_delay: bool = False,
    focuspass_exit_never: bool = False,
    focuspass_exit_refuses: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """Exercise the screen-lock handoff with fake process and X11 commands."""

    autostart = (ROLE / "files/minimal-desktop/autostart").read_text()
    with tempfile.TemporaryDirectory(prefix="focuspass-autostart-") as temp_dir:
        root = Path(temp_dir)
        bin_dir = root / "bin"
        proc_dir = root / "proc"
        bin_dir.mkdir()
        for pid, display in (("111", ":test"), ("222", ":other"), ("123", ":test")):
            (proc_dir / pid).mkdir(parents=True)
            (proc_dir / pid / "environ").write_bytes(f"DISPLAY={display}\0".encode())
            (proc_dir / pid / "exe").touch()
        (root / "state").write_text(initial_state)
        (root / "log").write_text("")

        scripts = {
            "pgrep": r'''#!/bin/sh
case "$*" in
  *tint2*) exit 1 ;;
esac
state=$(cat "$HARNESS_ROOT/state")
case "$state" in
  stock) echo 111 ;;
  other) echo 222 ;;
  focuspass|focuspass-failed) echo 123 ;;
  focuspass-stopping)
    if [ "$HARNESS_FOCUSPASS_EXIT_NEVER" = 1 ]; then
      echo 123
    else
      polls=$(cat "$HARNESS_ROOT/exit-polls")
      if [ "$polls" -ge 2 ]; then
        printf '%s' none >"$HARNESS_ROOT/state"
      else
        printf '%s' "$((polls + 1))" >"$HARNESS_ROOT/exit-polls"
        echo 123
      fi
    fi
    ;;
esac
''',
            "readlink": r'''#!/bin/sh
if [ "$1" = -f ]; then
  case "$2" in
    "$HARNESS_PROC"/111/exe|"$HARNESS_PROC"/222/exe)
      echo /usr/bin/xscreensaver
      exit 0
      ;;
    "$HARNESS_PROC"/123/exe)
      echo /opt/focuspass-screenlock/bin/xscreensaver
      exit 0
      ;;
    /opt/focuspass-screenlock/bin/xscreensaver)
      echo /opt/focuspass-screenlock/bin/xscreensaver
      exit 0
      ;;
  esac
fi
exec /usr/bin/readlink "$@"
''',
            "stat": r'''#!/bin/sh
if [ "$1" = -Lc ]; then
  case "$3" in
    /opt/focuspass-screenlock/bin/xscreensaver|"$HARNESS_PROC"/123/exe)
      echo 1:99
      exit 0
      ;;
  esac
fi
exec /usr/bin/stat "$@"
''',
            "xscreensaver-command": r'''#!/bin/sh
state=$(cat "$HARNESS_ROOT/state")
case "$1" in
  -time)
    status="$HARNESS_INITIAL_STATUS"
    case "$state" in
      focuspass|focuspass-failed|focuspass-stopping|none) status="${HARNESS_ROLLBACK_STATUS:-$status}" ;;
    esac
    case "$status" in
      locked) echo 'XScreenSaver 6.08: screen locked since now'; exit 0 ;;
      unlocked) echo 'XScreenSaver 6.08: screen unblanked since now'; exit 0 ;;
      error) exit 1 ;;
    esac
    ;;
  --exit)
    printf '%s\n' xscreensaver-exit >>"$HARNESS_ROOT/log"
    printf '%s' none >"$HARNESS_ROOT/state"
    exit 0
    ;;
esac
exit 1
''',
            "focuspass-screenlock-command": r'''#!/bin/sh
state=$(cat "$HARNESS_ROOT/state")
case "$1" in
  -time)
    if [ "$HARNESS_FOCUSPASS_RESPONSIVE" = 1 ] && [ "$state" = focuspass ]; then
      echo 'XScreenSaver 6.08: screen unblanked since now'
      exit 0
    fi
    exit 1
    ;;
  --exit)
    printf '%s\n' focuspass-exit >>"$HARNESS_ROOT/log"
    if [ "$HARNESS_FOCUSPASS_EXIT_REFUSES" = 1 ]; then
      exit 1
    fi
    if [ "$HARNESS_FOCUSPASS_EXIT_DELAY" = 1 ] || [ "$HARNESS_FOCUSPASS_EXIT_NEVER" = 1 ]; then
      printf '%s' focuspass-stopping >"$HARNESS_ROOT/state"
      printf '%s' 0 >"$HARNESS_ROOT/exit-polls"
    else
      printf '%s' none >"$HARNESS_ROOT/state"
    fi
    exit 0
    ;;
esac
exit 1
''',
            "focuspass-screenlock": r'''#!/bin/sh
printf '%s\n' focuspass-start >>"$HARNESS_ROOT/log"
if [ "$HARNESS_FOCUSPASS_REGISTERS" = 1 ]; then
  printf '%s' focuspass >"$HARNESS_ROOT/state"
fi
sleep 0.1
''',
            "xscreensaver": r'''#!/bin/sh
printf '%s\n' stock-start >>"$HARNESS_ROOT/log"
printf '%s' stock >"$HARNESS_ROOT/state"
sleep 0.1
''',
            "process-alive": r'''#!/bin/sh
pid=$1
case "$(cat "$HARNESS_ROOT/state"):$pid" in
  stock:111|other:222|focuspass:123|focuspass-failed:123|focuspass-stopping:123)
    exit 0
    ;;
esac
exit 1
''',
        }
        for name in (
            "xsetroot",
            "feh",
            "picom",
            "tint2",
            "dunst",
            "nm-applet",
            "pasystray",
            "blueman-applet",
            "flameshot",
        ):
            scripts[name] = "#!/bin/sh\nexit 0\n"
        for name, content in scripts.items():
            script = bin_dir / name
            script.write_text(content)
            script.chmod(0o755)

        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{bin_dir}:{environment['PATH']}",
                "DISPLAY": ":test",
                "FOCUSPASS_PROC_ROOT": str(proc_dir),
                "FOCUSPASS_PROCESS_ALIVE_HELPER": str(bin_dir / "process-alive"),
                "HARNESS_ROOT": str(root),
                "HARNESS_PROC": str(proc_dir),
                "HARNESS_INITIAL_STATUS": initial_status,
                "HARNESS_FOCUSPASS_RESPONSIVE": "1" if focuspass_responsive else "0",
                "HARNESS_FOCUSPASS_REGISTERS": "1" if focuspass_registers else "0",
                "HARNESS_FOCUSPASS_EXIT_DELAY": "1" if focuspass_exit_delay else "0",
                "HARNESS_FOCUSPASS_EXIT_NEVER": "1" if focuspass_exit_never else "0",
                "HARNESS_FOCUSPASS_EXIT_REFUSES": "1" if focuspass_exit_refuses else "0",
                "HARNESS_ROLLBACK_STATUS": rollback_status or "",
            }
        )
        result = subprocess.run(
            ["sh", "-c", autostart],
            text=True,
            capture_output=True,
            env=environment,
            timeout=15,
        )
        return result, (root / "state").read_text(), (root / "log").read_text()


def test_openbox_uses_pam_aware_locker_and_keeps_tty_locking() -> None:
    packages = _tasks(TASKS / "minimal-desktop-packages.yml")
    install = _named(packages, "PAM-aware locking tools")
    assert set(install["ansible.builtin.apt"]["name"]) == {"xscreensaver", "vlock"}
    assert install["when"] == "workstation_profile != 'byod-kiosk'"

    remove_i3lock = _named(packages, "Remove the single-prompt i3lock")
    assert set(remove_i3lock["ansible.builtin.apt"]["name"]) == {"i3lock", "xss-lock"}

    lock_tasks = _tasks(TASKS / "minimal-desktop-locking.yml") + _tasks(
        TASKS / "minimal-desktop-locking-user.yml"
    )
    config = _named(lock_tasks, "Configure XScreenSaver to blank")[
        "ansible.builtin.blockinfile"
    ]["block"]
    assert "// 60" in config and "% 60" in config
    assert "lock: True" in config and "mode: blank" in config
    pam = _named(lock_tasks, "host login authentication prompts")[
        "ansible.builtin.copy"
    ]["content"]
    assert "auth include login" in pam
    assert "account include" not in pam
    assert "session include" not in pam
    assert "This role will not change account passwords." in "\n".join(
        task.get("ansible.builtin.assert", {}).get("fail_msg", "")
        for task in lock_tasks
    )
    docs = (ROOT / "docs/configuration.md").read_text()
    assert "must be enrolled before automatic locking is enabled" in docs
    assert "does not reliably enforce PAM account-expiry" in docs

    autostart = (ROLE / "files/minimal-desktop/autostart").read_text()
    assert "display_xscreensaver_pids()" in autostart
    assert 'tr \'\\000\' \'\\n\' <"${FOCUSPASS_PROC_ROOT:-/proc}/$pid/environ"' in autostart
    assert 'focuspass_path=/opt/focuspass-screenlock/bin/xscreensaver' in autostart
    assert '"screen non-blanked since"' in autostart
    assert "active or unknown; leaving it in place" in autostart
    assert "xscreensaver-command --exit" in autostart
    assert "did not exit; leaving it untouched" in autostart
    assert "focuspass_command_responds" in autostart
    assert "focuspass_is_healthy" in autostart
    assert "focuspass_path" in autostart
    assert "process_identity" in autostart
    assert "FocusPass did not become responsive" in autostart
    assert "restored stock XScreenSaver" in autostart
    assert "rollback_status_rc" in autostart
    assert "screenlock_reports_unlocked" in autostart
    assert "screenlock_pid_is_alive" in autostart
    assert autostart.index("screenlock_reports_unlocked") < autostart.index(
        "xscreensaver-command --exit"
    )
    assert "focuspass-screenlock --no-splash &" in autostart
    assert "xscreensaver --no-splash &" in autostart
    shell_check = subprocess.run(
        ["sh", "-n"], input=autostart, text=True, capture_output=True
    )
    assert shell_check.returncode == 0, shell_check.stderr

    xml_root = ET.parse(ROLE / "files/minimal-desktop/rc.xml").getroot()
    ns = {"ob": "http://openbox.org/3.4/rc"}
    shortcut = xml_root.find(".//ob:keybind[@key='W-l']/ob:action/ob:command", ns)
    assert shortcut is not None and shortcut.text is not None
    assert "focuspass-screenlock-command --lock" in shortcut.text
    assert "xscreensaver-command --lock" in shortcut.text
    shell_check = subprocess.run(
        ["sh", "-n", "-c", shortcut.text], text=True, capture_output=True
    )
    assert shell_check.returncode == 0, shell_check.stderr


def test_autostart_preserves_locked_stock_screen() -> None:
    result, state, log = _run_autostart_harness(
        initial_state="stock",
        initial_status="locked",
        focuspass_responsive=True,
    )
    assert result.returncode == 1
    assert state == "stock"
    assert log == ""
    assert "active or unknown" in result.stderr


def test_autostart_ignores_stock_daemon_on_another_display() -> None:
    result, state, log = _run_autostart_harness(
        initial_state="other",
        initial_status="unlocked",
        focuspass_responsive=True,
    )
    assert result.returncode == 0
    assert state == "focuspass"
    assert log.splitlines() == ["focuspass-start"]


def test_autostart_replaces_unlocked_stock_after_responsive_focuspass_start() -> None:
    result, state, log = _run_autostart_harness(
        initial_state="stock",
        initial_status="unlocked",
        focuspass_responsive=True,
    )
    assert result.returncode == 0
    assert state == "focuspass"
    assert log.splitlines() == ["xscreensaver-exit", "focuspass-start"]


def test_autostart_does_not_trust_unresponsive_existing_focuspass() -> None:
    result, state, log = _run_autostart_harness(
        initial_state="focuspass",
        initial_status="unlocked",
        focuspass_responsive=False,
        rollback_status="unlocked",
        focuspass_exit_delay=True,
    )
    assert result.returncode == 0
    assert state == "stock"
    assert log.splitlines() == [
        "xscreensaver-exit",
        "focuspass-start",
        "focuspass-exit",
        "stock-start",
    ]
    assert "restored stock XScreenSaver" in result.stderr


def test_autostart_failure_rolls_back_only_when_status_is_not_locked() -> None:
    result, state, log = _run_autostart_harness(
        initial_state="stock",
        initial_status="unlocked",
        focuspass_responsive=False,
        rollback_status="locked",
    )
    assert result.returncode == 0
    assert state == "focuspass"
    assert log.splitlines() == ["xscreensaver-exit", "focuspass-start"]
    assert "preserving the current screen-lock state" in result.stderr


def test_autostart_preserves_a_daemon_when_rollback_status_is_unknown() -> None:
    result, state, log = _run_autostart_harness(
        initial_state="stock",
        initial_status="unlocked",
        focuspass_responsive=False,
        rollback_status="error",
    )
    assert result.returncode == 0
    assert state == "focuspass"
    assert log.splitlines() == ["xscreensaver-exit", "focuspass-start"]
    assert "preserving the current screen-lock state" in result.stderr


def test_autostart_restores_stock_when_focuspass_registers_no_daemon() -> None:
    result, state, log = _run_autostart_harness(
        initial_state="stock",
        initial_status="unlocked",
        focuspass_responsive=False,
        rollback_status="error",
        focuspass_registers=False,
    )
    assert result.returncode == 0
    assert state == "stock"
    assert log.splitlines() == ["xscreensaver-exit", "focuspass-start", "stock-start"]
    assert "restored stock XScreenSaver" in result.stderr


def test_autostart_restores_stock_on_fresh_session_without_a_daemon() -> None:
    result, state, log = _run_autostart_harness(
        initial_state="none",
        initial_status="error",
        focuspass_responsive=False,
        rollback_status="error",
        focuspass_registers=False,
    )
    assert result.returncode == 0
    assert state == "stock"
    assert log.splitlines() == ["focuspass-start", "stock-start"]
    assert "restored stock XScreenSaver" in result.stderr


def test_autostart_preserves_a_focuspass_daemon_that_will_not_exit() -> None:
    result, state, log = _run_autostart_harness(
        initial_state="focuspass",
        initial_status="unlocked",
        focuspass_responsive=False,
        rollback_status="unlocked",
        focuspass_exit_never=True,
    )
    assert result.returncode == 0
    assert state == "focuspass-stopping"
    assert log.splitlines() == ["xscreensaver-exit", "focuspass-start", "focuspass-exit"]
    assert "current screen-lock state is preserved" in result.stderr


def test_autostart_preserves_focuspass_if_graceful_exit_is_refused() -> None:
    result, state, log = _run_autostart_harness(
        initial_state="focuspass",
        initial_status="unlocked",
        focuspass_responsive=False,
        rollback_status="unlocked",
        focuspass_exit_refuses=True,
    )
    assert result.returncode == 0
    assert state == "focuspass"
    assert log.splitlines() == ["xscreensaver-exit", "focuspass-start", "focuspass-exit"]
    assert "current screen-lock state is preserved" in result.stderr


def test_focuspass_screenlock_is_opt_in_verified_and_profile_scoped() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["focuspass_screenlock_enabled"] is False
    assert defaults["focuspass_screenlock_install_url"] == ""
    assert defaults["focuspass_screenlock_install_sha256"] == ""

    desktop = _tasks(TASKS / "minimal-desktop.yml")
    preflight_index = next(
        index for index, task in enumerate(desktop)
        if "Validate optional FocusPass screen lock settings" in task.get("name", "")
    )
    focuspass_index = next(
        index for index, task in enumerate(desktop)
        if "Install optional FocusPass screen lock" in task.get("name", "")
    )
    locking_index = next(
        index for index, task in enumerate(desktop)
        if "PAM-aware graphical and TTY locking" in task.get("name", "")
    )
    openbox_index = next(
        index for index, task in enumerate(desktop)
        if "Deploy Openbox configuration" in task.get("name", "")
    )
    assert locking_index < openbox_index < preflight_index < focuspass_index
    preflight_include = desktop[preflight_index]
    assert "minimal-desktop-focuspass-preflight.yml" in preflight_include[
        "ansible.builtin.include_tasks"
    ]
    assert "workstation_profile != 'byod-kiosk'" in preflight_include["when"]
    assert "focuspass_screenlock_enabled | default(false) | bool" in preflight_include[
        "when"
    ]
    assert "ansible_check_mode" not in str(preflight_include["when"])

    focuspass_include = desktop[focuspass_index]
    assert "minimal-desktop-focuspass.yml" in focuspass_include[
        "ansible.builtin.include_tasks"
    ]
    assert "workstation_profile != 'byod-kiosk'" in focuspass_include["when"]
    assert "focuspass_screenlock_enabled | default(false) | bool" in focuspass_include[
        "when"
    ]
    assert "not ansible_check_mode" in focuspass_include["when"]

    preflight = _tasks(TASKS / "minimal-desktop-focuspass-preflight.yml")
    architecture = _named(preflight, "Require the supported amd64 FocusPass build architecture")
    assert architecture["ansible.builtin.assert"]["that"] == [
        "ansible_architecture == 'x86_64'"
    ]
    url_task = _named(preflight, "Require FocusPass screen lock package source")
    url_assertion = url_task["ansible.builtin.assert"]["that"][0]
    url_match = re.search(r"is match\('([^']+)'\)", url_assertion)
    assert url_match is not None
    url_pattern = url_match.group(1)
    assert re.match(url_pattern, "https://artifact.example/focuspass.deb")
    assert re.match(url_pattern, "https://artifact.example/focuspass.deb\n") is None

    sha_task = _named(preflight, "Require a SHA-256 for the FocusPass screen lock package")
    sha_assertion = sha_task["ansible.builtin.assert"]["that"][0]
    sha_match = re.search(r"is match\('([^']+)'\)", sha_assertion)
    assert sha_match is not None
    sha_pattern = sha_match.group(1)
    assert re.match(sha_pattern, "a" * 64)
    assert re.match(sha_pattern, "a" * 64 + "\n") is None

    focuspass = _tasks(TASKS / "minimal-desktop-focuspass.yml")
    download = _named(focuspass, "Download the verified FocusPass screen lock package")
    get_url = download["ansible.builtin.get_url"]
    assert get_url["url"] == "{{ focuspass_screenlock_install_url }}"
    assert get_url["checksum"] == (
        "sha256:{{ focuspass_screenlock_install_sha256 | lower }}"
    )
    assert "workstation_update_artifact_cache_dir" in get_url["dest"]
    assert get_url["force"] is False

    package_name = _named(focuspass, "Read the downloaded FocusPass Debian package name")
    assert package_name["ansible.builtin.command"]["argv"][-1] == "Package"
    assert "focuspass-screenlock" in str(package_name["ansible.builtin.command"]["argv"])
    package_identity = _named(
        focuspass, "Require the expected FocusPass Debian package identity"
    )
    changed_digest_remove = _named(
        focuspass, "Remove FocusPass package before installing a different pinned artifact"
    )
    install = _named(focuspass, "Install the verified FocusPass screen lock package")
    managed_marker = _named(
        focuspass, "Mark FocusPass as Catalog-managed before package mutation"
    )
    assert managed_marker["ansible.builtin.copy"]["dest"] == (
        "/var/lib/curiosity/focuspass-screenlock.managed"
    )
    assert focuspass.index(package_identity) < focuspass.index(managed_marker)
    assert focuspass.index(managed_marker) < focuspass.index(changed_digest_remove)
    assert focuspass.index(changed_digest_remove) < focuspass.index(install)
    assert "b64decode" in changed_digest_remove["when"]
    assert install["ansible.builtin.apt"]["deb"] == get_url["dest"]

    launcher_check = _named(focuspass, "Require executable FocusPass screen-lock launchers")
    assert "_focuspass_screenlock_daemon.stat.executable" in launcher_check[
        "ansible.builtin.assert"
    ]["that"]
    assert "_focuspass_screenlock_command.stat.executable" in launcher_check[
        "ansible.builtin.assert"
    ]["that"]
    assert _named(focuspass, "Inspect the FocusPass screen-lock launcher")[
        "become_user"
    ] == "{{ dev_user }}"
    assert _named(focuspass, "Inspect the FocusPass screen-lock command")[
        "become_user"
    ] == "{{ dev_user }}"
    digest_record = _named(focuspass, "Record the installed FocusPass artifact digest")
    assert digest_record["ansible.builtin.copy"]["dest"] == (
        "/var/lib/curiosity/focuspass-screenlock.sha256"
    )
    assert "focuspass_screenlock_install_sha256 | lower" in digest_record[
        "ansible.builtin.copy"
    ]["content"]

    main_tasks = _tasks(TASKS / "main.yml")
    ownership_stamp = _named(
        main_tasks, "Inspect FocusPass ownership state before optional package cleanup"
    )
    assert ownership_stamp["ansible.builtin.stat"]["path"] == (
        "/var/lib/curiosity/focuspass-screenlock.managed"
    )
    digest_stamp = _named(
        main_tasks, "Inspect FocusPass digest state before optional package cleanup"
    )
    assert digest_stamp["ansible.builtin.stat"]["path"] == (
        "/var/lib/curiosity/focuspass-screenlock.sha256"
    )
    cleanup = _named(
        main_tasks,
        "Remove FocusPass lock package outside its enabled minimal-desktop profiles",
    )
    assert cleanup["ansible.builtin.apt"]["name"] == "focuspass-screenlock"
    assert cleanup["ansible.builtin.apt"]["state"] == "absent"
    cleanup_condition = " ".join(cleanup["when"])
    assert "ansible_os_family == 'Debian'" in cleanup_condition
    assert "_focuspass_managed_marker_stamp.stat.exists" in cleanup_condition
    assert "_focuspass_installed_digest_stamp.stat.exists" in cleanup_condition
    assert "focuspass_screenlock_enabled" in cleanup_condition
    assert "workstation_profile in ['desktop', 'thin-client']" in cleanup_condition
    assert "minimal_desktop_enabled" in cleanup_condition
    cleanup_index = main_tasks.index(cleanup)
    desktop_include_index = next(
        index for index, task in enumerate(main_tasks)
        if task.get("name") == "Include minimal desktop (Openbox, LightDM, Firefox)"
    )
    assert (
        main_tasks.index(ownership_stamp)
        < main_tasks.index(digest_stamp)
        < cleanup_index
        < desktop_include_index
    )
    digest_cleanup = _named(
        main_tasks, "Remove stale FocusPass artifact digest when integration is inactive"
    )
    assert digest_cleanup["ansible.builtin.file"]["state"] == "absent"
    marker_cleanup = _named(
        main_tasks,
        "Remove stale FocusPass ownership marker when integration is inactive",
    )
    assert marker_cleanup["ansible.builtin.file"]["path"] == (
        "/var/lib/curiosity/focuspass-screenlock.managed"
    )
    assert marker_cleanup["ansible.builtin.file"]["state"] == "absent"
    assert main_tasks.index(cleanup) < main_tasks.index(digest_cleanup)
    assert main_tasks.index(cleanup) < main_tasks.index(marker_cleanup)


def test_kiosk_cleanup_checks_identity_and_retains_snapshots_on_restore_errors() -> None:
    kiosk = _tasks(TASKS / "kiosk-session.yml")
    identity_snapshot = _named(kiosk, "Save the account identity")
    identity = identity_snapshot["ansible.builtin.copy"]
    assert identity["dest"].endswith("/identity.json")
    assert "dev_user" in identity["content"] and "dev_user_home" in identity["content"]

    cleanup = _tasks(TASKS / "kiosk-cleanup.yml")
    identity_guard_index = next(
        index for index, task in enumerate(cleanup)
        if "Require kiosk cleanup to target" in task.get("name", "")
    )
    groups_restore_index = next(
        index for index, task in enumerate(cleanup)
        if "Restore the kiosk user's original supplementary groups" in task.get("name", "")
    )
    assert identity_guard_index < groups_restore_index

    unmask = _named(cleanup, "Remove the kiosk persistent mask")
    assert "not-found" not in " ".join(unmask["when"])
    assert "masked" in " ".join(unmask["when"])
    restore_tasks = [
        task for task in cleanup
        if any(fragment in task.get("name", "") for fragment in (
            "Remove the kiosk persistent mask",
            "Reapply originally runtime-masked",
            "Restore previously runtime-enabled",
        ))
    ]
    assert restore_tasks and all("failed_when" not in task for task in restore_tasks)
    snapshot_removal_index = next(
        index for index, task in enumerate(cleanup)
        if "Remove saved kiosk profile state" in task.get("name", "")
    )
    assert snapshot_removal_index > max(cleanup.index(task) for task in restore_tasks)


def test_sdkman_repairs_runtime_without_overwriting_dirty_source() -> None:
    tasks = _tasks(TASKS / "languages.yml")
    eligibility = _named(tasks, "source-update and runtime-repair eligibility")["ansible.builtin.set_fact"]
    assert "_sdkman_allow_source_update" in eligibility
    assert eligibility["_sdkman_manage_runtime"] is True

    checkout = _named(tasks, "Install or update SDKMAN")
    assert "_sdkman_allow_source_update | bool" in " ".join(checkout["when"])
    runtime_dirs = _named(tasks, "Ensure SDKMAN runtime directories exist")
    assert runtime_dirs["when"] == "_sdkman_manage_runtime | bool"
    patch = _named(tasks, "Exclude SDKMAN's own init file")
    assert "_sdkman_allow_source_update | bool" in " ".join(patch["when"])


def test_rsyslog_validates_configuration_before_restart() -> None:
    handlers = yaml.safe_load((ROLE / "handlers/main.yml").read_text())
    validate_index = next(
        index for index, handler in enumerate(handlers)
        if handler.get("name") == "Validate rsyslog configuration before restart"
    )
    restart_index = next(
        index for index, handler in enumerate(handlers)
        if handler.get("name") == "Restart rsyslog"
    )
    validate = handlers[validate_index]
    assert validate["ansible.builtin.command"]["argv"] == ["rsyslogd", "-N1"]
    assert validate_index < restart_index
    assert handlers[restart_index]["listen"] == validate["listen"] == "Restart rsyslog"


def test_platform_specific_greeter_and_wallpaper_settings_are_gated() -> None:
    packages = _tasks(TASKS / "minimal-desktop-packages.yml")
    wallpaper = _named(packages, "Ubuntu greeter wallpaper")
    assert wallpaper["when"] == "ansible_distribution == 'Ubuntu'"

    greeter = _tasks(TASKS / "minimal-desktop-lightdm.yml")
    config = _named(greeter, "Configure LightDM GTK greeter")
    assert "indicators = ~host;~spacer;~session;~language;~a11y;~clock;~power" in config[
        "ansible.builtin.copy"
    ]["content"]

    assert "'resolute': 'jammy'" in (TASKS / "ziti.yml").read_text()


def test_desktop_self_healing_installs_hardware_monitoring() -> None:
    tasks = _tasks(TASKS / "self-healing.yml")
    assert _named(tasks, "Install smartmontools")["ansible.builtin.apt"]["name"] == "smartmontools"
    assert _named(tasks, "Install lm-sensors")["ansible.builtin.apt"]["name"] == "lm-sensors"
