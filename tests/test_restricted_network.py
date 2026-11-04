"""A controlled network failure stops the run instead of installing stale software.

The test points release discovery at a stand-in proxy that refuses every
CONNECT. That proves two things without touching the real network: the proxy
settings Ansible exports reach `uri`, and a failed discovery ends the run with
the "no stale fallback" refusal rather than carrying on with an old release
(docs/quickstarts/restricted-networks.md).
"""

from __future__ import annotations

import json
import os
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")


class _RefusingProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        self.requests: list[str] = []
        super().__init__(("127.0.0.1", 0), _Handler)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline().decode("latin-1").strip()
        while self.rfile.readline().strip():
            pass
        self.server.requests.append(line)  # type: ignore[attr-defined]
        self.wfile.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")


PLAYBOOK = """\
---
- hosts: localhost
  connection: local
  gather_facts: false
  # The same mechanism examples/restricted-network-site.yml uses.
  environment:
    http_proxy: "{{ lab_http_proxy }}"
    https_proxy: "{{ lab_https_proxy }}"
    no_proxy: "{{ lab_no_proxy }}"
  tasks:
    - name: Resolve upstream releases through the proxy
      ansible.builtin.include_role:
        name: developer_workstation
        tasks_from: latest-releases.yml
"""


def test_failed_release_discovery_refuses_a_stale_fallback(tmp_path: Path) -> None:
    playbook = tmp_path / "discover.yml"
    playbook.write_text(PLAYBOOK)
    proxy = _RefusingProxy()
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{proxy.server_address[1]}"
        extra = {
            "lab_http_proxy": url,
            "lab_https_proxy": url,
            "lab_no_proxy": "",
            # One source keeps the retries short; the token is a fake that only
            # stops the role from calling the GitHub CLI.
            "latest_github_repositories": {"probe": "example/probe"},
            "latest_github_release_tag_prefixes": {},
            "github_api_token": "not-a-real-token",
            "workstation_update_state_dir": str(tmp_path / "state"),
        }
        env = dict(os.environ, ANSIBLE_ROLES_PATH=str(ROOT / "roles"), ANSIBLE_NOCOLOR="1", ANSIBLE_FORCE_COLOR="0")
        env.pop("ANSIBLE_STDOUT_CALLBACK", None)
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(playbook), "-e", json.dumps(extra)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    finally:
        proxy.shutdown()
        proxy.server_close()
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode != 0, output
    assert any("CONNECT api.github.com:443" in line for line in proxy.requests), proxy.requests
    assert "Could not resolve a current upstream release for probe" in output, output
    assert "refuses to fall back to a stale version" in output, output
    assert not (tmp_path / "state").exists(), "a failed discovery must not write release state"


def test_role_keeps_the_fail_closed_guards() -> None:
    """The refusals the guide quotes still exist in the task file."""
    tasks = yaml.safe_load((ROLE / "tasks/latest-releases.yml").read_text())
    asserts = {t["name"]: t for t in tasks if "ansible.builtin.assert" in t}
    stale = asserts["Require latest metadata for every binary release source"]
    assert "refuses to fall back to a stale version" in stale["ansible.builtin.assert"]["fail_msg"]
    digest = asserts["Require GitHub asset digests for verification"]
    assert "refuses to install release artifacts without a verifiable digest" in " ".join(
        digest["ansible.builtin.assert"]["fail_msg"].split()
    )
    main = (ROLE / "tasks/main.yml").read_text()
    assert "no stale fallback was installed" in main
