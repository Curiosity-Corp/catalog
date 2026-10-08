"""Regression checks for the minimal desktop and profile transitions."""

from pathlib import Path
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
    config = _named(lock_tasks, "Configure XScreenSaver to blank")["ansible.builtin.blockinfile"]["block"]
    assert "// 60" in config and "% 60" in config
    assert "lock: True" in config and "mode: blank" in config
    pam = _named(lock_tasks, "host login authentication prompts")["ansible.builtin.copy"]["content"]
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
    assert "xscreensaver --no-splash &" in autostart
    xml_root = ET.fromstring((ROLE / "files/minimal-desktop/rc.xml").read_text())
    ns = {"ob": "http://openbox.org/3.4/rc"}
    shortcut = xml_root.find(".//ob:keybind[@key='W-l']/ob:action/ob:command", ns)
    assert shortcut is not None and shortcut.text == "xscreensaver-command --lock"


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
