"""The changed downloads against the real vendor URLs (catalog#171).

tests/test_verified_download.py shows, against fixtures, that each path fails
closed. This file shows the role's own tasks still resolve and verify what the
vendors publish today: the same task slices, run with ansible-playbook on
localhost into pytest's tmp_path, against dl.k8s.io, get.helm.sh, go.dev,
GitHub (Codex, SDKMAN!) and downloads.claude.ai. A vendor that moves or drops
its checksum or signature turns this red, which is the point. CI runs it in the
`verified-downloads-vendors` job, apart from the offline fixture tests; it
needs network access, gpg and git.

Every network step is bounded (catalog#174, catalog#177): the test's own
lookups through `_fetch` (a connect and read timeout, three tries with backoff,
the whole body read inside each try), and each ansible-playbook run by a
progress watchdog. A vendor that has stopped answering fails its test with the
endpoint named; a slow runner that is still receiving does not.

GitHub API calls are kept to three (anonymous limit: 60 an hour); a
GITHUB_TOKEN in the environment is used when present, through a vars file.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verified_download_support import (
    CLAUDE_RELEASE,
    DEFAULTS,
    ROLE,
    TASKS,
    current_user,
    fixture_server,
    include,
    output,
    role_tasks,
    run_playbook,
)

SHA256_SUFFIX = re.compile(r"-[0-9a-f]{64}$")

# Seconds a lookup may wait to connect or between reads; tries and the backoff
# between them. Worst case for one lookup: 3 x 20 s + 2 s + 4 s.
URL_TIMEOUT = 20
URL_TRIES = 3
# A playbook run fails once nothing under its directory has grown for STALL
# seconds. That is a stopped vendor, not a slow runner: the role's get_url
# already gives up on a read after 60 s, three tries, so STALL only has to
# outlast one of those tries and a lookup between downloads (catalog#177).
STALL = 150
# The ceiling on one run, for a transfer that trickles without ever stalling.
# The slowest normal run is about 20 s (pipeline 174670, --durations); on
# 2026-11-27 a run took more than 180 s while still downloading (pipeline
# 175187, catalog#177). The job's 45-minute timeout is the backstop for all
# runs together.
PLAY_BUDGET = 480
# The Claude Code binary is the largest download.
CLAUDE_PLAY_BUDGET = 900


def _cached(directory: Path, prefix: str) -> Path:
    found = [path for path in directory.iterdir() if path.name.startswith(prefix)]
    assert len(found) == 1, f"expected one verified {prefix} in {directory}: {found}"
    assert SHA256_SUFFIX.search(found[0].name), found[0].name
    return found[0]


@dataclass(frozen=True)
class _Fetched:
    url: str
    body: bytes


def _fetch(request: urllib.request.Request | str) -> _Fetched:
    """GET `request`, bounded: URL_TIMEOUT to connect and per read, URL_TRIES tries.

    The role retries every upstream lookup, so a single slow or cut-off read is
    not a vendor change; this helper gives the test's own lookups a like
    allowance, backing off 2 s, then 4 s. The body is read inside each try, so a
    response cut short (IncompleteRead) is retried like a timeout, a refused or
    reset connection or a 5xx; any other HTTP error fails at once. Either way
    the failure names the URL. `url` is the address after redirects.
    """
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    for attempt in range(1, URL_TRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=URL_TIMEOUT) as response:
                return _Fetched(response.geturl(), response.read())
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == URL_TRIES:
                message = f"{url}: HTTP {error.code} {error.reason} (try {attempt} of {URL_TRIES})"
                raise AssertionError(message) from error
            failure: Exception = error
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError) as error:
            if attempt == URL_TRIES:
                message = f"{url}: {error!r} (try {attempt} of {URL_TRIES}, {URL_TIMEOUT} s timeout)"
                raise AssertionError(message) from error
            failure = error
        print(f"{url}: {failure!r}; retrying in {2 ** attempt} s")
        time.sleep(2 ** attempt)
    raise AssertionError(f"{url}: no try made")  # URL_TRIES is at least 1


def _github_token_vars() -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN", "")
    return {"_github_api_token": token} if token else {}


def _github_latest_release(repository: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "curiosity-catalog-tests",
            **({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"} if os.environ.get("GITHUB_TOKEN") else {}),
        },
    )
    return json.loads(_fetch(request).body)


def _latest_tag_from_redirect(repository: str) -> str:
    """The latest release tag, from github.com's redirect (no API quota)."""
    final = _fetch(f"https://github.com/{repository}/releases/latest").url
    match = re.search(r"/releases/tag/([^/?#]+)$", final)
    assert match, f"github.com/{repository}/releases/latest redirected to {final}"
    return match.group(1)


