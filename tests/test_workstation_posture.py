"""Regression checks for the minimal desktop and profile transitions."""

from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = ROLE / "tasks"


def _tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text())


def _named(tasks: list[dict], fragment: str) -> dict:
    return next(task for task in tasks if fragment in task.get("name", ""))


def test_openbox_uses_pam_aware_locker_and_keeps_tty_locking() -> None:
    packages = _tasks(TASKS / "minimal-desktop-packages.yml")
    install = _named(packages, "PAM-aware locking tools")
    assert set(install["ansible.builtin.apt"]["name"]) == {"xscreensaver", "vlock"}
    assert install["when"] == "workstation_profile != 'byod-kiosk'"

    remove_i3lock = _named(packages, "Remove the single-prompt i3lock")
    assert set(remove_i3lock["ansible.builtin.apt"]["name"]) == {"i3lock", "xss-lock"}

    lock_tasks = _tasks(TASKS / "minimal-desktop-locking.yml")
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
    assert "if command -v focuspass-screenlock >/dev/null; then" in autostart
    assert "elif command -v xscreensaver >/dev/null 2>&1; then" in autostart
    assert autostart.index("focuspass-screenlock >/dev/null") < autostart.index(
        "elif command -v xscreensaver"
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
    assert focuspass.index(package_identity) < focuspass.index(changed_digest_remove)
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
    cleanup = _named(
        main_tasks,
        "Remove FocusPass lock package outside its enabled minimal-desktop profiles",
    )
    assert cleanup["ansible.builtin.apt"]["name"] == "focuspass-screenlock"
    assert cleanup["ansible.builtin.apt"]["state"] == "absent"
    cleanup_condition = " ".join(cleanup["when"])
    assert "ansible_os_family == 'Debian'" in cleanup_condition
    assert "focuspass_screenlock_enabled" in cleanup_condition
    assert "workstation_profile in ['desktop', 'thin-client']" in cleanup_condition
    assert "minimal_desktop_enabled" in cleanup_condition
    cleanup_index = main_tasks.index(cleanup)
    desktop_include_index = next(
        index for index, task in enumerate(main_tasks)
        if task.get("name") == "Include minimal desktop (Openbox, LightDM, Firefox)"
    )
    assert cleanup_index < desktop_include_index
    digest_cleanup = _named(
        main_tasks, "Remove stale FocusPass artifact digest when integration is inactive"
    )
    assert digest_cleanup["ansible.builtin.file"]["state"] == "absent"


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
