"""Regression checks for Microsoft 365 CLI entry points published without +x."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
TASKS = yaml.safe_load((ROLE / "tasks/m365-cli.yml").read_text())


def test_every_m365_package_declares_its_entry_points() -> None:
    links = DEFAULTS["m365_cli_command_links"]
    assert {entry["package"] for entry in links} == set(DEFAULTS["m365_cli_packages"])
    commands = {entry["command"]: entry["relative_path"] for entry in links}
    assert commands["m365"] == "dist/index.js"
    # The healthcheck runs the m365 launcher, so it must be one of the fixed paths.
    healthcheck = DEFAULTS["workstation_application_contracts"]["m365-cli"]["healthcheck"]
    assert healthcheck[0] == "{{ npm_global_prefix }}/bin/m365"


def test_entry_points_are_made_executable_after_the_npm_refresh() -> None:
    names = [task["name"] for task in TASKS]
    install = names.index("Update user-scoped Microsoft 365 CLI to latest")
    chmod = names.index("Ensure Microsoft 365 CLI entry points are executable")
    assert install < chmod
    task = TASKS[chmod]
    assert task["ansible.builtin.file"]["mode"] == "0755"
    assert task["loop"] == "{{ m365_cli_command_links }}"
    assert task["become_user"] == "{{ dev_user }}"
