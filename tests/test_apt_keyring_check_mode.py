"""Check-mode preview of vendor APT keyrings (catalog#164).

On a machine the role has never run on, `--check --diff` used to stop at the
first vendor keyring: check mode does not create the staging directory, so
get_url had nowhere to download to. apt-keyring.yml now reports the keyring as
"would change" in that case and skips the download and inspection.

The other half of the contract matters more: a real apply must behave exactly
as before. These tests pin that the check-mode tasks can only run in check
mode, that the download/verify/install block always runs outside it, and that
the block still holds the same tasks with the same file modes, ownership,
force-refresh and fingerprint checks.
"""

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "roles/developer_workstation/tasks"
KEYRING = TASKS / "apt-keyring.yml"
BLOCK_NAME = "Download, verify and install the APT keyring for {{ apt_keyring_name }}"

# The real-apply task sequence, in order, as it was before catalog#164.
REAL_APPLY_SEQUENCE = [
    ("Ensure APT keyring directories exist", "ansible.builtin.file"),
    ("Download vendor APT signing key for {{ apt_keyring_name }}", "ansible.builtin.get_url"),
    ("Detect ASCII armor in the downloaded key for {{ apt_keyring_name }}", "ansible.builtin.command"),
    (
        "Require an armored download for an armored keyring path for {{ apt_keyring_name }}",
        "ansible.builtin.assert",
    ),
    ("Dearmor the downloaded key for {{ apt_keyring_name }}", "ansible.builtin.command"),
    ("Select the keyring content to install for {{ apt_keyring_name }}", "ansible.builtin.set_fact"),
    ("Inspect the keyring content for {{ apt_keyring_name }}", "ansible.builtin.command"),
    (
        "Require a valid key before replacing the installed keyring for {{ apt_keyring_name }}",
        "ansible.builtin.assert",
    ),
    ("Install the APT keyring for {{ apt_keyring_name }}", "ansible.builtin.copy"),
]
MODULES = {module for _, module in REAL_APPLY_SEQUENCE} | {
    "ansible.builtin.stat",
    "ansible.builtin.debug",
}


def _tasks() -> list[dict]:
    return yaml.safe_load(KEYRING.read_text())


def _module(task: dict) -> str:
    found = [key for key in task if key in MODULES]
    assert len(found) == 1, f"cannot tell the module of {task.get('name')!r}"
    return found[0]


def _conjuncts(task: dict) -> list[str]:
    when = task.get("when", [])
    return [when] if isinstance(when, str) else list(when)


def _block() -> dict:
    blocks = [task for task in _tasks() if task.get("name") == BLOCK_NAME]
    assert len(blocks) == 1, "apt-keyring.yml must have exactly one install block"
    return blocks[0]


def _real_apply_tasks() -> list[dict]:
    """The tasks a real apply runs: check-mode-only tasks out, the block inlined."""
    tasks = []
    for task in _tasks():
        if "block" in task:
            tasks.extend(task["block"])
        elif "ansible_check_mode" not in _conjuncts(task):
            tasks.append(task)
    return tasks


def _evaluate(condition: str, **variables) -> bool:
    # StrictUndefined: an attribute of a skipped stat result would raise, so
    # this also proves the condition never touches it outside check mode.
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    rendered = env.from_string("{% if " + condition + " %}yes{% else %}no{% endif %}").render(**variables)
    return rendered == "yes"


def test_check_mode_tasks_can_only_run_in_check_mode() -> None:
    for task in _tasks():
        if "block" in task or task["name"] == "Ensure APT keyring directories exist":
            continue
        assert "ansible_check_mode" in _conjuncts(task), (
            f"{task['name']!r} sits outside the install block, so it must be "
            "gated on ansible_check_mode or it would change a real apply"
        )
        assert "notify" not in task, f"{task['name']!r} must not notify a handler"


def test_install_block_always_runs_in_a_real_apply() -> None:
    (condition,) = _conjuncts(_block())
    skipped_stat = {"changed": False, "skipped": True, "skip_reason": "Conditional result was False"}
    assert _evaluate(condition, ansible_check_mode=False, _apt_keyring_staging=skipped_stat)
    assert _evaluate(condition, ansible_check_mode=True, _apt_keyring_staging={"stat": {"exists": True}})
    assert not _evaluate(condition, ansible_check_mode=True, _apt_keyring_staging={"stat": {"exists": False}})


