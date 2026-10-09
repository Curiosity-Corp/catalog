"""Rollback must restore symlinked launchers as links, not as copied binaries."""

from pathlib import Path

import yaml


TASKS = Path(__file__).resolve().parents[1] / "roles/developer_workstation/tasks"
FOUNDATION = yaml.safe_load((TASKS / "update-foundation.yml").read_text())
ROLLBACK = yaml.safe_load((TASKS / "update-rollback.yml").read_text())


def _task(tasks: list[dict], name: str) -> dict:
    return next(t for t in tasks if t.get("name") == name)


def test_inventory_records_symlink_targets_without_following() -> None:
    lstat = _task(FOUNDATION, "Check which managed paths are symlinks before the update transaction")
    assert lstat["ansible.builtin.stat"]["follow"] is False
    assert lstat["register"] == "_workstation_managed_links_before"
    inventory = _task(FOUNDATION, "Build the pre-update binary inventory")
    template = inventory["ansible.builtin.set_fact"]["workstation_update_before_inventory"]
    assert "'link_target'" in template and "lnk_target" in template
    assert inventory["loop_control"]["index_var"] == "_managed_path_index"


def test_snapshot_skips_symlinks() -> None:
    snapshot = _task(FOUNDATION, "Snapshot existing managed binaries for rollback")
    assert snapshot["loop"] == "{{ workstation_update_before_inventory }}"
    assert "item.link_target | length == 0" in snapshot["when"]


def test_rollback_restores_links_as_links_and_files_as_files() -> None:
    files = _task(ROLLBACK, "Restore managed binaries from the pre-update snapshot")
    assert "item.link_target | default('') | length == 0" in files["when"]
    links = _task(ROLLBACK, "Restore managed launcher symlinks from the pre-update inventory")
    module = links["ansible.builtin.file"]
    assert module["state"] == "link"
    assert module["src"] == "{{ item.link_target }}"
    assert module["force"] is True
    assert "item.link_target | default('') | length > 0" in links["when"]
