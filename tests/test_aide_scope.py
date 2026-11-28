"""The AIDE scope the developer_workstation role deploys (catalog#185).

The nightly dailyaidecheck on dm313red read the tmpfs /tmp (25G of agent
scratch, mostly swapped out), ran 7-8 h and peaked at 25-29 GiB of swap. These
checks pin the fix: the role deploys zz_workstation_excludes with
non-recursive prunes for the RAM-backed and volatile trees, bounds the daily
check with a systemd drop-in that applies on every host (not only before the
database exists), and keeps the aide-config-check CI job that proves the
rules with the distribution's own aide --config-check and --path-check.
"""

import re
from pathlib import Path

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
SECURITY = ROLE / "tasks/security.yml"
EXCLUDES_SRC = ROLE / "files/aide/zz_workstation_excludes"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
CI = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
CHECK_SCRIPT = ROOT / "scripts/aide_config_check.sh"

VOLATILE_TREES = ("/tmp", "/var/tmp", "/dev/shm", "/run/user")
DROP_IN = "/etc/systemd/system/dailyaidecheck.service.d/20-curiosity-resources.conf"


def _tasks() -> list[dict]:
    return yaml.safe_load(SECURITY.read_text())


def _task_writing(dest: str) -> dict:
    found = [
        task
        for task in _tasks()
        if isinstance(task.get("ansible.builtin.copy"), dict)
        and task["ansible.builtin.copy"].get("dest") == dest
    ]
    assert len(found) == 1, f"expected one task writing {dest}, found {len(found)}"
    return found[0]


def _rendered_excludes() -> str:
    """The file the role puts at /etc/aide/aide.conf.d/zz_workstation_excludes."""
    copy = _task_writing("/etc/aide/aide.conf.d/zz_workstation_excludes")["ansible.builtin.copy"]
    assert copy.get("src") == "aide/zz_workstation_excludes", copy
    assert "content" not in copy, "the rules live in files/, where the CI job reads them"
    assert copy.get("mode") == "0644" and copy.get("owner") == "root", copy
    return (ROLE / "files" / copy["src"]).read_text()


def _rules(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def test_excludes_prune_every_volatile_tree_non_recursively() -> None:
    rules = _rules(_rendered_excludes())
    for tree in VOLATILE_TREES:
        rule = f"-{tree}$ d"
        assert rule in rules, f"zz_workstation_excludes must prune {tree} with {rule!r}"


def test_excludes_keep_the_earlier_prunes_and_re_includes() -> None:
    rules = _rules(_rendered_excludes())
    for rule in (
        "-/home/[^/]+$ d",
        "-/var/lib/docker$ d",
        "-/var/lib/containers$ d",
        "-/var/lib/snapd$ d",
        "-/snap$ d",
        "-/swap\\.img$ f",
        "/home/[^/]+/\\.ssh/authorized_keys$ f R",
    ):
        assert rule in rules, f"zz_workstation_excludes lost {rule!r}"


def test_excludes_follow_the_syntax_aide_accepts() -> None:
    """security.yml's verified syntax notes: "-" prunes, "/" first, a type letter."""
    for rule in _rules(_rendered_excludes()):
        assert not rule.startswith("!"), f"'!' still descends into the tree, use '-': {rule}"
        regex = rule[1:] if rule.startswith("-") else rule
        assert regex.startswith("/"), f"an AIDE rule must start with a literal '/': {rule}"
        if rule.startswith("-"):
            assert re.fullmatch(r"-\S+ [fdlcbps](,[fdlcbps])*", rule), f"negative rule needs a type letter: {rule}"


def test_daily_check_drop_in_is_rendered_with_its_bounds() -> None:
    task = _task_writing(DROP_IN)
    assert "when" not in task, "the bounds must apply on hosts whose database already exists"
    assert task.get("notify") == "Reload systemd", task
    content = task["ansible.builtin.copy"]["content"]
    rendered = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(content).render(**DEFAULTS)
    lines = rendered.splitlines()
    assert "[Service]" in lines
    for setting in (
        "Nice=19",
        "IOSchedulingClass=idle",
        f"MemoryHigh={DEFAULTS['aide_check_memory_high']}",
        f"MemorySwapMax={DEFAULTS['aide_check_memory_swap_max']}",
    ):
        assert setting in lines, f"{DROP_IN} is missing {setting}"
    assert not any(line.startswith("CPUSchedulingPolicy=") for line in lines), (
        "SCHED_IDLE can starve the check on a busy workstation; Nice=19 is the bound"
    )
    assert re.fullmatch(r"\d+[KMGT]?|infinity", str(DEFAULTS["aide_check_memory_high"]))
    assert re.fullmatch(r"\d+[KMGT]?|infinity", str(DEFAULTS["aide_check_memory_swap_max"]))


def test_drop_in_directory_exists_on_every_host() -> None:
    dirs = [
        task
        for task in _tasks()
        if isinstance(task.get("ansible.builtin.file"), dict)
        and task["ansible.builtin.file"].get("path") == "/etc/systemd/system/dailyaidecheck.service.d"
    ]
    assert len(dirs) == 1 and "when" not in dirs[0], dirs


def test_ci_proves_the_rules_with_aide() -> None:
    job = CI.get("aide-config-check")
    assert isinstance(job, dict), ".gitlab-ci.yml must keep the aide-config-check job"
    assert "@sha256:" in job["image"], job["image"]
    assert any("scripts/aide_config_check.sh" in line for line in job["script"]), job["script"]
    script = CHECK_SCRIPT.read_text()
    assert "--config-check" in script and "--path-check" in script
    assert "files/aide/zz_workstation_excludes" in script
    for tree in VOLATILE_TREES:
        assert tree in script, f"scripts/aide_config_check.sh does not path-check {tree}"
    changes = {
        path for rule in job.get("rules", []) for path in rule.get("changes", [])
    }
    for path in (
        "roles/developer_workstation/files/aide/**/*",
        "roles/developer_workstation/tasks/security.yml",
        "scripts/aide_config_check.sh",
    ):
        assert path in changes, f"aide-config-check must run when {path} changes"