def test_kubectl_resolves_and_verifies(tmp_path: Path) -> None:
    tasks = role_tasks(TASKS / "kubernetes.yml", "Resolve latest kubectl release", "Download and verify latest kubectl")
    result = run_playbook(tmp_path, tasks, {}, timeout=PLAY_BUDGET, stall=STALL, endpoint="dl.k8s.io")
    assert result.returncode == 0, output(result)
    binary = _cached(tmp_path / "cache", "kubectl-v")
    assert binary.stat().st_mode & 0o777 == 0o755


def test_helm_resolves_and_verifies(tmp_path: Path) -> None:
    tag = _latest_tag_from_redirect("helm/helm")
    tasks = role_tasks(TASKS / "kubernetes.yml", "Download and verify latest Helm")
    result = run_playbook(
        tmp_path, tasks, {"latest_github_releases": {"helm": {"tag_name": tag}}},
        timeout=PLAY_BUDGET, stall=STALL, endpoint="get.helm.sh",
    )
    assert result.returncode == 0, output(result)
    _cached(tmp_path / "cache", f"helm-{tag}-linux-amd64.tar.gz")


def test_go_resolves_and_verifies(tmp_path: Path) -> None:
    tasks = [
        task
        for task in role_tasks(TASKS / "languages.yml", "Get installed Go version", "Download and verify latest Go release")
        # The snapshot of an existing /usr/local/go is not part of the download.
        if task["name"] not in {"Inspect the existing Go tree before replacement", "Snapshot the existing Go tree before replacement"}
    ]
    # Force the download even on a machine that already has the current Go.
    tasks.insert(
        [task["name"] for task in tasks].index("Set Go update state") + 1,
        {"name": "Download regardless of an installed Go", "ansible.builtin.set_fact": {"go_needs_update": True}},
    )
    result = run_playbook(tmp_path, tasks, {}, timeout=PLAY_BUDGET, stall=STALL, endpoint="go.dev")
    assert result.returncode == 0, output(result)
    _cached(tmp_path / "cache", "go")


def test_codex_installer_resolves_and_verifies(tmp_path: Path) -> None:
    release = _github_latest_release(DEFAULTS["latest_github_repositories"]["codex"])
    user = current_user(tmp_path)
    tasks = role_tasks(
        TASKS / "codex-standalone-install.yml",
        "Select the Codex installer from the latest release",
        "Download and verify the Codex standalone installer",
    )
    result = run_playbook(tmp_path, tasks, {
        "dev_user": user["name"],
        "dev_user_home": user["home"],
        "codex_standalone_enabled": True,
        "codex_standalone_release": "latest",
        "_codex_standalone_check": {"rc": 1},
        "latest_github_releases": {"codex": {"tag_name": release["tag_name"], "assets": release["assets"]}},
    }, timeout=PLAY_BUDGET, stall=STALL, endpoint="github.com (openai/codex release assets)")
    assert result.returncode == 0, output(result)
    installer = _cached(Path(user["home"]) / ".cache/curiosity-corp/downloads", "codex-install.sh")
    assert installer.read_bytes().startswith(b"#!"), "the release install.sh is a script"


