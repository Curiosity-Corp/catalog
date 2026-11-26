"""Secret-bearing tasks carry `no_log` (catalog#24, docs/secret-handling-audit.md).

Ansible prints a task's rendered arguments, loop items and registered result
at `-v` and above, and writes them to any configured callback log. A task that
renders a secret therefore needs `no_log: true`, whether the value reaches it
through a role variable, a fact or a registered result.

Rule 1: a task whose arguments, name, loop or vars reference a variable whose
name looks secret-like (SECRET_NAME) must set `no_log: true`, itself or on an
enclosing block. Conditions (`when`, `failed_when`, `changed_when`, `until`
and `assert`'s `that`) are exempt: Ansible prints them as written, never with
the values substituted.

Rule 2: a `slurp` of a file that typically holds a credential (SECRET_FILE)
must set `no_log: true`, because slurp returns the whole file in its result.

ALLOWED lists reviewed false positives: a name that matches SECRET_NAME but
holds no secret (a path, a vault entry name, a boolean flag). Each entry is
(task file, variable) with the reason it is safe. Adding one is a review
decision, so keep the reason specific. An entry that no longer matches
anything fails the test, so the list cannot rot.
"""

import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]

SECRET_NAME = re.compile(
    r"token|password|(^|_)pass$|passphrase|secret|psk|api_?key|private_?key"
    r"|luks_key|credential|shadow",
    re.I,
)
SECRET_FILE = re.compile(r"account\.json$|\.env$|shadow$|\.key$|credential|secret", re.I)

# Keys whose values Ansible prints unrendered, or never prints.
UNPRINTED_KEYS = {
    "when", "failed_when", "changed_when", "until", "no_log", "register",
    "notify", "listen", "tags", "block", "rescue", "always",
}

ALLOWED = {
    ("roles/developer_workstation/tasks/fleetd.yml", "fleetd_enroll_secret_env_file"):
        "path to the root-only enroll secret file; the value is checked with grep on the host and never read into the play",
    ("roles/developer_workstation/tasks/user-git-timer-entry.yml", "credentials_file"):
        "path to a root-only git-credential-store file; only the path reaches git's environment",
    ("roles/developer_workstation/tasks/ansible-pull-timer.yml", "ansible_pull_credentials_file"):
        "path to the root-only git-credential-store file the site bootstrap writes; the role stats it and the wrapper hands only the path to a get-only git helper, never reading its content into the play",
    ("roles/developer_workstation/tasks/user-git-timer-entry.yml", "credential"):
        "the literal git config key credential.helper, not a value",
    ("roles/developer_workstation/tasks/workspace-helpers.yml", "credential"):
        "the literal git config key credential.https://<host>.helper, not a value; the helper fetches tokens at git request time, never in the play",
    ("roles/developer_workstation/tasks/samsung-tv-cli.yml", "samsung_tv_key_vault_secret"):
        "the Azure Key Vault secret's name; the token is fetched by the user's CLI at run time, never by the role",
    ("roles/developer_workstation/tasks/pro-mimic-pull-status.yml", "pro_mimic_ssh_password_auth"):
        "boolean flag for sshd PasswordAuthentication, not a password",
    ("roles/developer_workstation/tasks/pro-mimic-pull-status.yml", "ssh_password_auth"):
        "status record field mirroring the boolean flag above",
    ("roles/workstation_storage/tasks/main.yml", "workstation_storage_luks_key_file"):
        "path to the LUKS key file handed to hh-storage-provision; the key itself is never read into the play",
}


class _Loader(yaml.SafeLoader):
    """SafeLoader that reads Ansible's `!unsafe` scalars as plain strings."""


_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))

TASK_FILES = sorted(ROOT.glob("roles/*/tasks/*.yml")) + sorted(ROOT.glob("roles/*/handlers/*.yml"))
JINJA = re.compile(r"\{\{(.*?)\}\}|\{%(.*?)%\}", re.S)
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _tasks(items, inherited_no_log=False):
    """Every task, with whether no_log applies to it (its own or a block's)."""
    for task in items or []:
        if not isinstance(task, dict):
            continue
        no_log = task.get("no_log", inherited_no_log) is True
        if any(key in task for key in ("block", "rescue", "always")):
            for key in ("block", "rescue", "always"):
                yield from _tasks(task.get(key), no_log)
        else:
            yield task, no_log


def _references(node):
    """Identifiers used inside Jinja expressions anywhere in node."""
    found = set()
    if isinstance(node, str):
        for match in JINJA.finditer(node):
            found |= set(IDENTIFIER.findall(match.group(1) or match.group(2) or ""))
    elif isinstance(node, dict):
        for value in node.values():
            found |= _references(value)
    elif isinstance(node, list):
        for value in node:
            found |= _references(value)
    return found


def _printed_parts(task):
    parts = {key: value for key, value in task.items() if key not in UNPRINTED_KEYS}
    for module in ("ansible.builtin.assert", "assert"):
        if isinstance(parts.get(module), dict):
            parts[module] = {k: v for k, v in parts[module].items() if k != "that"}
    return parts


def _secret_references(path):
    """(task name, variable) for every secret-like variable a task prints."""
    rel = str(path.relative_to(ROOT))
    data = yaml.load(path.read_text(), Loader=_Loader) or []
    for task, no_log in _tasks(data):
        for name in sorted(_references(_printed_parts(task))):
            if SECRET_NAME.search(name):
                yield rel, task.get("name"), name, no_log


def _slurps(path):
    data = yaml.load(path.read_text(), Loader=_Loader) or []
    for task, no_log in _tasks(data):
        module = task.get("ansible.builtin.slurp") or task.get("slurp")
        if isinstance(module, dict):
            yield task.get("name"), str(module.get("src", "")), no_log


@pytest.mark.parametrize("path", TASK_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_tasks_printing_a_secret_set_no_log(path):
    offenders = [
        f"{task!r} references {name}"
        for rel, task, name, no_log in _secret_references(path)
        if not no_log and (rel, name) not in ALLOWED
    ]
    assert not offenders, (
        "set no_log: true on these tasks, or add a reviewed (file, variable) "
        f"entry with its reason to ALLOWED: {offenders}"
    )


@pytest.mark.parametrize("path", TASK_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_slurp_of_a_credential_file_sets_no_log(path):
    offenders = [
        f"{task!r} reads {src}"
        for task, src, no_log in _slurps(path)
        if SECRET_FILE.search(src) and not no_log
    ]
    assert not offenders, f"slurp returns the whole file; set no_log: true: {offenders}"


def test_every_allow_list_entry_still_matches_a_task():
    used = {
        (rel, name)
        for path in TASK_FILES
        for rel, _task, name, no_log in _secret_references(path)
        if not no_log
    }
    stale = sorted(set(ALLOWED) - used)
    assert not stale, f"remove ALLOWED entries that no longer match a task: {stale}"


def test_the_rule_catches_a_secret_without_no_log():
    task = {"name": "Log in", "ansible.builtin.command": "tool login {{ vendor_api_token }}"}
    assert [n for n in _references(_printed_parts(task)) if SECRET_NAME.search(n)] == ["vendor_api_token"]
    assert next(_tasks([{"block": [task], "no_log": True}]))[1] is True
    assert next(_tasks([task]))[1] is False
    guarded = {"ansible.builtin.assert": {"that": ["wifi_psk | length >= 8"], "fail_msg": "too short"}}
    assert not _references(_printed_parts(guarded))
