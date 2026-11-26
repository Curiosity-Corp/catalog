"""Provider-neutral workspace credential helper (catalog#29, docs/workspace-providers.md).

The role's own tasks install the helper (tasks/workspace-helpers.yml, rendered
by ansible-playbook on localhost with every path under pytest's tmp_path). Real
git then clones from a loopback HTTP server that demands a token, and the
helper gets its token from a stand-in workspace service on the same loopback
server, through a stub `coder` command that asks it the way the real client
asks its deployment. So token acquisition succeeds and fails here without a
workspace service, a network or a real host, and each failure must give a
clear message and never print the token.

Earlier design: Curiosity-Corp/catalog!176 (closed, not merged), whose
variables and stub-binary cases this keeps.
"""

from __future__ import annotations

import base64
import functools
import http.server
import os
import re
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from verified_download_support import DEFAULTS, ROLE, TASKS, output, role_tasks, run_playbook

HELPERS = TASKS / "workspace-helpers.yml"
TOKEN = "SENTINEL-workspace-token-4f9a"
PROVIDER = "primary-git"
USERNAME = "oauth2"
EXPECTED_AUTH = "Basic " + base64.b64encode(f"{USERNAME}:{TOKEN}".encode()).decode()


# --- the loopback fixture: a git server and a stand-in workspace service -----

class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves a bare repository over git's dumb HTTP protocol behind Basic auth,
    and the workspace service's token endpoint at /external-auth/<provider>."""

    state: dict[str, Any]

    def do_GET(self) -> None:  # noqa: N802 - the http.server interface
        if self.path.startswith("/external-auth/"):
            self.state["token_requests"].append(self.path)
            status, body = self.state["token_reply"]
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        auth = self.headers.get("Authorization")
        self.state["git_auth"].append(auth)
        if auth != EXPECTED_AUTH:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


@contextmanager
def _loopback(root: Path, state: dict[str, Any]) -> Iterator[str]:
    handler = type("Handler", (_Handler,), {"state": state})
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(handler, directory=str(root))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _git(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120, check=False
    )


