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
import signal
import subprocess
import sys
import threading
import time
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
    stall: int | None = None,
    endpoint: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `tasks` on localhost; variables go in a file, never on the command line.

    `timeout` bounds the whole run (catalog#174). `stall`, when given, also
    fails the run once nothing under `tmp_path` has grown for that many
    seconds: a download that is slow but moving keeps going, one that has
    stopped fails early (catalog#177). Either failure names the `endpoint` it
    was reaching (a vendor host, for the network tests), the task it was in and
    the bytes written, instead of holding the CI job until its own timeout.
    """
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
    # git (the SDKMAN! checkout) has no timeout of its own: abort a transfer
    # that stays under 1 KiB/s for 30 s, so a stalled remote fails the task.
    env.setdefault("GIT_HTTP_LOW_SPEED_LIMIT", "1024")
    env.setdefault("GIT_HTTP_LOW_SPEED_TIME", "30")
    command = [
        "ansible-playbook", "-i", "localhost,", "-c", "local",
        "-e", f"ansible_python_interpreter={sys.executable}",
        str(playbook),
    ]
    # Output goes to files, not pipes: the watchdog below polls instead of
    # reading, and a long run must not block on a full pipe.
    stdout_path, stderr_path = work / f"{playbook.stem}.out", work / f"{playbook.stem}.err"
    with stdout_path.open("w") as stdout_file, stderr_path.open("w") as stderr_file:
        # Its own process group: on a failure the module processes ansible
        # forked are killed with it.
        process = subprocess.Popen(
            command, stdout=stdout_file, stderr=stderr_file, text=True, env=env,
            start_new_session=True,
        )
        failure = _watch(process, tmp_path, timeout, stall)
    stdout, stderr = stdout_path.read_text(errors="replace"), stderr_path.read_text(errors="replace")
    if failure is not None:
        raise AssertionError(_stopped_message(failure, stdout, stderr, endpoint, _tree_bytes(tmp_path)))
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


_POLL_SECONDS = 2.0


def _tree_bytes(directory: Path) -> int:
    """The bytes in the files under `directory`; files that vanish mid-walk count 0."""
    total = 0
    for root, _, files in os.walk(directory):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except FileNotFoundError:
                continue
    return total


def _watch(process: subprocess.Popen[str], tmp_path: Path, timeout: int, stall: int | None) -> str | None:
    """Wait for `process`; on a timeout or a stall, kill its group and say which."""
    started = last_growth = time.monotonic()
    size = _tree_bytes(tmp_path)
    while True:
        try:
            process.wait(timeout=_POLL_SECONDS)
            return None
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        failure = None
        if now - started >= timeout:
            failure = f"did not finish within {timeout} s"
        elif stall is not None:
            current = _tree_bytes(tmp_path)
            if current != size:
                size, last_growth = current, now
            elif now - last_growth >= stall:
                failure = f"made no progress for {stall} s (after {round(now - started)} s)"
        if failure is not None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return failure


def _stopped_message(failure: str, stdout: str, stderr: str, endpoint: str | None, written: int) -> str:
    tasks = [line for line in stdout.splitlines() if line.startswith("TASK [")]
    where = f" reaching {endpoint}" if endpoint else ""
    last = tasks[-1] if tasks else "(no task started)"
    tail = "\n".join((stdout + stderr).splitlines()[-20:])
    return (
        f"ansible-playbook {failure}{where}; it was in {last}; "
        f"{written / 1_000_000:.1f} MB written under the test directory\n{tail}"
    )


def output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def current_user(tmp_path: Path) -> dict[str, str]:
    """A dev_users entry for the user running the test, with its home in tmp_path."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    try:
        name = getpass.getuser()
    except OSError:
        # A CI runner's uid may have no passwd entry and no USER; the name is
        # only passed through (become is dropped), so the uid stands in.
        name = str(os.getuid())
    return {"name": name, "home": str(home)}


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
