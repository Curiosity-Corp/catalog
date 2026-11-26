"""A controlled network failure stops the run instead of installing stale software.

catalog#28, docs/quickstarts/restricted-networks.md. Nothing here reaches the
real network or a real host; every path is under pytest's tmp_path.

- Release discovery through a stand-in proxy that refuses every CONNECT: the
  proxy settings Ansible exports reach `uri`, and the failed discovery ends the
  run with the "no stale fallback" refusal instead of an old release.
- A release asset GitHub never hashed (`digest: null`) is dropped at discovery,
  so no installer can download it unverified.
- verified-download.yml with its upstream unreachable (a port nothing listens
  on): a checksum file it cannot read stops the run before any download, a
  cache pre-filled by digest is verified and reused without the network, and a
  pre-filled file that does not match its digest is refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from verified_download_support import VERIFIED_DOWNLOAD, fixture_server, include, output, role_tasks, run_playbook

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


# --- Release assets without a digest ------------------------------------------

LATEST = ROLE / "tasks/latest-releases.yml"
GOOD_DIGEST = "sha256:" + "a" * 64


def _asset(name: str, digest: str | None) -> dict[str, Any]:
    return {"name": name, "browser_download_url": f"https://example.invalid/{name}", "digest": digest}


def _discovery_checks(tmp_path: Path, releases: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    tasks = role_tasks(
        LATEST,
        "Keep only release assets that carry a verifiable SHA-256 digest",
        "Require latest metadata for every binary release source",
    )
    tasks.append(
        {
            "name": "Show the assets an installer can select",
            "ansible.builtin.debug": {
                "msg": "selectable={{ latest_github_releases | dict2items | map(attribute='value.assets') "
                "| flatten | map(attribute='name') | sort | join(',') }}"
            },
        }
    )
    return run_playbook(
        tmp_path,
        tasks,
        {"latest_github_releases": releases, "_latest_release_sources": {name: "example/" + name for name in releases}},
    )


def test_an_asset_without_a_digest_cannot_be_selected(tmp_path: Path) -> None:
    result = _discovery_checks(
        tmp_path,
        {
            "tool": {
                "tag_name": "v2.0.0",
                "assets": [
                    _asset("tool_linux_amd64.tar.gz", None),
                    _asset("tool_linux_arm64.tar.gz", GOOD_DIGEST),
                    {"name": "tool_no_digest_key.tar.gz", "browser_download_url": "https://example.invalid/x"},
                    _asset("tool_md5.tar.gz", "md5:" + "b" * 32),
                ],
            }
        },
    )
    assert result.returncode == 0, output(result)
    assert "selectable=tool_linux_arm64.tar.gz" in result.stdout, output(result)


def test_a_release_with_no_hashed_asset_fails_closed(tmp_path: Path) -> None:
    result = _discovery_checks(
        tmp_path,
        {
            "hashed": {"tag_name": "v1.0.0", "assets": [_asset("hashed.tar.gz", GOOD_DIGEST)]},
            "unhashed": {"tag_name": "v1.0.0", "assets": [_asset("unhashed.tar.gz", None)]},
        },
    )
    text = " ".join(output(result).split())
    assert result.returncode != 0, text
    assert "Could not resolve a current upstream release for unhashed with a SHA-256 digest" in text, text
    assert "refuses to fall back to a stale version" in text, text
    assert "selectable=" not in result.stdout


# --- verified-download.yml with the upstream unreachable -----------------------

PAYLOAD = b"research toolchain\n" * 1024
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture()
def dead_url() -> str:
    """A loopback URL whose port nothing listens on: every request is refused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@pytest.fixture()
def site(tmp_path: Path) -> Iterator[tuple[Path, str, list[str]]]:
    root = tmp_path / "site"
    root.mkdir()
    with fixture_server(root) as (base, requests):
        yield root, base, requests


def _cache(tmp_path: Path) -> Path:
    return tmp_path / "cache"


def _prefill(tmp_path: Path, name: str, content: bytes, digest: str) -> Path:
    """What the guide's offline preparation does: the file under <name>-<sha256>."""
    path = _cache(tmp_path) / f"{name}-{digest}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o750)
    path.write_bytes(content)
    path.chmod(0o644)
    return path


def _download(tmp_path: Path, **task_vars: Any) -> subprocess.CompletedProcess[str]:
    return run_playbook(
        tmp_path,
        [
            include(VERIFIED_DOWNLOAD, **task_vars),
            {"name": "Show the verified file", "ansible.builtin.debug": {"msg": "verified={{ verified_download_path }}"}},
        ],
        {},
    )


def test_unreachable_checksum_file_fails_closed_before_any_download(tmp_path: Path, site, dead_url: str) -> None:
    root, base, requests = site
    (root / "tool.tar.gz").write_bytes(PAYLOAD)
    result = _download(
        tmp_path,
        verified_download_name="tool-v1.0.0.tar.gz",
        verified_download_url=f"{base}/tool.tar.gz",
        verified_download_sha256_url=f"{dead_url}/tool.tar.gz.sha256",
    )
    assert result.returncode != 0, output(result)
    assert "Read the vendor checksum file for tool-v1.0.0.tar.gz" in result.stdout, output(result)
    assert "verified=" not in result.stdout
    # The artifact server was reachable, and still nothing was fetched from it.
    assert requests == []
    assert not _cache(tmp_path).exists() or list(_cache(tmp_path).iterdir()) == []


def test_unreachable_checksum_file_ignores_a_prefilled_cache(tmp_path: Path, dead_url: str) -> None:
    """The digest comes from upstream or the caller, never from what is in the cache."""
    cached = _prefill(tmp_path, "tool-v1.0.0.tar.gz", PAYLOAD, DIGEST)
    result = _download(
        tmp_path,
        verified_download_name="tool-v1.0.0.tar.gz",
        verified_download_url=f"{dead_url}/tool.tar.gz",
        verified_download_sha256_url=f"{dead_url}/tool.tar.gz.sha256",
    )
    assert result.returncode != 0, output(result)
    assert "verified=" not in result.stdout
    assert cached.read_bytes() == PAYLOAD


def test_prefilled_cache_is_verified_and_reused_offline(tmp_path: Path, dead_url: str) -> None:
    cached = _prefill(tmp_path, "go1.0.linux-amd64.tar.gz", PAYLOAD, DIGEST)
    result = _download(
        tmp_path,
        verified_download_name="go1.0.linux-amd64.tar.gz",
        verified_download_url=f"{dead_url}/go1.0.linux-amd64.tar.gz",
        verified_download_sha256=f"sha256:{DIGEST}",
    )
    assert result.returncode == 0, output(result)
    assert f"verified={cached}" in result.stdout, output(result)
    assert re.search(r"localhost\s*:\s*ok=\d+\s+changed=0\s", result.stdout), output(result)
    assert cached.read_bytes() == PAYLOAD


def test_prefilled_file_that_does_not_match_its_digest_is_refused_offline(tmp_path: Path, dead_url: str) -> None:
    _prefill(tmp_path, "go1.0.linux-amd64.tar.gz", b"truncated or tampered", DIGEST)
    result = _download(
        tmp_path,
        verified_download_name="go1.0.linux-amd64.tar.gz",
        verified_download_url=f"{dead_url}/go1.0.linux-amd64.tar.gz",
        verified_download_sha256=f"sha256:{DIGEST}",
    )
    assert result.returncode != 0, output(result)
    assert "Download and verify go1.0.linux-amd64.tar.gz" in result.stdout, output(result)
    assert "verified=" not in result.stdout
