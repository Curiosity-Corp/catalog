"""Public OSS contract checks that do not require a managed host."""

import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"


def test_required_public_files_exist() -> None:
    required = (
        "README.md",
        "LICENSE",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
        "SUPPORT.md",
        "CHANGELOG.md",
        "galaxy.yml",
        "docs/architecture.md",
        "docs/compatibility.md",
        "docs/configuration.md",
        "docs/release-checklist.md",
    )
    assert all((ROOT / path).is_file() for path in required)
    assert (ROLE / "meta/main.yml").is_file()


def test_collection_metadata_is_publishable() -> None:
    metadata = yaml.safe_load((ROOT / "galaxy.yml").read_text())
    assert metadata["namespace"] == "curiosity"
    assert metadata["name"] == "workstation"
    assert metadata["version"] == "0.1.0"
    assert "Apache-2.0" in metadata["license"]
    assert metadata["dependencies"]["community.general"]


def test_defaults_are_safe_for_unrelated_machines() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["coder_cli_enabled"] is False
    assert defaults["cloud_clis_enabled"] is True
    assert defaults["keycloak_sso_enabled"] is False
    assert defaults["ziti_enabled"] is False
    assert defaults["sunshine_enabled"] is False
    assert defaults["profile_cleanup_enabled"] is False
    assert defaults["ziti_etc_hosts_pins"] == []
    assert defaults["ziti_intercept_domains"] == []
    assert defaults["custom_ca_certificates"] == []
    assert defaults["sunshine_default_pass"] == ""
    assert defaults["desktop_polish_enabled"] is False
    assert defaults["minimal_desktop_autologin"] is True
    assert defaults["boot_prompt_visibility_enabled"] is False


def test_t460_profile_supports_connected_camera_and_scarlett_devices() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    packages = set(defaults["hardware_profiles"]["t460"]["packages"])
    assert {"gphoto2", "gvfs-backends", "gvfs-fuse"} <= packages
    assert {"alsa-scarlett-gui", "alsa-utils"} <= packages

    rules = defaults["hardware_profiles"]["t460"]["udev_rules"]
    assert any("04a9" in rule["content"] and "319b" in rule["content"] for rule in rules)


def test_encrypted_boot_prompt_is_opt_in_and_keeps_grub_reproducible() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["boot_prompt_visibility_enabled"] is False

    main = (ROLE / "tasks/main.yml").read_text()
    assert "include_tasks: boot-experience.yml" in main
    assert "boot_prompt_visibility_enabled | default(false) | bool" in main

    boot = (ROLE / "tasks/boot-experience.yml").read_text()
    assert "plymouth-theme-ubuntu-text" in boot
    assert "GRUB_CMDLINE_LINUX_DEFAULT" in boot
    assert "['quiet', 'splash']" in boot
    assert "Refresh GRUB and initramfs for Plymouth" in boot

    hardware = (ROLE / "tasks/hardware-drivers.yml").read_text()
    assert "cpu-performance.service" in hardware
    assert "notify: Reload systemd" in hardware

    handlers = (ROLE / "handlers/main.yml").read_text()
    assert "name: Reload systemd" in handlers
    assert "daemon_reload: true" in handlers


def test_private_material_is_not_in_the_public_surface() -> None:
    source_paths = [ROOT / "roles", ROOT / "playbooks", ROOT / "renovate.json"]
    old_private_markers = (
        "developerdojo.org",
        "focusapiary",
        "focuscell.org",
        "focuspass.com",
        "focusbuzz.org",
        "dreamhive.org",
        "dreamdecks.org",
        "hardmagic.com",
        "hypersight.net",
        "slidee.net",
        "omlab-secrets",
        "harbor-internal-ca",
    )
    text = "\n".join(
        path.read_text(errors="replace")
        for root in source_paths
        for path in (root.rglob("*") if root.is_dir() else [root])
        if path.is_file()
    ).lower()
    for marker in old_private_markers:
        assert marker not in text
    assert not list((ROLE / "files").rglob("*.crt"))
    assert 'CLIENT_ID = ""' in (ROLE / "files/teams.py").read_text()


def test_renovate_is_public_and_valid_json() -> None:
    config = json.loads((ROOT / "renovate.json").read_text())
    assert config["$schema"].startswith("https://docs.renovatebot.com/")
    assert "config:recommended" in config["extends"]



def test_desktop_polish_preserves_openbox_configuration() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/desktop-polish.yml").read_text())
    shortcut = next(
        task["ansible.builtin.blockinfile"]
        for task in tasks
        if task.get("name", "").startswith("Add an alternate Flameshot")
    )
    pattern = shortcut["insertbefore"]
    assert re.search(pattern, "</keyboard>")
    assert re.search(pattern, "  </keyboard>")
    assert shortcut["validate"]

    tint2 = yaml.safe_load((ROLE / "tasks/minimal-desktop-tint2.yml").read_text())
    startup = next(
        task
        for task in tint2
        if task.get("name", "").startswith("Add useful helpers")
    )
    assert "ansible.builtin.blockinfile" in startup
    assert startup["when"] == [
        "workstation_profile == 'desktop'",
        "desktop_polish_enabled | default(false) | bool",
        "not (minimal_desktop_enabled | default(false) | bool)",
    ]
