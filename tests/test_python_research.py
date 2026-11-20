"""The Python research profile is a real, validated variable set, not a code edit.

`science_python_packages` and friends replace the package lists that were
hardcoded in science-tools.yml. These tests run without network access: they
execute the role's own validation task with synthetic lists, check that the
example only uses real role variables and keeps ipykernel, preview the
documented tag set, and keep the guide and the CI validation script in step.
The real install runs in the research-profile CI job
(scripts/validate_python_research.py).
"""

from __future__ import annotations

import importlib.util
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
EXAMPLE = yaml.safe_load((ROOT / "examples/python-research.yml").read_text())
GUIDE = (ROOT / "docs/python-research-profile.md").read_text()

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")


def tasks(name: str) -> list[dict]:
    return yaml.safe_load((ROLE / "tasks" / name).read_text())


def task_named(items: list[dict], name: str) -> dict:
    return next(t for t in items if t["name"] == name)


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")}
    env.update(ANSIBLE_ROLES_PATH=str(ROOT / "roles"), ANSIBLE_NOCOLOR="1", ANSIBLE_FORCE_COLOR="0",
               ANSIBLE_LOCALHOST_WARNING="False", ANSIBLE_INVENTORY_UNPARSED_WARNING="False")
    return env


def _validator():
    spec = importlib.util.spec_from_file_location("validate_python_research", ROOT / "scripts/validate_python_research.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the variables ------------------------------------------------------------


def test_example_uses_only_real_variables_and_keeps_ipykernel() -> None:
    unknown = sorted(set(EXAMPLE) - set(DEFAULTS))
    assert not unknown, unknown
    assert "ipykernel" in EXAMPLE["science_python_packages"]
    assert EXAMPLE["workstation_profile"] == "workspace", "the profile must not pull in the desktop stack"
    assert len(EXAMPLE["science_python_packages"]) < len(DEFAULTS["science_python_packages"]), "the example is the small baseline"
    assert set(EXAMPLE["science_python_upgrade_packages"]) <= set(EXAMPLE["science_python_packages"])


def test_example_pins_nothing() -> None:
    for requirement in EXAMPLE["science_python_packages"] + EXAMPLE["science_python_upgrade_packages"]:
        assert re.fullmatch(r"[A-Za-z0-9._-]+", requirement), f"{requirement} pins a version; use a constraints file"


def test_default_package_list_is_unchanged_so_existing_fleets_see_no_difference() -> None:
    packages = DEFAULTS["science_python_packages"]
    for expected in ("numpy", "scipy", "pandas", "matplotlib", "ipykernel", "ipywidgets", "qutip", "astropy", "pybullet"):
        assert expected in packages
    assert len(packages) == 26
    assert len(DEFAULTS["science_python_upgrade_packages"]) == 17
    assert set(DEFAULTS["science_python_upgrade_packages"]) <= set(packages)
    assert DEFAULTS["science_python_pip_extra_args"] == ""
    assert DEFAULTS["science_python_venv"] == "/opt/science-venv"


def test_both_pip_tasks_read_the_variables_and_no_package_is_hardcoded() -> None:
    venv = tasks("science-venv.yml")
    install = task_named(venv, "Install/upgrade scientific Python packages into venv")
    upgrade = task_named(venv, "Upgrade all pip packages in science-venv (periodic)")
    assert install["ansible.builtin.pip"]["name"] == "{{ science_python_packages }}"
    assert upgrade["ansible.builtin.pip"]["name"] == "{{ science_python_upgrade_packages }}"
    for step in (install, upgrade):
        assert step["ansible.builtin.pip"]["extra_args"] == "{{ science_python_pip_extra_args | default(omit, true) }}"
        assert step["ansible.builtin.pip"]["virtualenv"] == "{{ science_python_venv }}"
        assert step["ansible.builtin.pip"]["state"] == "latest"
    text = (ROLE / "tasks/science-tools.yml").read_text() + (ROLE / "tasks/science-venv.yml").read_text()
    for package in ("numpy", "scipy", "pybullet", "qutip"):
        assert not re.search(rf"^\s*-\s*{package}\s*$", text, re.MULTILINE), f"{package} is hardcoded again"


def test_system_paths_follow_the_venv_variable() -> None:
    text = (ROLE / "tasks/science-tools.yml").read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "/opt/science-venv" not in code


# --- the validation task, executed -------------------------------------------

BASE = {"science_python_venv": "/tmp/unused", "science_python_upgrade_packages": [], "science_python_pip_extra_args": ""}
LISTS_ACCEPTED = {
    "small": {"science_python_packages": ["numpy", "ipykernel"]},
    "specifiers": {"science_python_packages": ["numpy>=2", "IPyKernel==6.29.5", "ipykernel_x"]},
    "extras": {"science_python_packages": ["ipykernel[all]>=6"], "science_python_upgrade_packages": ["ipykernel"]},
    "example": {k: EXAMPLE[k] for k in ("science_python_packages", "science_python_upgrade_packages", "science_python_pip_extra_args")},
    "defaults": {"science_python_packages": DEFAULTS["science_python_packages"],
                 "science_python_upgrade_packages": DEFAULTS["science_python_upgrade_packages"]},
}
LISTS_REJECTED = {
    "empty": {"science_python_packages": []},
    "no_ipykernel": {"science_python_packages": ["numpy", "scipy"]},
    "lookalike": {"science_python_packages": ["ipykernel-helpers", "numpy"]},
    "string": {"science_python_packages": "numpy ipykernel"},
    "mapping": {"science_python_packages": {"ipykernel": "latest"}},
    "upgrade_not_list": {"science_python_packages": ["ipykernel"], "science_python_upgrade_packages": "ipykernel"},
    "extra_args_list": {"science_python_packages": ["ipykernel"], "science_python_pip_extra_args": ["--no-index"]},
    "relative_venv": {"science_python_packages": ["ipykernel"], "science_python_venv": "science-venv"},
}


@needs_ansible
def test_validation_task_accepts_workable_lists_and_rejects_the_rest(tmp_path: Path) -> None:
    validate = task_named(tasks("science-venv.yml"), "Validate the scientific Python package list")
    plays = []
    for name, variables in {**LISTS_ACCEPTED, **LISTS_REJECTED}.items():
        accepted = {"ansible.builtin.copy": {"dest": str(tmp_path / f"{name}.accepted"), "content": "ok"}}
        rejected = {"ansible.builtin.copy": {"dest": str(tmp_path / f"{name}.rejected"), "content": "{{ ansible_failed_result.msg }}"}}
        plays.append({"name": name, "hosts": "localhost", "connection": "local", "gather_facts": False,
                      "vars": {**BASE, **variables, "ansible_python_interpreter": "{{ ansible_playbook_python }}"},
                      "tasks": [{"name": name, "block": [validate, accepted], "rescue": [rejected]}]})
    playbook = tmp_path / "play.yml"
    playbook.write_text(yaml.safe_dump(plays))
    result = subprocess.run(["ansible-playbook", "-i", "localhost,", str(playbook)], capture_output=True, text=True,
                            env=_env(), cwd=ROOT, timeout=600, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    for name in LISTS_ACCEPTED:
        assert (tmp_path / f"{name}.accepted").exists(), name
        assert not (tmp_path / f"{name}.rejected").exists(), name
    for name in LISTS_REJECTED:
        assert (tmp_path / f"{name}.rejected").exists(), name
        assert not (tmp_path / f"{name}.accepted").exists(), name
    assert "ipykernel" in (tmp_path / "no_ipykernel.rejected").read_text()


def test_validation_runs_before_any_venv_task() -> None:
    names = [t["name"] for t in tasks("science-venv.yml")]
    assert names[0] == "Validate the scientific Python package list"


# --- the documented tag set previews ------------------------------------------


@needs_ansible
def test_documented_tag_set_previews_with_the_example_file() -> None:
    result = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", "playbooks/site.yml", "-e", "@examples/python-research.yml",
         "--tags", "packages,python,science", "--list-tasks"],
        cwd=ROOT, env=_env(), capture_output=True, text=True, timeout=600, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    # The science tasks are included by the update transaction, which
    # --list-tasks shows as one task; managed-updates.yml says which tags and
    # profiles reach the science unit.
    assert "Run workstation software update transaction" in result.stdout
    unit = task_named(tasks("managed-updates.yml"), "Include scientific Python and data-science tools")
    assert unit["ansible.builtin.include_tasks"]["file"] == "science-tools.yml"
    assert unit["when"][0] == "workstation_profile in ['desktop', 'workspace']"
    assert "science" in unit["tags"]
    assert EXAMPLE["workstation_profile"] in ("desktop", "workspace")
    assert "--tags packages,python,science" in GUIDE


# --- the guide, the example and the CI validation stay in step ----------------


def _slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def test_guide_names_only_real_role_variables_and_every_profile_variable() -> None:
    named = set(re.findall(r"`(science_python_[a-z_]+)`", GUIDE))
    assert not sorted(named - set(DEFAULTS)), "the guide names variables the role does not define"
    assert {k for k in DEFAULTS if k.startswith("science_python_")} <= named


def test_guide_links_resolve() -> None:
    for target in re.findall(r"\]\(([^)\s]+)\)", GUIDE):
        if re.match(r"^[a-z]+:", target):
            continue
        path_part, _, anchor = target.partition("#")
        dest = (ROOT / "docs" / path_part).resolve() if path_part else ROOT / "docs/python-research-profile.md"
        assert dest.exists(), target
        if anchor:
            headings = {_slug(line.lstrip("#")) for line in dest.read_text().splitlines() if line.startswith("#")}
            assert anchor in headings, target


def test_guide_lists_the_example_packages() -> None:
    assert "examples/python-research.yml" in GUIDE
    for package in EXAMPLE["science_python_packages"]:
        assert package.lower() in GUIDE.lower(), package


def test_guide_shows_the_project_commands_the_ci_job_runs() -> None:
    validator = _validator()
    for command in validator.PROJECT_COMMANDS:
        assert shlex.join(command) in GUIDE, shlex.join(command)
    assert f"~/projects/{validator.PROJECT_NAME}" in GUIDE


def test_ci_runs_the_disposable_install() -> None:
    ci = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    job = ci["research-profile"]
    assert "python scripts/validate_python_research.py --work .research-profile --report research-profile-report.md" in job["script"]
    versions = job["parallel"]["matrix"][0]["PYTHON_VERSION"]
    assert "3.14" in versions, "Ubuntu 26.04's python3 is the interpreter the role uses"
    assert "tests/test_python_research.py" in " ".join(ci["policy"]["script"])


def test_validation_playbook_refuses_the_real_venv_path() -> None:
    play = yaml.safe_load((ROOT / "tests/research_profile/apply-science-venv.yml").read_text())[0]
    guard = play["tasks"][0]["ansible.builtin.assert"]["that"]
    assert "science_python_venv != '/opt/science-venv'" in guard
    assert play["tasks"][1]["ansible.builtin.include_role"] == {"name": "developer_workstation", "tasks_from": "science-venv.yml"}
