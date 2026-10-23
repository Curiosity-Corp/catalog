"""The system time zone is declared per host (`workstation_timezone`).

The properties run on Eastern time; a host left on UTC showed the wrong clock and
put StreamStack's sunrise and sunset at Greenwich.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROLE = Path(__file__).resolve().parents[1] / "roles/developer_workstation"


def test_the_default_leaves_the_zone_alone() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["workstation_timezone"] == ""


def test_main_sets_the_zone_only_when_declared() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    task = next(t for t in tasks if t["name"] == "Set the system time zone")
    assert task["ansible.builtin.include_tasks"]["file"] == "timezone.yml"
    assert task["when"] == "workstation_timezone | length > 0"
    assert task["tags"] == ["timezone"]


def test_an_unknown_zone_is_refused_before_the_clock_changes() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/timezone.yml").read_text())
    names = [t["name"] for t in tasks]
    assert names.index("Refuse an unknown time zone") < names.index("Set the system time zone")
    setter = tasks[names.index("Set the system time zone")]
    assert setter["community.general.timezone"]["name"] == "{{ workstation_timezone }}"