def test_real_apply_task_sequence_is_unchanged() -> None:
    tasks = _real_apply_tasks()
    assert [(task["name"], _module(task)) for task in tasks] == REAL_APPLY_SEQUENCE
    by_name = {task["name"]: task for task in tasks}

    dirs = by_name["Ensure APT keyring directories exist"]
    assert dirs["ansible.builtin.file"] == {
        "path": "{{ item }}", "state": "directory", "owner": "root", "group": "root", "mode": "0755",
    }
    download = by_name["Download vendor APT signing key for {{ apt_keyring_name }}"]
    assert download["ansible.builtin.get_url"] == {
        "url": "{{ apt_keyring_url }}",
        "dest": "{{ apt_keyring_staging_dir }}/{{ apt_keyring_name }}.download",
        "mode": "0644",
        "force": True,
        "timeout": 30,
    }
    assert (download["retries"], download["delay"]) == (3, 5)
    install = by_name["Install the APT keyring for {{ apt_keyring_name }}"]
    assert install["ansible.builtin.copy"] == {
        "src": "{{ _apt_keyring_candidate }}",
        "dest": "{{ apt_keyring_dest }}",
        "remote_src": True,
        "owner": "root",
        "group": "root",
        "mode": "0644",
    }
    assert install["notify"] == "Update apt cache"
    verify = by_name["Require a valid key before replacing the installed keyring for {{ apt_keyring_name }}"]
    that = " ".join(verify["ansible.builtin.assert"]["that"])
    assert "_apt_keyring_info.rc == 0" in that
    assert "^fpr:.*:" in that and "apt_keyring_fingerprint" in that
    for task in tasks:
        assert "check_mode" not in task, f"{task['name']!r} must not change its check-mode behaviour"


def test_check_mode_preview_on_a_clean_machine(tmp_path: Path) -> None:
    """Run the include in check mode with a staging directory that does not exist.

    Every path is under tmp_path and the run is --check, so nothing on the host
    running the test is read or written beyond tmp_path.
    """
    staging = tmp_path / "staging"
    dest = tmp_path / "keyrings" / "vendor-archive-keyring.gpg"
    playbook = tmp_path / "preview.yml"
    playbook.write_text(textwrap.dedent(f"""\
        - name: Preview one vendor keyring
          hosts: localhost
          gather_facts: false
          vars:
            apt_keyring_staging_dir: {staging}
          handlers:
            - name: Update apt cache
              ansible.builtin.debug:
                msg: handler
          tasks:
            - name: Converge vendor APT keyring
              ansible.builtin.include_tasks: {KEYRING}
              vars:
                apt_keyring_name: vendor
                apt_keyring_url: https://keys.invalid/vendor.gpg
                apt_keyring_dest: {dest}
    """))
    env = dict(
        os.environ,
        ANSIBLE_NOCOLOR="1",
        ANSIBLE_FORCE_COLOR="0",
        ANSIBLE_LOCAL_TMP=str(tmp_path / "ansible-local"),
        ANSIBLE_REMOTE_TMP=str(tmp_path / "ansible-remote"),
    )
    result = subprocess.run(
        [
            "ansible-playbook", "-i", "localhost,", "-c", "local",
            "-e", f"ansible_python_interpreter={sys.executable}",
            "--check", "--diff", str(playbook),
        ],
        capture_output=True, text=True, env=env, timeout=300, check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Would download https://keys.invalid/vendor.gpg" in output, output
    download = re.search(r"TASK \[Download vendor APT signing key for vendor\] \**\n(\S+)", output)
    assert download and download.group(1) == "skipping:", output
    assert re.search(r"changed=[1-9]\d*\s.*failed=0\s", output), output
    assert not staging.exists() and not dest.parent.exists()


def test_mongodb_channel_discovery_runs_in_check_mode() -> None:
    for name in ("repos.yml", "repos-workspace.yml"):
        tasks = yaml.safe_load((TASKS / name).read_text())
        (discover,) = [t for t in tasks if t.get("name") == "Discover newest MongoDB APT channel"]
        assert set(discover) == {"name", "ansible.builtin.uri", "register", "changed_when", "check_mode"}
        assert "method" not in discover["ansible.builtin.uri"], "only a read-only GET may run in check mode"
        assert discover["check_mode"] is False and discover["changed_when"] is False