def test_claude_code_release_signature_and_binary_verify(tmp_path: Path) -> None:
    user = current_user(tmp_path)
    tasks = role_tasks(
        TASKS / "claude-code-user.yml",
        "Resolve and verify the Claude Code release",
        "Download and verify the Claude Code binary",
    )
    for task in tasks:
        task.pop("when", None)
    result = run_playbook(
        tmp_path,
        tasks,
        {
            "claude_native_channel": "stable",
            "claude_native_release_base_url": DEFAULTS["claude_native_release_base_url"],
            "claude_native_release_key_file": str(ROLE / "files" / DEFAULTS["claude_native_release_key_file"]),
            "claude_native_release_key_fingerprint": DEFAULTS["claude_native_release_key_fingerprint"],
            "claude_native_release_cache_dir": str(tmp_path / "cache/claude-code"),
            "claude_native_platform_map": DEFAULTS["claude_native_platform_map"],
            "dw_user": user,
        },
        gather_facts=True,
        timeout=CLAUDE_PLAY_BUDGET, stall=STALL,
        endpoint="downloads.claude.ai",
    )
    assert result.returncode == 0, output(result)
    _cached(Path(user["home"]) / ".claude/downloads", "claude-")


def test_claude_code_release_check_rejects_an_altered_real_manifest(tmp_path: Path) -> None:
    """The real signature does not cover a manifest with a changed checksum."""
    base = DEFAULTS["claude_native_release_base_url"]
    version = _fetch(f"{base}/stable").body.decode().strip()
    site = tmp_path / "site" / version
    site.mkdir(parents=True)
    for name in ("manifest.json", "manifest.json.sig"):
        (site / name).write_bytes(_fetch(f"{base}/{version}/{name}").body)
    (tmp_path / "site/stable").write_text(version)
    manifest = (site / "manifest.json").read_text()
    altered = re.sub(r'"checksum":\s*"[0-9a-f]{64}"', '"checksum": "' + "0" * 64 + '"', manifest, count=1)
    assert altered != manifest
    (site / "manifest.json").write_text(altered)

    with fixture_server(tmp_path / "site") as (url, _):
        result = run_playbook(
            tmp_path,
            [include(CLAUDE_RELEASE)],
            {
                "claude_native_channel": "stable",
                "claude_native_release_base_url": url,
                "claude_native_release_key_file": str(ROLE / "files" / DEFAULTS["claude_native_release_key_file"]),
                "claude_native_release_key_fingerprint": DEFAULTS["claude_native_release_key_fingerprint"],
                "claude_native_release_cache_dir": str(tmp_path / "cache/claude-code"),
                "claude_native_platform_map": DEFAULTS["claude_native_platform_map"],
            },
            gather_facts=True,
            timeout=PLAY_BUDGET, stall=STALL,
            endpoint="the local copy of downloads.claude.ai's manifest",
        )
    assert result.returncode != 0
    assert "BADSIG" in output(result), output(result)


def test_sdkman_release_commit_resolves_and_checks_out(tmp_path: Path) -> None:
    user = current_user(tmp_path)
    tasks = role_tasks(
        TASKS / "languages.yml",
        "Resolve the latest SDKMAN release",
        "Require the SDKMAN checkout to be the release commit",
    )
    # The API tasks keep their no_log: the optional token rides in the headers.
    play_vars = {
        "dev_user": user["name"],
        "dev_user_home": user["home"],
        "_sdkman_allow_source_update": True,
        **_github_token_vars(),
    }
    result = run_playbook(
        tmp_path, tasks, play_vars, timeout=PLAY_BUDGET, stall=STALL,
        endpoint="api.github.com and github.com (sdkman/sdkman-cli)",
    )
    assert result.returncode == 0, output(result)
    head = subprocess.run(
        ["git", "-C", str(Path(user["home"]) / ".sdkman"), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{40}", head)
