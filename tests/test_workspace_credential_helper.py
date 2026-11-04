"""Provider-neutral workspace credential helper (docs/workspace-providers.md).

The shipped template is rendered with Jinja2 and run for real against stub
`coder` binaries, so token-acquisition failure is exercised without a Coder
deployment or network.
"""

import os
import stat
import subprocess
from pathlib import Path

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TOKEN = "stub-token-value-123"


def render(tmp_path: Path, **overrides) -> Path:
    variables = {
        "workspace_external_auth_binary": "coder",
        "workspace_gitlab_external_auth_provider": "gitlab",
        "workspace_credential_helper_username": "oauth2",
        "workspace_gitlab_host": "git.example.org",
    }
    variables.update(overrides)
    template = (ROLE / "templates/git-credential-coder-gitlab.j2").read_text()
    out = tmp_path / "git-credential-helper"
    out.write_text(jinja2.Template(template).render(**variables))
    out.chmod(out.stat().st_mode | stat.S_IXUSR)
    return out


def stub(tmp_path: Path, name: str, body: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    path = bindir / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return bindir


def run(helper: Path, action: str, bindir: Path | None, tmp_path: Path):
    path = f"{bindir}:/usr/bin:/bin" if bindir else "/usr/bin:/bin"
    return subprocess.run(
        [str(helper), action],
        capture_output=True,
        text=True,
        env={"PATH": path, "HOME": str(tmp_path), "STUB_LOG": str(tmp_path / "args.log")},
        timeout=30,
        check=False,
    )


def test_success_prints_a_git_credential_with_the_token(tmp_path: Path) -> None:
    bindir = stub(tmp_path, "coder", f'echo "$@" > "$STUB_LOG"; echo {TOKEN}')
    result = run(render(tmp_path), "get", bindir, tmp_path)
    assert result.returncode == 0
    assert result.stdout == f"username=oauth2\npassword={TOKEN}\n"
    assert (tmp_path / "args.log").read_text().strip() == "external-auth access-token gitlab"


def test_token_acquisition_failure_falls_back_silently(tmp_path: Path) -> None:
    bindir = stub(tmp_path, "coder", 'echo "boom: cannot reach deployment" >&2; exit 1')
    result = run(render(tmp_path), "get", bindir, tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_empty_token_falls_back_silently(tmp_path: Path) -> None:
    bindir = stub(tmp_path, "coder", "exit 0")
    result = run(render(tmp_path), "get", bindir, tmp_path)
    assert (result.returncode, result.stdout) == (0, "")


def test_missing_binary_falls_back_silently(tmp_path: Path) -> None:
    result = run(render(tmp_path), "get", None, tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_store_and_erase_do_nothing(tmp_path: Path) -> None:
    bindir = stub(tmp_path, "coder", f'echo called > "$STUB_LOG"; echo {TOKEN}')
    helper = render(tmp_path)
    for action in ("store", "erase"):
        result = run(helper, action, bindir, tmp_path)
        assert (result.returncode, result.stdout) == (0, "")
    assert not (tmp_path / "args.log").exists()


def test_provider_binary_and_username_are_parameters(tmp_path: Path) -> None:
    bindir = stub(tmp_path, "my-auth", f'echo "$@" > "$STUB_LOG"; echo {TOKEN}')
    helper = render(
        tmp_path,
        workspace_external_auth_binary="my-auth",
        workspace_gitlab_external_auth_provider="corp-gitlab",
        workspace_credential_helper_username="token-user",
    )
    result = run(helper, "get", bindir, tmp_path)
    assert result.stdout == f"username=token-user\npassword={TOKEN}\n"
    assert (tmp_path / "args.log").read_text().strip() == "external-auth access-token corp-gitlab"


def test_no_provider_or_organization_is_hard_coded() -> None:
    for name in ("git-credential-coder-gitlab.j2", "coder-gitlab-profile.sh.j2"):
        text = (ROLE / "templates" / name).read_text()
        assert "gitlab.com" not in text
        assert "developerdojo" not in text


def test_defaults_install_nothing_for_an_unconfigured_provider_name() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["workspace_credential_helper_enabled"] is True
    assert defaults["workspace_external_auth_binary"] == "coder"
    assert defaults["workspace_credential_helper_username"] == "oauth2"
    assert defaults["coder_cli_enabled"] is False
    assert defaults["coder_deployment_url"] == ""


def test_plain_ssh_workspace_skips_the_provider_tasks_but_keeps_login_shell_fix() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/coder-workspace-helpers.yml").read_text())
    gated = {t["name"] for t in tasks if "workspace_credential_helper_enabled" in str(t.get("when", ""))}
    assert gated == {
        "Install git-credential-coder-gitlab helper",
        "Install /etc/profile.d/coder-gitlab.sh",
        "Wire git credential helper for the configured GitLab host in /etc/gitconfig",
    }
    for task in tasks:
        if "bash_profile" in task["name"]:
            assert "when" not in task
    for task in tasks:
        if task["name"] in gated:
            assert "workspace_gitlab_host | length > 0" in task["when"]
