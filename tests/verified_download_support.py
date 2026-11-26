"""Helpers shared by the verified-download tests (catalog#171).

The tests run the role's own task files, or named slices of them, through
ansible-playbook on localhost with every path under the test's temporary
directory. Nothing here targets a real host or inventory.
"""

from __future__ import annotations

import copy
import functools
import getpass
import http.server
import os
import subprocess
import sys
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = ROLE / "tasks"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
VERIFIED_DOWNLOAD = TASKS / "verified-download.yml"
CLAUDE_RELEASE = TASKS / "claude-code-release.yml"

_BECOME_KEYS = ("become", "become_user")


def role_tasks(task_file: Path, first: str, last: str | None = None) -> list[dict[str, Any]]:
    """The tasks of `task_file` from the one named `first` to the one named `last`.

    Includes are pointed at the role's tasks directory, and privilege changes
    are dropped: the test already runs as the user whose home it writes to.
    """
    tasks = yaml.safe_load(task_file.read_text())
    names = [task.get("name") for task in tasks]
    for name in (first, last):
        if name is not None and name not in names:
            raise AssertionError(f"{task_file.name} has no task named {name!r}")
    start = names.index(first)
    end = names.index(last) if last is not None else start
    selected = []
    for task in copy.deepcopy(tasks[start : end + 1]):
        for key in _BECOME_KEYS:
            task.pop(key, None)
        include = task.get("ansible.builtin.include_tasks")
        if isinstance(include, str):
            task["ansible.builtin.include_tasks"] = str(TASKS / include)
        elif isinstance(include, dict):
            include["file"] = str(TASKS / include["file"])
            include.pop("apply", None)
        selected.append(task)
    return selected


def include(task_file: Path, **task_vars: Any) -> dict[str, Any]:
    return {
        "name": f"Include {task_file.name}",
        "ansible.builtin.include_tasks": str(task_file),
        "vars": task_vars,
    }


def run_playbook(
    tmp_path: Path,
    tasks: Sequence[Mapping[str, Any]],
    play_vars: Mapping[str, Any],
    *,
    gather_facts: bool = False,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    """Run `tasks` on localhost; variables go in a file, never on the command line."""
    work = tmp_path / "ansible"
    work.mkdir(exist_ok=True)
    playbook = work / f"play-{len(list(work.glob('play-*.yml')))}.yml"
    play: dict[str, Any] = {
        "name": "Verified download test",
        "hosts": "localhost",
        "gather_facts": gather_facts,
        "vars": {"workstation_update_artifact_cache_dir": str(tmp_path / "cache"), **play_vars},
        "tasks": list(tasks),
    }
    if gather_facts:
        play["gather_subset"] = ["!all"]
    playbook.write_text(yaml.safe_dump([play], sort_keys=False))
    env = dict(
        os.environ,
        ANSIBLE_NOCOLOR="1",
        ANSIBLE_FORCE_COLOR="0",
        ANSIBLE_LOCAL_TMP=str(work / "local-tmp"),
        ANSIBLE_REMOTE_TMP=str(work / "remote-tmp"),
        ANSIBLE_RETRY_FILES_ENABLED="0",
    )
    return subprocess.run(
        [
            "ansible-playbook", "-i", "localhost,", "-c", "local",
            "-e", f"ansible_python_interpreter={sys.executable}",
            str(playbook),
        ],
        capture_output=True, text=True, env=env, timeout=timeout, check=False,
    )


def output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def current_user(tmp_path: Path) -> dict[str, str]:
    """A dev_users entry for the user running the test, with its home in tmp_path."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {"name": getpass.getuser(), "home": str(home)}


class _RecordingHandler(http.server.SimpleHTTPRequestHandler):
    requests: list[str]

    def do_GET(self) -> None:  # noqa: N802 - the http.server interface
        self.requests.append(self.path)
        super().do_GET()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


@contextmanager
def fixture_server(root: Path) -> Iterator[tuple[str, list[str]]]:
    """Serve `root` on 127.0.0.1; yields the base URL and the paths requested."""
    requests: list[str] = []
    handler = type("Handler", (_RecordingHandler,), {"requests": requests})
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(handler, directory=str(root))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