def _clean_env(tmp_path: Path, path: str) -> dict[str, str]:
    """Git with no user or system configuration and no prompts."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": path,
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }


@pytest.fixture()
def service(tmp_path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """A loopback server with a one-commit repository at /repo.git."""
    root = tmp_path / "srv"
    root.mkdir()
    env = _clean_env(tmp_path, os.environ["PATH"])
    work = tmp_path / "work"
    assert _git("init", "-q", "-b", "main", str(work), env=env).returncode == 0
    (work / "README").write_text("workspace helper fixture\n")
    _git("add", "README", cwd=work, env=env)
    commit = _git(
        "-c", "user.name=Fixture", "-c", "user.email=fixture@example.org",
        "commit", "-q", "-m", "fixture", cwd=work, env=env,
    )
    assert commit.returncode == 0, commit.stderr
    assert _git("clone", "-q", "--bare", str(work), str(root / "repo.git"), env=env).returncode == 0
    assert _git("update-server-info", cwd=root / "repo.git", env=env).returncode == 0
    state: dict[str, Any] = {"token_requests": [], "git_auth": [], "token_reply": (200, f"{TOKEN}\n".encode())}
    with _loopback(root, state) as host:
        yield host, state


# --- installing the helper with the role's own task ---------------------------

def _helper_tasks(first: str, last: str | None = None) -> list[dict[str, Any]]:
    """The role's tasks, run unprivileged: templates come from the role's
    templates directory and keep the test user as their owner."""
    tasks = role_tasks(HELPERS, first, last)
    for task in tasks:
        template = task.get("ansible.builtin.template")
        if template is not None:
            template["src"] = str(ROLE / "templates" / template["src"])
            template.pop("owner", None)
            template.pop("group", None)
    return tasks


def _install_task() -> list[dict[str, Any]]:
    return _helper_tasks("Install the workspace git credential helper")


def _install(tmp_path: Path, **overrides: Any) -> Path:
    helper = tmp_path / "helper" / "git-credential-workspace"
    helper.parent.mkdir(exist_ok=True)
    play_vars = {
        "workspace_git_auth_helper_enabled": True,
        "workspace_external_auth_binary": DEFAULTS["workspace_external_auth_binary"],
        "workspace_git_auth_username": DEFAULTS["workspace_git_auth_username"],
        "workspace_external_auth_provider": PROVIDER,
        "workspace_git_host": "git.example.org",
        "workspace_git_auth_helper_script_path": str(helper),
        **overrides,
    }
    result = run_playbook(tmp_path, _install_task(), play_vars)
    assert result.returncode == 0, output(result)
    return helper


def _stub_bin(tmp_path: Path, name: str, body: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / name
    stub.write_text("#!/bin/sh\n" + body + "\n")
    stub.chmod(0o755)
    return bindir


# The stub client asks the stand-in service for the token, as the real client
# asks its deployment; curl -f turns an HTTP error into a non-zero exit. It
# also writes the token's name to stderr and echoes any body to stdout, so a
# helper that forwarded either would leak it.
_SERVICE_CLIENT = (
    '[ "$1 $2" = "external-auth access-token" ] || exit 64\n'
    'echo "client: asking for $3 (token $WORKSPACE_TOKEN_HINT)" >&2\n'
    'exec curl -sS -f "http://$WORKSPACE_SERVICE/external-auth/$3"'
)


def _clone(tmp_path: Path, host: str, helper: Path, bindir: Path | None) -> subprocess.CompletedProcess[str]:
    path = os.environ["PATH"] if bindir is None else f"{bindir}:{os.environ['PATH']}"
    if bindir is None:
        # No token command anywhere on PATH: keep only git's and the shell's directories.
        path = ":".join(sorted({str(Path(p).parent) for p in (_which("git"), _which("sh"))}))
    env = _clean_env(tmp_path, path)
    env["WORKSPACE_SERVICE"] = host
    env["WORKSPACE_TOKEN_HINT"] = TOKEN
    target = tmp_path / f"clone-{len(list(tmp_path.glob('clone-*')))}"
    return _git(
        "-c", f"credential.http://{host}.helper=!{helper}",
        "clone", "-q", f"http://{host}/repo.git", str(target),
        env=env,
    )


def _which(name: str) -> str:
    found = subprocess.run(["sh", "-c", f"command -v {name}"], capture_output=True, text=True, check=True)
    return found.stdout.strip()


def _assert_no_token(result: subprocess.CompletedProcess[str], state: dict[str, Any]) -> None:
    assert TOKEN not in output(result)
    assert EXPECTED_AUTH not in state["git_auth"]


# --- token acquisition: success and every failure -----------------------------

def test_token_from_the_workspace_service_authenticates_git(tmp_path: Path, service) -> None:
    host, state = service
    helper = _install(tmp_path, workspace_git_host=host)
    result = _clone(tmp_path, host, helper, _stub_bin(tmp_path, "coder", _SERVICE_CLIENT))
    assert result.returncode == 0, output(result)
    assert (tmp_path / "clone-0" / "README").read_text() == "workspace helper fixture\n"
    assert state["token_requests"] == [f"/external-auth/{PROVIDER}"]
    assert EXPECTED_AUTH in state["git_auth"]
    assert TOKEN not in output(result)


@pytest.mark.parametrize("status", [401, 500])
def test_service_refusing_the_token_fails_clearly_without_printing_it(tmp_path: Path, service, status: int) -> None:
    host, state = service
    state["token_reply"] = (status, f"error: {TOKEN}\n".encode())
    helper = _install(tmp_path, workspace_git_host=host)
    result = _clone(tmp_path, host, helper, _stub_bin(tmp_path, "coder", _SERVICE_CLIENT))
    assert result.returncode != 0
    assert (
        f"git-credential-workspace: cannot get a token for {host}: "
        f"'coder external-auth access-token {PROVIDER}' failed (exit status 22). "
        f"Sign in to the workspace service and connect your {PROVIDER} account there"
    ) in result.stderr
    assert state["token_requests"] == [f"/external-auth/{PROVIDER}"]
    _assert_no_token(result, state)


def test_unreachable_service_fails_clearly(tmp_path: Path, service) -> None:
    host, state = service
    helper = _install(tmp_path, workspace_git_host=host)
    bindir = _stub_bin(tmp_path, "coder", _SERVICE_CLIENT.replace("$WORKSPACE_SERVICE", "127.0.0.1:9"))
    result = _clone(tmp_path, host, helper, bindir)
    assert result.returncode != 0
    assert re.search(r"cannot get a token for .*' failed \(exit status 7\)", result.stderr), result.stderr
    _assert_no_token(result, state)


def test_missing_client_fails_clearly(tmp_path: Path, service) -> None:
    host, state = service
    helper = _install(tmp_path, workspace_git_host=host)
    result = _clone(tmp_path, host, helper, None)
    assert result.returncode != 0
    assert f"cannot get a token for {host}: coder is not installed or not on PATH" in result.stderr
    assert state["token_requests"] == []
    _assert_no_token(result, state)


def test_empty_token_fails_clearly(tmp_path: Path, service) -> None:
    host, state = service
    state["token_reply"] = (200, b"")
    helper = _install(tmp_path, workspace_git_host=host)
    result = _clone(tmp_path, host, helper, _stub_bin(tmp_path, "coder", _SERVICE_CLIENT))
    assert result.returncode != 0
    assert f"'coder external-auth access-token {PROVIDER}' printed no token" in result.stderr
    _assert_no_token(result, state)


def test_output_that_is_not_one_token_is_refused_unseen(tmp_path: Path, service) -> None:
    # A second line could smuggle extra fields into git's credential protocol.
    host, state = service
    state["token_reply"] = (200, f"{TOKEN}\nusername=someone-else\n".encode())
    helper = _install(tmp_path, workspace_git_host=host)
    result = _clone(tmp_path, host, helper, _stub_bin(tmp_path, "coder", _SERVICE_CLIENT))
    assert result.returncode != 0
    assert "printed something other than a single token" in result.stderr
    assert "someone-else" not in output(result)
    _assert_no_token(result, state)


def test_store_and_erase_keep_nothing(tmp_path: Path) -> None:
    helper = _install(tmp_path)
    log = tmp_path / "called"
    bindir = _stub_bin(tmp_path, "coder", f'echo called > "{log}"; echo {TOKEN}')
    env = _clean_env(tmp_path, f"{bindir}:{os.environ['PATH']}")
    for action in ("store", "erase"):
        result = subprocess.run(
            [str(helper), action], input=f"protocol=https\nhost=git.example.org\npassword={TOKEN}\n\n",
            env=env, capture_output=True, text=True, timeout=30, check=False,
        )
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert not log.exists()


def test_client_provider_and_username_are_settings(tmp_path: Path) -> None:
    helper = _install(
        tmp_path,
        workspace_external_auth_binary="lab-auth",
        workspace_external_auth_provider="forge.internal-2",
        workspace_git_auth_username="token-user",
    )
    args = tmp_path / "args"
    bindir = _stub_bin(tmp_path, "lab-auth", f'echo "$@" > "{args}"; echo {TOKEN}')
    result = subprocess.run(
        [str(helper), "get"], input="protocol=https\nhost=git.example.org\n\n",
        env=_clean_env(tmp_path, f"{bindir}:{os.environ['PATH']}"),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"username=token-user\npassword={TOKEN}\n"
    assert args.read_text().strip() == "external-auth access-token forge.internal-2"


# --- /etc/gitconfig wiring, against a throwaway system config -----------------

def _wire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Path:
    gitconfig = tmp_path / "system-gitconfig"
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(gitconfig))
    files = tmp_path / "files"
    files.mkdir(exist_ok=True)
    tasks = _helper_tasks(
        "Install the workspace git credential helper",
        "Remove the workspace login-shell environment when none is set",
    )
    play_vars = {
        "workspace_git_auth_helper_enabled": False,
        "workspace_git_host": "",
        "workspace_external_auth_provider": "",
        "workspace_external_auth_binary": "coder",
        "workspace_git_auth_username": "oauth2",
        "workspace_login_env": {},
        "workspace_login_env_unset": [],
        "workspace_git_auth_helper_script_path": str(files / "git-credential-workspace"),
        "workspace_login_env_config_path": str(files / "workspace-env.sh"),
        "workspace_legacy_git_auth_helper_script_path": str(files / "git-credential-coder-gitlab"),
        "workspace_legacy_login_env_config_path": str(files / "coder-gitlab.sh"),
        **overrides,
    }
    result = run_playbook(tmp_path, tasks, play_vars)
    assert result.returncode == 0, output(result)
    return gitconfig


def _helpers(gitconfig: Path) -> list[str]:
    listed = subprocess.run(
        ["git", "config", "--file", str(gitconfig), "--get-regexp", r"^credential\..*\.helper$"],
        capture_output=True, text=True, check=False,
    )
    return listed.stdout.splitlines()


def test_enabling_wires_one_host_and_removes_the_earlier_helper(tmp_path: Path, monkeypatch) -> None:
    files = tmp_path / "files"
    files.mkdir()
    legacy = files / "git-credential-coder-gitlab"
    legacy.write_text("#!/bin/sh\n")
    legacy_profile = files / "coder-gitlab.sh"
    legacy_profile.write_text("export GITLAB_HOST=gitlab.com\n")
    gitconfig = tmp_path / "system-gitconfig"
    subprocess.run(["git", "config", "--file", str(gitconfig), "credential.https://gitlab.com.helper", f"!{legacy}"], check=True)
    subprocess.run(["git", "config", "--file", str(gitconfig), "credential.https://other.example.org.helper", "!/opt/their-helper"], check=True)

    _wire(
        tmp_path, monkeypatch,
        workspace_git_auth_helper_enabled=True,
        workspace_git_host="git.example.org",
        workspace_external_auth_provider=PROVIDER,
        workspace_login_env={"GITLAB_HOST": "git.example.org"},
        workspace_login_env_unset=["GITLAB_TOKEN"],
    )

    assert sorted(_helpers(gitconfig)) == [
        f"credential.https://git.example.org.helper !{files / 'git-credential-workspace'}",
        "credential.https://other.example.org.helper !/opt/their-helper",
    ]
    assert (files / "git-credential-workspace").exists()
    assert not legacy.exists() and not legacy_profile.exists()
    env_file = (files / "workspace-env.sh").read_text()
    assert "export GITLAB_HOST=git.example.org\n" in env_file
    assert "unset GITLAB_TOKEN\n" in env_file


def test_defaults_install_nothing_that_reaches_a_service(tmp_path: Path, monkeypatch) -> None:
    files = tmp_path / "files"
    files.mkdir()
    helper = files / "git-credential-workspace"
    helper.write_text("#!/bin/sh\n")
    (files / "workspace-env.sh").write_text("export A=1\n")
    gitconfig = tmp_path / "system-gitconfig"
    subprocess.run(["git", "config", "--file", str(gitconfig), "credential.https://git.example.org.helper", f"!{helper}"], check=True)

    _wire(tmp_path, monkeypatch)  # the role's defaults: helper off, no environment

    assert _helpers(gitconfig) == []
    assert sorted(p.name for p in files.iterdir()) == []


def test_role_defaults_are_safe_and_name_no_deployment() -> None:
    assert DEFAULTS["workspace_git_auth_helper_enabled"] is False
    assert DEFAULTS["workspace_git_host"] == ""
    assert DEFAULTS["workspace_external_auth_provider"] == ""
    assert DEFAULTS["workspace_login_env"] == {} and DEFAULTS["workspace_login_env_unset"] == []
    assert DEFAULTS["coder_cli_enabled"] is False and DEFAULTS["coder_deployment_url"] == ""
    for name in ("workspace_gitlab_host", "workspace_gitlab_external_auth_provider"):
        assert name not in DEFAULTS, f"{name} is retired (catalog#29)"
    texts = [HELPERS.read_text()] + [
        (ROLE / "templates" / name).read_text()
        for name in ("git-credential-workspace.j2", "workspace-login-env.sh.j2")
    ]
    for text in texts:
        assert "gitlab.com" not in text and "github.com" not in text


def test_the_helper_tasks_are_gated_on_the_switch() -> None:
    tasks = {task["name"]: task for task in yaml.safe_load(HELPERS.read_text())}
    for name in (
        "Install the workspace git credential helper",
        "Wire the credential helper for the workspace git host in /etc/gitconfig",
    ):
        assert tasks[name]["when"] == "workspace_git_auth_helper_enabled | bool"
    for name in (
        "Ensure /etc/skel/.bash_profile sources ~/.bashrc for new users",
        "Backfill ~/.bash_profile for the current dev_user (skel only applies to new users)",
    ):
        assert "when" not in tasks[name]
