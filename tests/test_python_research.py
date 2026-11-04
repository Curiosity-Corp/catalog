"""The Python research baseline is a real, validated variable set, not a code edit.

`science_python_packages` replaces the hardcoded package list in
science-tools.yml. The tests execute the role's own validation task with
synthetic lists, check that the example file only uses real role variables and
keeps ipykernel, preview the documented tag set, and run the opt-in
release-source filter for slow links.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
EXAMPLE = yaml.safe_load((ROOT / "examples/python-research.yml").read_text())

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")


def tasks(name: str) -> list[dict]:
    return yaml.safe_load((ROLE / "tasks" / name).read_text())


def task_named(items: list[dict], name: str) -> dict:
    return next(t for t in items if t["name"] == name)


def _run(playbook: Path, plays: list[dict]) -> subprocess.CompletedProcess:
    playbook.write_text(yaml.safe_dump(plays))
    env = {**os.environ, "ANSIBLE_LOCALHOST_WARNING": "False", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False",
           "ANSIBLE_ROLES_PATH": str(ROOT / "roles"), "ANSIBLE_NOCOLOR": "1"}
    env.pop("ANSIBLE_STDOUT_CALLBACK", None)
    return subprocess.run(["ansible-playbook", "-i", "localhost,", str(playbook)], capture_output=True, text=True,
                          env=env, cwd=ROOT, timeout=600, check=False)


# --- the variables ------------------------------------------------------------


def test_example_uses_only_real_variables_and_keeps_ipykernel() -> None:
    unknown = sorted(set(EXAMPLE) - set(DEFAULTS))
    assert not unknown, unknown
    assert any(re.match(r"ipykernel\b", p) for p in EXAMPLE["science_python_packages"])
    assert EXAMPLE["workstation_profile"] == "workspace"
    assert len(EXAMPLE["science_python_packages"]) < len(DEFAULTS["science_python_packages"]), "the example is the small baseline"


def test_default_package_list_is_unchanged_so_existing_fleets_see_no_difference() -> None:
    packages = DEFAULTS["science_python_packages"]
    for expected in ("numpy", "scipy", "pandas", "matplotlib", "ipykernel", "ipywidgets", "qutip", "astropy", "pybullet"):
        assert expected in packages
    assert len(packages) == 26
    assert DEFAULTS["science_python_pip_extra_args"] == ""
    assert set(DEFAULTS["science_python_upgrade_packages"]) <= set(packages)


def test_both_pip_tasks_read_the_variables_and_no_package_is_hardcoded() -> None:
    science = tasks("science-tools.yml")
    install = task_named(science, "Install/upgrade scientific Python packages into venv  # noqa: args package-latest".split("  #")[0])
    upgrade = task_named(science, "Upgrade all pip packages in science-venv (periodic)")
    assert install["ansible.builtin.pip"]["name"] == "{{ science_python_packages }}"
    assert upgrade["ansible.builtin.pip"]["name"] == "{{ science_python_upgrade_packages }}"
    for step in (install, upgrade):
        assert step["ansible.builtin.pip"]["extra_args"] == "{{ science_python_pip_extra_args }}"
        assert step["ansible.builtin.pip"]["virtualenv"] == "/opt/science-venv"


# --- the validation task, executed -------------------------------------------

LISTS_ACCEPTED = {
    "small": {"science_python_packages": ["numpy", "ipykernel"], "science_python_upgrade_packages": []},
    "specifiers": {"science_python_packages": ["numpy>=2", "IPyKernel==6.29.5", "ipykernel_x"], "science_python_upgrade_packages": []},
    "extras": {"science_python_packages": ["ipykernel[all]>=6"], "science_python_upgrade_packages": ["ipykernel"]},
}
LISTS_REJECTED = {
    "empty": {"science_python_packages": [], "science_python_upgrade_packages": []},
    "no_ipykernel": {"science_python_packages": ["numpy", "scipy"], "science_python_upgrade_packages": []},
    "lookalike": {"science_python_packages": ["ipykernel-helpers", "numpy"], "science_python_upgrade_packages": []},
    "string": {"science_python_packages": "numpy ipykernel", "science_python_upgrade_packages": []},
    "upgrade_not_list": {"science_python_packages": ["ipykernel"], "science_python_upgrade_packages": "ipykernel"},
}


@needs_ansible
def test_validation_task_accepts_workable_lists_and_rejects_the_rest(tmp_path: Path) -> None:
    validate = task_named(tasks("science-tools.yml"), "Validate the scientific Python package list")
    plays = []
    for name, variables in {**LISTS_ACCEPTED, **LISTS_REJECTED}.items():
        accepted = {"ansible.builtin.copy": {"dest": str(tmp_path / f"{name}.accepted"), "content": "ok"}}
        rejected = {"ansible.builtin.copy": {"dest": str(tmp_path / f"{name}.rejected"), "content": "{{ ansible_failed_result.msg }}"}}
        plays.append({"hosts": "localhost", "connection": "local", "gather_facts": False, "vars": variables,
                      "tasks": [{"name": name, "block": [validate, accepted], "rescue": [rejected]}]})
    result = _run(tmp_path / "play.yml", plays)
    assert result.returncode == 0, result.stdout + result.stderr
    for name in LISTS_ACCEPTED:
        assert (tmp_path / f"{name}.accepted").exists(), name
        assert not (tmp_path / f"{name}.rejected").exists(), name
    for name in LISTS_REJECTED:
        assert (tmp_path / f"{name}.rejected").exists(), name
        assert not (tmp_path / f"{name}.accepted").exists(), name
    assert "ipykernel" in (tmp_path / "no_ipykernel.rejected").read_text()


@needs_ansible
def test_validation_runs_before_any_venv_task() -> None:
    science = tasks("science-tools.yml")
    names = [t["name"] for t in science]
    assert names[0] == "Validate the scientific Python package list"
    assert names.index("Validate the scientific Python package list") < names.index("Install/upgrade scientific Python packages into venv  # noqa: args package-latest".split("  #")[0])


# --- the documented tag set previews ------------------------------------------


@needs_ansible
def test_documented_tag_set_previews_with_the_example_file() -> None:
    env = dict(os.environ, ANSIBLE_ROLES_PATH=str(ROOT / "roles"), ANSIBLE_NOCOLOR="1")
    env.pop("ANSIBLE_STDOUT_CALLBACK", None)
    result = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", "playbooks/site.yml", "-e", "@examples/python-research.yml",
         "--tags", "packages,python,science", "--list-tasks"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    # The science tasks are included by the update transaction, which --list-tasks
    # shows as one task; the transaction table says which tags reach the unit.
    assert "Run workstation software update transaction" in result.stdout
    assert "Include ansible-pull timer" not in result.stdout
    managed = (ROLE / "tasks/managed-updates.yml").read_text()
    assert "file: science-tools.yml" in managed
    unit = task_named(yaml.safe_load(managed), "Include scientific Python and data-science tools")
    assert unit["when"][0] == "workstation_profile in ['desktop', 'workspace']"
    assert EXAMPLE["workstation_profile"] in ("desktop", "workspace")


# --- opt-in: skip release lookups for sources whose feature is off ------------

SOURCES = {"alpha": "org/alpha", "beta": "org/beta", "gamma": "org/gamma"}
CONTRACTS = {
    "alpha": {"enabled_when": "feature_alpha"},
    "beta": {"enabled_when": "feature_beta"},
    "gamma": {},
}


@needs_ansible
@pytest.mark.parametrize(
    ("skip", "expected"),
    [(False, ["alpha", "beta", "gamma"]), (True, ["beta", "gamma"])],
)
def test_release_source_filter_is_off_by_default_and_drops_disabled_features_when_asked(tmp_path: Path, skip: bool, expected: list[str]) -> None:
    releases = tasks("latest-releases.yml")
    find = task_named(releases, "Find release sources whose feature flag is off")
    select = task_named(releases, "Select the release sources to query")
    record = {"ansible.builtin.copy": {"dest": str(tmp_path / "selected.txt"), "content": "{{ _latest_release_sources.keys() | list | sort | join(',') }}"}}
    play = {"hosts": "localhost", "connection": "local", "gather_facts": False,
            "vars": {"latest_github_repositories": SOURCES, "latest_release_contracts": CONTRACTS,
                     "feature_alpha": False, "feature_beta": True, "latest_release_skip_disabled_sources": skip},
            "tasks": [find, select, record]}
    result = _run(tmp_path / "play.yml", [play])
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "selected.txt").read_text() == ",".join(expected)


def test_release_loops_use_the_filtered_sources() -> None:
    text = (ROLE / "tasks/latest-releases.yml").read_text()
    assert text.count("_latest_release_sources | dict2items") == 2
    assert "_latest_release_sources.keys()" in text
    assert "latest_github_repositories | dict2items\n         | rejectattr('key', 'in', _latest_release_sources_off" in text
    assert DEFAULTS["latest_release_skip_disabled_sources"] is False


# --- the guides name real variables, files and anchors --------------------------

GUIDES = ("python-research-profile.md", "accessibility-and-localization.md", "maintenance-summary.md", "low-bandwidth-and-offline.md")
DOC_VARIABLE = re.compile(r"`((?:keyboard|locale|science_python|latest_release|minimal_desktop_display|workstation_update_summary)[a-z_]*)")


def _slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


@pytest.mark.parametrize("guide", GUIDES)
def test_guides_name_only_real_role_variables(guide: str) -> None:
    text = (ROOT / "docs" / guide).read_text()
    named = set(DOC_VARIABLE.findall(text))
    assert named or guide == "maintenance-summary.md", guide
    assert not sorted(named - set(DEFAULTS)), f"{guide} names variables the role does not define"


@pytest.mark.parametrize("guide", GUIDES)
def test_guide_anchors_resolve(guide: str) -> None:
    text = (ROOT / "docs" / guide).read_text()
    for target in re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", text):
        path_part, _, anchor = target.partition("#")
        if re.match(r"^[a-z]+:", target) or not anchor:
            continue
        dest = (ROOT / "docs" / path_part).resolve() if path_part else ROOT / "docs" / guide
        headings = {_slug(line.lstrip("#")) for line in dest.read_text().splitlines() if line.startswith("#")}
        assert anchor in headings, f"{guide}: no heading for {target}"


def test_python_profile_guide_matches_the_example_and_the_evidence() -> None:
    guide = (ROOT / "docs/python-research-profile.md").read_text()
    assert "examples/python-research.yml" in guide
    for package in EXAMPLE["science_python_packages"]:
        assert package in guide, package
    evidence = (ROOT / "docs/evidence/python-research-sample-install.md").read_text()
    assert "packages requested: " + " ".join(EXAMPLE["science_python_packages"]) in evidence, "the recorded run used the example list"
    assert "ipykernel" in guide and "uv venv" in guide


def test_low_bandwidth_guide_is_honest_about_what_is_missing() -> None:
    text = " ".join((ROOT / "docs/low-bandwidth-and-offline.md").read_text().split())
    assert "Resumable downloads" in text and "not implemented" in text
    assert "interrupted-update recovery" in text
    assert "latest_release_skip_disabled_sources" in text


def test_readme_and_thin_client_guide_link_the_new_pages() -> None:
    readme = (ROOT / "README.md").read_text()
    for target in GUIDES:
        assert f"docs/{target}" in readme, target
    thin = (ROOT / "docs/quickstarts/thin-client-and-shared-lab.md").read_text()
    assert "../accessibility-and-localization.md" in thin and "keyboard_layout" in thin


def test_validation_script_reads_the_same_example_file() -> None:
    script = (ROOT / "scripts/validate_python_research.py").read_text()
    assert 'examples/python-research.yml' in script and "science_python_packages" in script
