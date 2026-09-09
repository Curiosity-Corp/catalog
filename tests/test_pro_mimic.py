"""Regression checks for the free Ubuntu-Pro equivalents plane (pro-mimic).

The fleet converges Pro-equivalent posture (security origins, needrestart
automation, hardened SSH, MOTD visibility, ESM reporting, GA kernel
pinning, pull-status records, display-manager mode) via ansible-pull without
attaching machines to Ubuntu Pro. These checks pin the safety properties:
no reboots, no remediation, no token, and codename-templated (never pinned)
release references.
"""

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = ROLE / "tasks"

PRO_MIMIC_CHILDREN = (
    "pro-mimic-unattended-origins.yml",
    "pro-mimic-needrestart.yml",
    "pro-mimic-ssh.yml",
    "pro-mimic-motd.yml",
    "pro-mimic-esm.yml",
    "pro-mimic-kernel.yml",
    "pro-mimic-display-manager.yml",
    "pro-mimic-pull-status.yml",
)


def _child_text() -> dict:
    return {name: (TASKS / name).read_text() for name in PRO_MIMIC_CHILDREN}


def test_pro_mimic_safe_defaults() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["ubuntu_pro_token"] == ""
    assert defaults["pro_mimic_needrestart_auto"] is True
    assert defaults["pro_mimic_needrestart_blacklist"] == []
    assert defaults["pro_mimic_ssh_password_auth"] is False
    assert defaults["pro_mimic_cis_remediate"] is False
    assert defaults["display_manager_mode"] == "always"
    assert defaults["pro_mimic_motd_news_enabled"] is False

    origins = (TASKS / "pro-mimic-unattended-origins.yml").read_text()
    assert 'Unattended-Upgrade::Automatic-Reboot "false"' in origins


def test_pro_mimic_files_are_included_with_tags() -> None:
    main = (TASKS / "main.yml").read_text()
    assert "ansible.builtin.include_tasks: pro-mimic.yml" in main
    assert "tags: [pro-mimic, security]" in main

    parent = (TASKS / "pro-mimic.yml").read_text()
    for name in PRO_MIMIC_CHILDREN:
        stem = name.removesuffix(".yml")
        assert f"include_tasks: {name}" in parent, f"{name} is not included"
        assert (TASKS / name).is_file()
    # Every child include in the parent carries tags so focused
    # `--tags pro-mimic` runs stay selectable per area.
    assert parent.count("tags: [pro-mimic") == len(PRO_MIMIC_CHILDREN)


def _code_lines(text: str) -> list:
    """Non-comment, non-blank lines: documentation may name a prohibition
    (e.g. "no AllowUsers") while the managed configuration must not contain
    it, so functional assertions run against code lines only."""
    return [
        line
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_no_pro_attach_outside_the_existing_token_gate() -> None:
    for name, text in _child_text().items():
        code = "\n".join(_code_lines(text))
        assert "pro attach" not in code, f"{name} must not attach to Pro"
        assert "ubuntu_pro_token" not in code, f"{name} must not touch the token"
    maintenance = (TASKS / "maintenance.yml").read_text()
    assert "pro attach {{ ubuntu_pro_token }}" in maintenance
    assert "ubuntu_pro_token | default('') | length > 0" in maintenance


def test_no_numeric_release_urls_or_pinned_codenames() -> None:
    for name, text in _child_text().items():
        assert not re.findall(r"releases/download/(?:v)?\d+(?:\.\d+)+", text), (
            f"{name} contains a numeric release URL"
        )
        for codename in ("noble", "jammy", "focal", "mantic", "oracular", "plucky"):
            assert codename not in text.lower(), (
                f"{name} pins release codename {codename!r}; "
                "template on ansible_distribution_release instead"
            )
    origins = (TASKS / "pro-mimic-unattended-origins.yml").read_text()
    assert "{{ ansible_distribution_release }}" in origins
    assert "universe-security" in origins


def test_t460_posture_flag_parity() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    profiles = defaults["hardware_profiles"]
    assert profiles["t460"]["disable_sleep"] is True
    assert profiles["t460"]["performance_mode"] is True
    # Every laptop-kind profile that ships TLP must define both posture
    # flags, otherwise the system-tuning gates silently skip the fleet.
    for name, profile in profiles.items():
        if "tlp" in (profile.get("packages") or []):
            assert profile.get("disable_sleep") is True, f"{name} lacks disable_sleep"
            assert profile.get("performance_mode") is True, (
                f"{name} lacks performance_mode"
            )


def test_laptop_netplan_gate_covers_both_laptop_profiles() -> None:
    tuning = (TASKS / "system-tuning.yml").read_text()
    assert "hardware_profile in ['t460', 'x86-byod-laptop']" in tuning
    assert "hardware_profile == 'x86-byod-laptop'" not in tuning


def test_ssh_hardening_is_a_minimal_hand_rolled_subset() -> None:
    ssh = (TASKS / "pro-mimic-ssh.yml").read_text()
    for fragment in (
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "PermitRootLogin prohibit-password",
        "X11Forwarding no",
        "sshd_config.d/60-pro-mimic.conf",
        "pro_mimic_ssh_password_auth",
        "Validate sshd configuration",
    ):
        assert fragment in ssh, f"ssh hardening is missing {fragment!r}"
    # Workstation-safe: the port stays 22 and no user-access restriction is
    # introduced (key-only BatchMode SSH must keep working).
    code = "\n".join(_code_lines(ssh))
    assert "AllowUsers" not in code
    assert "AllowGroups" not in code
    assert re.search(r"(?m)^\s*Port\s", code) is None
    # The collection decision is documented in-file: the pull path never
    # installs new Galaxy collections, so this stays hand-rolled.
    assert "devsec.hardening" in ssh
    assert "ansible-galaxy" in ssh
    handlers = (ROLE / "handlers/main.yml").read_text()
    assert "Validate sshd configuration" in handlers
    assert "cmd: sshd -t" in handlers


def test_display_manager_modes_are_explicit() -> None:
    display = (TASKS / "pro-mimic-display-manager.yml").read_text()
    assert "display_manager_mode in ['always', 'ondemand', 'disabled']" in display
    assert "systemctl get-default" in display
    assert "systemctl set-default multi-user.target" in display
    assert "lightdm" in display


def test_no_livepatch_diy() -> None:
    for name, text in _child_text().items():
        for line in _code_lines(text):
            assert "kpatch" not in line.lower(), f"{name} must not DIY livepatch"
            assert "livepatch" not in line.lower(), (
                f"{name} references livepatch outside a comment: {line.strip()!r}"
            )


def test_cis_remediation_stays_absent_with_allowlist_approach() -> None:
    defaults_text = (ROLE / "defaults/main.yml").read_text()
    assert "pro_mimic_cis_remediate: false" in defaults_text
    assert "allowlist" in defaults_text
    for name, text in _child_text().items():
        assert "--remediate" not in text, f"{name} must not auto-remediate CIS"
