"""The variable naming plan (docs/variable-naming-plan.md) stays computable."""

import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import variable_mapping as vm  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_rule_examples() -> None:
    assert vm.new_name("developer_workstation_local_vars_file") == "developer_workstation_local_vars_file"
    assert vm.new_name("workstation_update_ring") == "developer_workstation_update_ring"
    assert vm.new_name("workstation_profile") == "developer_workstation_profile"
    assert vm.new_name("coder_cli_enabled") == "developer_workstation_coder_cli_enabled"
    assert vm.new_name("hardware_profile") == "developer_workstation_hardware_profile"


def test_rule_is_idempotent_for_every_current_name() -> None:
    for old in vm.public_names():
        new = vm.new_name(old)
        assert vm.new_name(new) == new


def test_no_two_current_names_collide() -> None:
    assert vm.collisions(vm.mapping(vm.public_names())) == {}


def test_collision_detection_works() -> None:
    table = {"workstation_x": vm.new_name("workstation_x"), "developer_workstation_x": "developer_workstation_x"}
    assert vm.collisions(table) == {"developer_workstation_x": ["workstation_x", "developer_workstation_x"]}


def test_names_cover_the_defaults_file() -> None:
    defaults = yaml.safe_load((ROOT / "roles/developer_workstation/defaults/main.yml").read_text())
    assert set(vm.public_names()) == {k for k in defaults if not k.startswith("_")}
    assert len(vm.public_names()) > 100


def test_cli_check_passes_on_the_real_role() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/variable_mapping.py"), "--check"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_lint_exception_the_plan_removes_is_still_declared() -> None:
    text = (ROOT / ".ansible-lint").read_text()
    assert "var-naming[no-role-prefix]" in text
