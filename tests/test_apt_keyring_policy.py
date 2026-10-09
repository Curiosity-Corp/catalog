"""Regression checks for vendor APT keyring refresh.

Vendor signing keys rotate (GitHub CLI's 23F3D4EA75716059 expired 2026-09-05
and was replaced by 5612B36462313325). A key that is fetched once behind a
`creates:` guard is never refreshed, so `apt update` then fails with
NO_PUBKEY on every host that kept the old file.
"""

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "roles/developer_workstation/tasks"
KEYRING_DIRS = ("/usr/share/keyrings/", "/etc/apt/keyrings/")
INCLUDE = "apt-keyring.yml"
# authd manages its own armored-key + keyring pair and already refreshes it
# with `force: true`; it is the only other place allowed to dearmor.
OWN_REFRESH_FLOWS = {"apt-keyring.yml", "authd-repo.yml"}


class _AnsibleLoader(yaml.SafeLoader):
    """Safe loader that tolerates Ansible's `!unsafe` scalar tag."""


_AnsibleLoader.add_constructor(
    "!unsafe", lambda loader, node: loader.construct_scalar(node)
)


def _load(path: Path):
    return yaml.load(path.read_text(), Loader=_AnsibleLoader)


def _walk(tasks: list[dict]):
    for task in tasks or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _walk(task.get(key))


def _all_tasks() -> list[tuple[str, dict]]:
    found = []
    for path in sorted(TASKS.glob("*.yml")):
        data = _load(path)
        found.extend((path.name, task) for task in _walk(data))
    return found


def _module_args(task: dict) -> dict:
    for module in (
        "ansible.builtin.shell",
        "ansible.builtin.command",
        "ansible.builtin.get_url",
        "ansible.builtin.copy",
    ):
        if module in task:
            args = task[module]
            return args if isinstance(args, dict) else {"cmd": args}
    return {}


def _includes(filename: str) -> list[dict]:
    data = _load(TASKS / filename)
    return [
        task["vars"]
        for task in _walk(data)
        if task.get("ansible.builtin.include_tasks") == INCLUDE
    ]


def test_no_vendor_keyring_task_is_creates_guarded() -> None:
    for filename, task in _all_tasks():
        args = _module_args(task)
        creates = str(args.get("creates", ""))
        assert not creates.startswith(KEYRING_DIRS), (
            f"{filename}: '{task.get('name')}' guards a keyring with creates:; "
            "the key would never be refreshed after a vendor rotation"
        )


def test_no_shell_pipeline_dearmors_outside_the_refresh_flows() -> None:
    for filename, task in _all_tasks():
        if filename in OWN_REFRESH_FLOWS:
            continue
        rendered = yaml.safe_dump(task)
        assert "dearmor" not in rendered, (
            f"{filename}: '{task.get('name')}' dearmors a key itself; use "
            f"include_tasks: {INCLUDE} so the keyring converges on every run"
        )


def test_keyring_downloads_always_refetch() -> None:
    for filename, task in _all_tasks():
        args = _module_args(task)
        if "ansible.builtin.get_url" not in task:
            continue
        if not str(args.get("dest", "")).startswith(KEYRING_DIRS):
            continue
        assert args.get("force") is True, (
            f"{filename}: '{task.get('name')}' downloads straight into a "
            "keyring path without force: true, so it is never refreshed"
        )


def test_converge_flow_is_loud_retried_and_notifies_apt_cache() -> None:
    tasks = list(_walk(_load(TASKS / INCLUDE)))

    download = next(t for t in tasks if "ansible.builtin.get_url" in t)
    args = download["ansible.builtin.get_url"]
    assert args["force"] is True
    assert "retries" in download and "delay" in download and "until" in download
    # A failed fetch must fail the run, never keep a stale key silently.
    assert "ignore_errors" not in download
    assert "failed_when" not in download

    # Content is validated before the installed keyring is replaced.
    names = [t["name"] for t in tasks]
    validate = next(i for i, n in enumerate(names) if n.startswith("Require a valid key"))
    install = next(i for i, n in enumerate(names) if n.startswith("Install the APT keyring"))
    assert validate < install
    assert "ansible.builtin.assert" in tasks[validate]

    copy = tasks[install]
    assert copy["ansible.builtin.copy"]["remote_src"] is True
    assert copy["notify"] == "Update apt cache"

    # Armored sources are normalised, binary sources are installed as-is.
    text = (TASKS / INCLUDE).read_text()
    assert "--dearmor" in text
    assert "BEGIN PGP PUBLIC KEY BLOCK" in text


def test_every_signed_by_keyring_converges_through_the_shared_flow() -> None:
    for filename in ("repos.yml", "repos-workspace.yml", "repos-thin-client.yml"):
        text = (TASKS / filename).read_text()
        signed_by = {
            match
            for match in re.findall(r"signed-by=(/[^\] \n]+)", text)
            if match.startswith(KEYRING_DIRS)
        }
        converged = {v["apt_keyring_dest"] for v in _includes(filename)}
        assert signed_by == converged, (
            f"{filename}: signed-by keyrings {sorted(signed_by - converged)} are "
            f"not refreshed by {INCLUDE}; {sorted(converged - signed_by)} are "
            "refreshed but never referenced"
        )


def test_keyring_destinations_and_sources_are_stable() -> None:
    desktop = {v["apt_keyring_name"]: v["apt_keyring_dest"] for v in _includes("repos.yml")}
    assert desktop == {
        "github-cli": "/usr/share/keyrings/githubcli-archive-keyring.gpg",
        "microsoft": "/usr/share/keyrings/microsoft-archive-keyring.gpg",
        "google-cloud": "/usr/share/keyrings/cloud-google-archive-keyring.gpg",
        "mongodb": "/usr/share/keyrings/mongodb-archive-keyring.gpg",
        "google-chrome": "/usr/share/keyrings/google-chrome-archive-keyring.gpg",
        "1password": "/usr/share/keyrings/1password-archive-keyring.gpg",
        "brave-browser": "/usr/share/keyrings/brave-browser-archive-keyring.gpg",
        "nodesource": "/etc/apt/keyrings/nodesource.gpg",
        "openziti": "/usr/share/keyrings/openziti-archive-keyring.gpg",
        "mozilla": "/etc/apt/keyrings/packages.mozilla.org.asc",
    }

    github = {
        filename: next(
            v for v in _includes(filename) if v["apt_keyring_name"] == "github-cli"
        )
        for filename in ("repos.yml", "repos-workspace.yml")
    }
    for filename, spec in github.items():
        assert (
            spec["apt_keyring_url"]
            == "https://cli.github.com/packages/githubcli-archive-keyring.gpg"
        ), filename

    # Pinned trust anchors that existed before the refresh must still be
    # enforced against the downloaded key.
    for filename in ("repos.yml", "repos-thin-client.yml"):
        pinned = {
            v["apt_keyring_name"]: v.get("apt_keyring_fingerprint")
            for v in _includes(filename)
        }
        assert pinned["nodesource"] == "{{ nodesource_key_fingerprint }}"
        assert pinned["openziti"] == "{{ openziti_key_fingerprint }}"
    assert {
        v["apt_keyring_name"]: v.get("apt_keyring_fingerprint")
        for v in _includes("repos.yml")
    }["mongodb"] == "{{ latest_mongodb_key_fingerprint }}"
