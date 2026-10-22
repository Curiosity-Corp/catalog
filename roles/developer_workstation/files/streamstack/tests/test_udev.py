"""Packaged udev rules grant hidraw access to exact Litra IDs and nothing else."""

from __future__ import annotations

import re
from pathlib import Path

RULES = Path(__file__).parent.parent / "packaging/udev/60-streamstack-litra.rules"
LITRA_PIDS = {"c900", "c901", "b901", "c903"}


def rule_lines() -> list[str]:
    return [ln for ln in RULES.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_rules_match_exactly_the_litra_product_ids() -> None:
    pids = set()
    for line in rule_lines():
        assert 'SUBSYSTEM=="hidraw"' in line
        assert 'ATTRS{idVendor}=="046d"' in line
        match = re.search(r'ATTRS\{idProduct\}=="([0-9a-f]{4})"', line)
        assert match, line
        pids.add(match.group(1))
    assert pids == LITRA_PIDS


def test_rules_leave_other_logitech_devices_alone() -> None:
    text = "\n".join(rule_lines())
    assert "c539" not in text  # Lightspeed receiver
    assert "*" not in text and "KERNEL" not in text  # no wildcard widening


def test_rules_use_the_workstation_role_pattern() -> None:
    for line in rule_lines():
        assert 'TAG+="uaccess"' in line
        assert 'GROUP="plugdev"' in line
        assert 'MODE="0660"' in line
