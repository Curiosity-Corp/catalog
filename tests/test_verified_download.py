"""Vendor-verified downloads (catalog#171), against local fixtures.

The first dependency register review found security-critical downloads that
only HTTPS vouched for. They now go through verified-download.yml (a vendor
SHA-256, given or read from the vendor's checksum file), with Claude Code's
SHA-256 taken from a release manifest whose signature claude-code-release.yml
checks against a pinned key, and SDKMAN! checked out at the commit GitHub's
API names for its release tag.

These tests run those task files with ansible-playbook on localhost against a
local HTTP server, a throwaway signing key and a local git remote, and show
each path accepts the vendor's artifact and fails closed on anything else:
nothing unverified is left where the role would use it. Every path is under
pytest's tmp_path. tests/test_verified_download_vendors.py runs the same task
files against the real vendor URLs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from verified_download_support import (
    CLAUDE_RELEASE,
    DEFAULTS,
    ROLE,
    TASKS,
    VERIFIED_DOWNLOAD,
    current_user,
    fixture_server,
    include,
    output,
    role_tasks,
    run_playbook,
)

PAYLOAD = b"#!/bin/sh\necho verified\n" * 512
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
OTHER_DIGEST = hashlib.sha256(b"something else").hexdigest()


@pytest.fixture()
def site(tmp_path: Path) -> Iterator[tuple[Path, str, list[str]]]:
    root = tmp_path / "site"
    root.mkdir()
    with fixture_server(root) as (base, requests):
        yield root, base, requests


def _cached(tmp_path: Path) -> list[Path]:
    cache = tmp_path / "cache"
    return sorted(cache.iterdir()) if cache.exists() else []


def _download(tmp_path: Path, **task_vars: Any):
    return run_playbook(tmp_path, [include(VERIFIED_DOWNLOAD, **task_vars)], {})


# --- verified-download.yml ---------------------------------------------------

def test_checksum_file_download_verifies(tmp_path: Path, site) -> None:
    root, base, requests = site
    (root / "kubectl").write_bytes(PAYLOAD)
    # dl.k8s.io's .sha256 is the bare digest; get.helm.sh's .sha256sum adds the name.
    (root / "kubectl.sha256sum").write_text(f"{DIGEST}  kubectl\n")
    result = _download(
        tmp_path,
        verified_download_name="kubectl-v1.0.0",
        verified_download_url=f"{base}/kubectl",
        verified_download_sha256_url=f"{base}/kubectl.sha256sum",
        verified_download_mode="0755",
    )
    assert result.returncode == 0, output(result)
    cached = tmp_path / "cache" / f"kubectl-v1.0.0-{DIGEST}"
    assert _cached(tmp_path) == [cached]
    assert cached.read_bytes() == PAYLOAD
    assert cached.stat().st_mode & 0o777 == 0o755
    assert requests == ["/kubectl.sha256sum", "/kubectl"]


def test_given_digest_verifies_and_a_verified_cache_is_reused(tmp_path: Path, site) -> None:
    root, base, requests = site
    (root / "go.tar.gz").write_bytes(PAYLOAD)
    task_vars = dict(
        verified_download_name="go1.0.linux-amd64.tar.gz",
        verified_download_url=f"{base}/go.tar.gz",
        # GitHub's asset digests carry the algorithm; go.dev's JSON does not.
        verified_download_sha256=f"sha256:{DIGEST.upper()}",
    )
    first = _download(tmp_path, **task_vars)
    assert first.returncode == 0, output(first)
    second = _download(tmp_path, **task_vars)
    assert second.returncode == 0, output(second)
    assert re.search(r"localhost\s*:\s*ok=\d+\s+changed=0\s", second.stdout), output(second)
    assert requests == ["/go.tar.gz"], "a cached file whose digest matches must not be fetched again"
    assert [path.name for path in _cached(tmp_path)] == [f"go1.0.linux-amd64.tar.gz-{DIGEST}"]


def test_wrong_checksum_fails_closed(tmp_path: Path, site) -> None:
    root, base, requests = site
    (root / "helm.tar.gz").write_bytes(PAYLOAD)
    (root / "helm.tar.gz.sha256sum").write_text(f"{OTHER_DIGEST}  helm.tar.gz\n")
    result = _download(
        tmp_path,
        verified_download_name="helm-v3.0.0-linux-amd64.tar.gz",
        verified_download_url=f"{base}/helm.tar.gz",
        verified_download_sha256_url=f"{base}/helm.tar.gz.sha256sum",
    )
    assert result.returncode != 0, output(result)
    assert "Download and verify helm-v3.0.0-linux-amd64.tar.gz" in result.stdout
    assert re.search(r"checksum", output(result), re.IGNORECASE), output(result)
    # Retried like a truncated transfer, then refused: nothing is left to install.
    assert requests.count("/helm.tar.gz") == 4, requests
    assert _cached(tmp_path) == []


@pytest.mark.parametrize(
    ("sum_file", "status"),
    [
        ("<html>Not the checksum</html>\n", 200),
        (f"{DIGEST[:63]}  short\n", 200),
        (f"{DIGEST}0  too-long\n", 200),
        (None, 404),
    ],
    ids=["no-digest", "short-digest", "long-digest", "missing-file"],
)
def test_unusable_checksum_file_downloads_nothing(
    tmp_path: Path, site, sum_file: str | None, status: int
) -> None:
    root, base, requests = site
    (root / "artifact").write_bytes(PAYLOAD)
    if sum_file is not None:
        (root / "artifact.sha256").write_text(sum_file)
    result = _download(
        tmp_path,
        verified_download_name="artifact",
        verified_download_url=f"{base}/artifact",
        verified_download_sha256_url=f"{base}/artifact.sha256",
    )
    assert result.returncode != 0, output(result)
    if status == 200:
        assert "refusing to install an unverified download" in output(result), output(result)
    else:
        assert requests.count("/artifact.sha256") == 4, requests
    assert "/artifact" not in requests
    assert _cached(tmp_path) == []


@pytest.mark.parametrize(
    "digests",
    [
        {},
        {"verified_download_sha256": DIGEST, "verified_download_sha256_url": "https://example.invalid/sum"},
    ],
    ids=["neither", "both"],
)
def test_exactly_one_digest_source_is_required(tmp_path: Path, digests: dict[str, str]) -> None:
    result = _download(
        tmp_path,
        verified_download_name="artifact",
        verified_download_url="https://example.invalid/artifact",
        **digests,
    )
    assert result.returncode != 0
    assert "exactly one of verified_download_sha256" in output(result), output(result)
    assert _cached(tmp_path) == []


# --- Claude Code: signed release manifest -------------------------------------

# The fixtures' own gpg, resolved before any test puts a wrapper on PATH.
GPG = shutil.which("gpg") or "gpg"


def _gpg(home: Path, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [GPG, "--homedir", str(home), "--batch", "--pinentry-mode", "loopback", "--passphrase", "", *args],
        capture_output=True, text=True, check=True, timeout=120, **kwargs,
    )


class SigningKey:
    def __init__(self, home: Path, uid: str) -> None:
        home.mkdir(mode=0o700)
        self.home = home
        _gpg(home, "--quick-gen-key", uid, "ed25519", "sign", "never")
        colons = _gpg(home, "--with-colons", "--list-keys").stdout
        self.fingerprint = next(
            line.split(":")[9] for line in colons.splitlines() if line.startswith("fpr:")
        )
        self.public = _gpg(home, "--armor", "--export", self.fingerprint).stdout

    def sign(self, path: Path) -> None:
        _gpg(self.home, "--yes", "--detach-sign", "--output", f"{path}.sig", str(path))

    def close(self) -> None:
        subprocess.run(["gpgconf", "--homedir", str(self.home), "--kill", "gpg-agent"], check=False)


@pytest.fixture(scope="module")
def keys(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[SigningKey, SigningKey]]:
    if shutil.which("gpg") is None:
        pytest.fail("gpg is required: the role verifies the Claude Code manifest with it")
    # gpg-agent's socket path must stay short; tmp_path_factory's base does.
    base = tmp_path_factory.mktemp("gpg")
    vendor = SigningKey(base / "v", "Fixture release key <release@example.invalid>")
    impostor = SigningKey(base / "i", "Impostor <impostor@example.invalid>")
    yield vendor, impostor
    vendor.close()
    impostor.close()


PLATFORMS = ("linux-x64", "linux-arm64")


def _publish_release(
    root: Path,
    signer: SigningKey,
    *,
    version: str = "1.2.3",
    manifest_version: str | None = None,
    platforms: tuple[str, ...] = PLATFORMS,
    binary: bytes = PAYLOAD,
) -> None:
    release = root / "claude-code-releases"
    (release / version).mkdir(parents=True)
    (release / "stable").write_text(f"{version}\n")
    manifest = {
        "version": manifest_version or version,
        "platforms": {name: {"checksum": DIGEST, "size": len(PAYLOAD)} for name in platforms},
    }
    (release / version / "manifest.json").write_text(json.dumps(manifest, indent=2))
    signer.sign(release / version / "manifest.json")
    for name in PLATFORMS:
        (release / version / name).mkdir()
        (release / version / name / "claude").write_bytes(binary)


def _claude_vars(tmp_path: Path, base: str, key: SigningKey, fingerprint: str | None = None) -> dict[str, Any]:
    key_file = tmp_path / "release-key.asc"
    key_file.write_text(key.public)
    return {
        "claude_native_channel": "stable",
        "claude_native_release_base_url": f"{base}/claude-code-releases",
        "claude_native_release_key_file": str(key_file),
        "claude_native_release_key_fingerprint": fingerprint or key.fingerprint,
        "claude_native_release_cache_dir": str(tmp_path / "cache" / "claude-code"),
        "claude_native_platform_map": DEFAULTS["claude_native_platform_map"],
        "dw_user": current_user(tmp_path),
    }


def _claude_tasks() -> list[dict[str, Any]]:
    """The release resolution and binary download exactly as claude-code-user.yml runs them."""
    tasks = role_tasks(
        TASKS / "claude-code-user.yml",
        "Resolve and verify the Claude Code release",
        "Download and verify the Claude Code binary",
    )
    for task in tasks:
        task.pop("when", None)
    return tasks


def _run_claude(tmp_path: Path, play_vars: dict[str, Any]):
    return run_playbook(tmp_path, _claude_tasks(), play_vars, gather_facts=True)


def _binary_requests(requests: list[str]) -> list[str]:
    return [path for path in requests if path.endswith("/claude")]


def test_signed_manifest_verifies_the_binary(tmp_path: Path, site, keys) -> None:
    root, base, requests = site
    vendor, _ = keys
    _publish_release(root, vendor)
    result = _run_claude(tmp_path, _claude_vars(tmp_path, base, vendor))
    assert result.returncode == 0, output(result)
    downloads = tmp_path / "home" / ".claude" / "downloads"
    binaries = sorted(downloads.iterdir())
    assert len(binaries) == 1 and binaries[0].name.startswith("claude-1.2.3-linux-")
    assert binaries[0].name.endswith(DIGEST)
    assert binaries[0].read_bytes() == PAYLOAD
    assert binaries[0].stat().st_mode & 0o777 == 0o700
    assert len(_binary_requests(requests)) == 1


def test_tampered_manifest_is_refused(tmp_path: Path, site, keys) -> None:
    root, base, requests = site
    vendor, _ = keys
    _publish_release(root, vendor)
    manifest = root / "claude-code-releases/1.2.3/manifest.json"
    manifest.write_text(manifest.read_text().replace(DIGEST, OTHER_DIGEST))
    result = _run_claude(tmp_path, _claude_vars(tmp_path, base, vendor))
    assert result.returncode != 0
    assert "Verify the Claude Code release manifest signature" in result.stdout
    assert "BADSIG" in output(result), output(result)
    assert _binary_requests(requests) == []


@pytest.fixture()
def gpg_without_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """gpg as on the CI runner: no /run/user, so the agent socket path overflows.

    There gpg does its work, then reports it cannot reach a gpg-agent and exits
    2 even after a good import or signature. The role must judge by the status
    lines, so this wrapper turns every successful gpg run into that exit.
    """
    real = shutil.which("gpg")
    assert real, "gpg is required"
    assert real == GPG, "the wrapper must wrap the fixtures' gpg"
    wrapper_dir = tmp_path / "agentless-bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "gpg"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'"{real}" "$@"\n'
        "rc=$?\n"
        '[ "$rc" -ne 0 ] && exit "$rc"\n'
        "echo \"gpg: can't connect to the gpg-agent: File name too long\" >&2\n"
        "exit 2\n"
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{wrapper_dir}{os.pathsep}{os.environ['PATH']}")


def test_signed_manifest_verifies_when_gpg_cannot_reach_an_agent(
    tmp_path: Path, site, keys, gpg_without_agent: None
) -> None:
    root, base, requests = site
    vendor, _ = keys
    _publish_release(root, vendor)
    result = _run_claude(tmp_path, _claude_vars(tmp_path, base, vendor))
    assert result.returncode == 0, output(result)
    assert len(_binary_requests(requests)) == 1


def test_tampered_manifest_is_refused_when_gpg_cannot_reach_an_agent(
    tmp_path: Path, site, keys, gpg_without_agent: None
) -> None:
    root, base, requests = site
    vendor, _ = keys
    _publish_release(root, vendor)
    manifest = root / "claude-code-releases/1.2.3/manifest.json"
    manifest.write_text(manifest.read_text().replace(DIGEST, OTHER_DIGEST))
    result = _run_claude(tmp_path, _claude_vars(tmp_path, base, vendor))
    assert result.returncode != 0
    assert "BADSIG" in output(result), output(result)
    assert _binary_requests(requests) == []


def test_a_key_file_that_imports_nothing_is_refused(tmp_path: Path, site, keys, gpg_without_agent: None) -> None:
    root, base, requests = site
    vendor, _ = keys
    _publish_release(root, vendor)
    play_vars = _claude_vars(tmp_path, base, vendor)
    Path(play_vars["claude_native_release_key_file"]).write_text("not a key\n")
    result = _run_claude(tmp_path, play_vars)
    assert result.returncode != 0
    assert "Import the pinned Claude Code release signing key" in result.stdout, output(result)
    assert _binary_requests(requests) == []


def test_manifest_signed_by_another_key_is_refused(tmp_path: Path, site, keys) -> None:
    root, base, requests = site
    vendor, impostor = keys
    _publish_release(root, impostor)
    result = _run_claude(tmp_path, _claude_vars(tmp_path, base, vendor))
    assert result.returncode != 0
    assert "Verify the Claude Code release manifest signature" in result.stdout
    assert "VALIDSIG" not in output(result)
    assert _binary_requests(requests) == []


def test_valid_signature_from_an_unpinned_key_is_refused(tmp_path: Path, site, keys) -> None:
    """gpg accepts any key in its keyring; the role also requires the pinned fingerprint."""
    root, base, requests = site
    vendor, impostor = keys
    _publish_release(root, vendor)
    result = _run_claude(tmp_path, _claude_vars(tmp_path, base, vendor, fingerprint=impostor.fingerprint))
    assert result.returncode != 0
    assert "Verify the Claude Code release manifest signature" in result.stdout
    assert "VALIDSIG" in output(result) and vendor.fingerprint in output(result)
    assert _binary_requests(requests) == []


@pytest.mark.parametrize(
    "release",
    [{"platforms": ()}, {"manifest_version": "1.2.2"}],
    ids=["no-platform-checksum", "other-version"],
)
def test_signed_manifest_without_a_usable_checksum_is_refused(
    tmp_path: Path, site, keys, release: dict[str, Any]
) -> None:
    root, base, requests = site
    vendor, _ = keys
    _publish_release(root, vendor, **release)
    result = _run_claude(tmp_path, _claude_vars(tmp_path, base, vendor))
    assert result.returncode != 0
    assert "refusing to install an unverified binary" in output(result), output(result)
    assert _binary_requests(requests) == []


def test_binary_that_does_not_match_the_manifest_is_refused(tmp_path: Path, site, keys) -> None:
    root, base, requests = site
    vendor, _ = keys
    _publish_release(root, vendor, binary=b"replaced on the CDN")
    result = _run_claude(tmp_path, _claude_vars(tmp_path, base, vendor))
    assert result.returncode != 0
    assert len(_binary_requests(requests)) == 4, requests
    downloads = tmp_path / "home" / ".claude" / "downloads"
    assert list(downloads.iterdir()) == []


@pytest.mark.parametrize("value", ["", "../escape", "latest/../x"])
def test_unexpected_channel_version_is_refused(tmp_path: Path, site, keys, value: str) -> None:
    root, base, requests = site
    vendor, _ = keys
    _publish_release(root, vendor)
    (root / "claude-code-releases/stable").write_text(value)
    result = _run_claude(tmp_path, _claude_vars(tmp_path, base, vendor))
    assert result.returncode != 0
    assert "did not name a release version" in output(result), output(result)
    assert not any("manifest" in path for path in requests)


# --- Codex: the release install.sh with its GitHub digest ---------------------

def _codex_tasks() -> list[dict[str, Any]]:
    return role_tasks(
        TASKS / "codex-standalone-install.yml",
        "Select the Codex installer from the latest release",
        "Download and verify the Codex standalone installer",
    )


def _codex_vars(tmp_path: Path, base: str, digest: str | None) -> dict[str, Any]:
    user = current_user(tmp_path)
    asset: dict[str, Any] = {"name": "install.sh", "browser_download_url": f"{base}/install.sh"}
    if digest is not None:
        asset["digest"] = digest
    return {
        "dev_user": user["name"],
        "dev_user_home": user["home"],
        "codex_standalone_enabled": True,
        "codex_standalone_release": "latest",
        "_codex_standalone_check": {"rc": 1},
        "latest_github_releases": {"codex": {"tag_name": "rust-v0.99.0", "assets": [asset]}},
    }


def test_codex_installer_verifies_against_the_release_digest(tmp_path: Path, site) -> None:
    root, base, _ = site
    (root / "install.sh").write_bytes(PAYLOAD)
    result = run_playbook(tmp_path, _codex_tasks(), _codex_vars(tmp_path, base, f"sha256:{DIGEST}"))
    assert result.returncode == 0, output(result)
    installer = tmp_path / "home/.cache/curiosity-corp/downloads" / f"codex-install.sh-{DIGEST}"
    assert installer.read_bytes() == PAYLOAD
    assert installer.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    ("digest", "message"),
    [(f"sha256:{OTHER_DIGEST}", "checksum"), (None, "refusing to run an unverified installer")],
    ids=["wrong-digest", "no-digest"],
)
def test_codex_installer_fails_closed(tmp_path: Path, site, digest: str | None, message: str) -> None:
    root, base, _ = site
    (root / "install.sh").write_bytes(PAYLOAD)
    result = run_playbook(tmp_path, _codex_tasks(), _codex_vars(tmp_path, base, digest))
    assert result.returncode != 0
    assert re.search(message, output(result), re.IGNORECASE), output(result)
    downloads = tmp_path / "home/.cache/curiosity-corp/downloads"
    assert not downloads.exists() or list(downloads.iterdir()) == []


# --- SDKMAN!: the release tag's commit, as GitHub's API names it --------------

def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=60,
        env=dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1"),
    ).stdout.strip()


@pytest.fixture()
def sdkman_remote(tmp_path: Path) -> tuple[str, str]:
    work = tmp_path / "sdkman-src"
    work.mkdir()
    _git("init", "-q", "-b", "master", cwd=work)
    (work / "README.md").write_text("fixture\n")
    _git("add", "README.md", cwd=work)
    _git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "release", cwd=work)
    _git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "tag", "-a", "5.0.0", "-m", "5.0.0", cwd=work)
    commit = _git("rev-parse", "HEAD", cwd=work)
    bare = tmp_path / "sdkman-cli.git"
    _git("clone", "-q", "--bare", str(work), str(bare))
    return f"file://{bare}", commit


def _sdkman_tasks(repo: str) -> list[dict[str, Any]]:
    tasks = role_tasks(
        TASKS / "languages.yml",
        "Install or update SDKMAN from its latest release tag",
        "Require the SDKMAN checkout to be the release commit",
    )
    assert tasks[0]["ansible.builtin.git"]["repo"] == "https://github.com/sdkman/sdkman-cli.git"
    tasks[0]["ansible.builtin.git"]["repo"] = repo
    return tasks


def _sdkman_vars(tmp_path: Path, commit: str) -> dict[str, Any]:
    user = current_user(tmp_path)
    return {
        "dev_user": user["name"],
        "dev_user_home": user["home"],
        "_sdkman_allow_source_update": True,
        "_sdkman_latest_release": {"json": {"tag_name": "5.0.0"}},
        "_sdkman_release_commit": {"json": {"sha": commit}},
    }


def test_sdkman_checkout_matches_the_release_commit(tmp_path: Path, sdkman_remote) -> None:
    repo, commit = sdkman_remote
    result = run_playbook(tmp_path, _sdkman_tasks(repo), _sdkman_vars(tmp_path, commit))
    assert result.returncode == 0, output(result)
    assert _git("rev-parse", "HEAD", cwd=tmp_path / "home/.sdkman") == commit


def test_sdkman_checkout_of_another_commit_stops(tmp_path: Path, sdkman_remote) -> None:
    repo, commit = sdkman_remote
    result = run_playbook(tmp_path, _sdkman_tasks(repo), _sdkman_vars(tmp_path, "0" * 40))
    assert result.returncode != 0
    assert "stopping before it is used" in output(result), output(result)


# --- The role uses these paths for every changed download ---------------------

def _include_vars(task_file: str, name: str) -> dict[str, Any]:
    tasks = yaml.safe_load((TASKS / task_file).read_text())
    task = next(task for task in tasks if task.get("name") == name)
    target = task["ansible.builtin.include_tasks"]
    target = target["file"] if isinstance(target, dict) else target
    assert target == "verified-download.yml", f"{name} must use verified-download.yml"
    return task["vars"]


@pytest.mark.parametrize(
    ("task_file", "name", "url", "digest_key", "digest"),
    [
        (
            "kubernetes.yml", "Download and verify latest kubectl",
            "https://dl.k8s.io/release/{{ _kubectl_latest.content | trim }}/bin/linux/amd64/kubectl",
            "verified_download_sha256_url",
            "https://dl.k8s.io/release/{{ _kubectl_latest.content | trim }}/bin/linux/amd64/kubectl.sha256",
        ),
        (
            "kubernetes.yml", "Download and verify latest Helm",
            "https://get.helm.sh/helm-{{ latest_github_releases.helm.tag_name }}-linux-amd64.tar.gz",
            "verified_download_sha256_url",
            "https://get.helm.sh/helm-{{ latest_github_releases.helm.tag_name }}-linux-amd64.tar.gz.sha256sum",
        ),
        (
            "languages.yml", "Download and verify latest Go release",
            "https://go.dev/dl/{{ _go_latest_asset.filename }}",
            "verified_download_sha256", "{{ _go_latest_asset.sha256 }}",
        ),
        (
            "codex-standalone-install.yml", "Download and verify the Codex standalone installer",
            "{{ _codex_installer_asset.browser_download_url }}",
            "verified_download_sha256", "{{ _codex_installer_asset.digest }}",
        ),
        (
            "claude-code-user.yml", "Download and verify the Claude Code binary",
            "{{ _claude_native_release.url }}",
            "verified_download_sha256", "{{ _claude_native_release.sha256 }}",
        ),
    ],
    ids=["kubectl", "helm", "go", "codex", "claude-code"],
)
def test_role_downloads_use_the_vendor_digest(
    task_file: str, name: str, url: str, digest_key: str, digest: str
) -> None:
    task_vars = _include_vars(task_file, name)
    assert task_vars["verified_download_url"] == url
    assert task_vars[digest_key] == digest


def test_installers_run_only_the_verified_file() -> None:
    text = "\n".join((TASKS / name).read_text() for name in ("ai-assistants.yml", "codex-standalone.yml", "codex-standalone-install.yml", "claude-code-user.yml", "kubernetes.yml", "languages.yml"))
    for retired in ("https://claude.ai/install.sh", "https://chatgpt.com/codex/install.sh", "/tmp/codex", "/tmp/claude"):
        assert retired not in text, f"{retired} is still downloaded or run"
    for retired in ("codex_standalone_installer_url", "claude_native_installer_url", "ziti_install_script_url"):
        assert retired not in DEFAULTS, f"{retired} is retired (catalog#171)"


def test_pinned_claude_release_key_has_the_pinned_fingerprint(tmp_path: Path) -> None:
    key_file = ROLE / "files" / DEFAULTS["claude_native_release_key_file"]
    home = tmp_path / "g"
    home.mkdir(mode=0o700)
    shown = subprocess.run(
        [GPG, "--homedir", str(home), "--batch", "--with-colons", "--show-keys", str(key_file)],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    primary = [line for line in shown.splitlines() if line.startswith(("pub:", "fpr:"))]
    assert primary[0].startswith("pub:") and primary[1].startswith("fpr:"), shown
    assert primary[1].split(":")[9] == DEFAULTS["claude_native_release_key_fingerprint"]


# catalog#177: run_playbook judges a network run by progress. These stand-ins
# write into the test's directory the way a download does, without a network.
def _writer(tmp_path: Path, seconds: int, every: float) -> list[dict[str, Any]]:
    target = tmp_path / "trickle"
    script = f"for i in $(seq {int(seconds / every)}); do printf x >> {target}; sleep {every}; done"
    return [{"name": "Trickle into the test directory", "ansible.builtin.shell": script}]


def test_a_run_that_keeps_writing_outlives_its_stall_window(tmp_path: Path) -> None:
    result = run_playbook(tmp_path, _writer(tmp_path, 12, 1), {}, timeout=60, stall=6, endpoint="a stand-in")
    assert result.returncode == 0, output(result)
    assert (tmp_path / "trickle").stat().st_size == 12


def test_a_run_that_stops_writing_fails_naming_endpoint_and_task(tmp_path: Path) -> None:
    stuck = [{"name": "Hang without writing", "ansible.builtin.command": "sleep 300"}]
    with pytest.raises(AssertionError) as raised:
        run_playbook(tmp_path, stuck, {}, timeout=120, stall=6, endpoint="a stand-in")
    message = str(raised.value)
    assert "made no progress for 6 s" in message, message
    assert "reaching a stand-in" in message and "TASK [Hang without writing]" in message, message
    assert "MB written under the test directory" in message, message


def test_a_run_past_its_ceiling_fails_even_while_writing(tmp_path: Path) -> None:
    with pytest.raises(AssertionError) as raised:
        run_playbook(tmp_path, _writer(tmp_path, 60, 1), {}, timeout=8, stall=6, endpoint="a stand-in")
    assert "did not finish within 8 s" in str(raised.value), str(raised.value)
