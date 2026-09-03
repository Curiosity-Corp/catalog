"""Regression checks for declarative user-scoped systemd masks."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"


def test_user_systemd_masks_are_opt_in_and_native() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["workstation_user_systemd_masked_units"] == []

    policy = (ROLE / "tasks/user-systemd.yml").read_text()
    for fragment in (
        "ansible.builtin.systemd_service:",
        "scope: user",
        "state: stopped",
        "enabled: false",
        "src: /dev/null",
        "default.target.wants",
        "timers.target.wants",
        "systemctl",
        "show-environment",
        "ActiveState=failed",
        "Result=",
        "reset-failed",
    ):
        assert fragment in policy, f"user systemd policy is missing {fragment!r}"

    # The role must use the declarative module/file primitives, not recreate
    # the retired imperative coaching loop or invoke `systemctl mask` directly.
    assert "dream-hive-poppy-coach" not in policy
    assert "systemctl mask" not in policy


def test_main_reaches_user_systemd_policy() -> None:
    main = (ROLE / "tasks/main.yml").read_text()
    assert "file: user-systemd.yml" in main
    assert "workstation_user_systemd_masked_units" in main


def test_configuration_documents_protected_inventory_boundary() -> None:
    docs = (ROOT / "docs/configuration.md").read_text()
    assert "workstation_user_systemd_masked_units" in docs
    assert "protected" in docs
    assert "inventory" in docs
