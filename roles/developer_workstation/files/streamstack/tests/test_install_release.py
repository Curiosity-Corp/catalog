"""scripts/install-release.sh: checksum gate and signature handling, with a stub curl serving a fake release."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-release.sh"
VERSION = "0.0.8"
WHEEL = f"streamstack-{VERSION}-py3-none-any.whl"
SDIST = f"streamstack-{VERSION}.tar.gz"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def release_dir(tmp_path: Path) -> Path:
    """What the Generic Package Registry would serve for the release."""
    out = tmp_path / "served"
    out.mkdir()
    files = {WHEEL: b"wheel bytes", SDIST: b"sdist bytes", "python-sbom.cdx.json": b'{"bomFormat":"CycloneDX"}'}
    for name, data in files.items():
        (out / name).write_bytes(data)
    (out / "RELEASE-SHA256SUMS").write_text("".join(f"{_sha(data)}  {name}\n" for name, data in files.items()))
    (out / "RELEASE-SHA256SUMS.cose.sig").write_bytes(b"signature")
    return out


@pytest.fixture
def stub_path(tmp_path: Path, release_dir: Path) -> str:
    """A PATH whose curl copies from the served directory (and records that the token never reached argv)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        'out=""; url=""\n'
        'while [ $# -gt 0 ]; do case "$1" in\n'
        '  -o) out="$2"; shift ;; -K|--retry) shift ;; -*) ;; *) url="$1" ;;\n'
        "esac; shift; done\n"
        "cat >/dev/null\n"
        f'cp "{release_dir}/$(basename "$url")" "$out"\n'
    )
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    return f"{bin_dir}:{os.environ['PATH']}"


# The script forks a few dozen processes (five stub downloads, sha256sum, grep) and never touches the network under
# the stub curl, so the only way it takes long is a starved runner: the deadline is a hang guard, not a speed check.
SCRIPT_DEADLINE_S = 600


def _run(stub_path: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the script hermetically: stub curl first on PATH, no inherited stdin, and temp files and HOME kept in the
    stub's own directory (stub_path starts with it), so no real config, token helper or shared /tmp can interfere."""
    sandbox = Path(stub_path.split(os.pathsep, 1)[0]).parent
    env = dict(
        os.environ,
        PATH=stub_path,
        GITLAB_TOKEN="secret-token",
        HOME=str(sandbox),
        TMPDIR=str(sandbox),
        STREAMSTACK_GITLAB_HOST="gitlab.example",
    )
    env.pop("STREAMSTACK_SIGNING_CERT", None)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        stdin=subprocess.DEVNULL,
        timeout=SCRIPT_DEADLINE_S,
    )


def test_verify_only_passes_matching_checksums_and_says_the_signature_was_not_checked(stub_path: str) -> None:
    if shutil.which("notation"):
        pytest.skip("a real notation on PATH changes the signature path")
    result = _run(stub_path, "--verify-only", VERSION)
    assert result.returncode == 0, result.stderr
    assert f"{WHEEL}: OK" in result.stdout
    assert "signature unchecked" in result.stdout
    assert "NOT verified" in result.stderr
    assert "secret-token" not in result.stdout + result.stderr


def test_a_tampered_wheel_is_refused(stub_path: str, release_dir: Path) -> None:
    (release_dir / WHEEL).write_bytes(b"tampered")
    result = _run(stub_path, "--verify-only", VERSION)
    assert result.returncode != 0
    assert f"{WHEEL}: FAILED" in result.stdout


def test_a_file_missing_from_the_manifest_is_refused(stub_path: str, release_dir: Path) -> None:
    manifest = release_dir / "RELEASE-SHA256SUMS"
    manifest.write_text("".join(line for line in manifest.read_text().splitlines(keepends=True) if "sbom" not in line))
    result = _run(stub_path, "--verify-only", VERSION)
    assert result.returncode != 0
    assert "not listed in the signed manifest" in result.stderr


def test_require_signature_fails_without_notation_and_a_certificate(stub_path: str) -> None:
    result = _run(stub_path, "--verify-only", "--require-signature", VERSION)
    assert result.returncode != 0
    assert "--require-signature needs notation" in result.stderr


def test_a_version_is_required_and_a_token_too(stub_path: str) -> None:
    assert _run(stub_path, "--verify-only").returncode != 0
    assert _run(stub_path, "--verify-only", "latest").returncode != 0
    env = dict(os.environ, PATH=stub_path, STREAMSTACK_GITLAB_HOST="gitlab.example")
    env.pop("GITLAB_TOKEN", None)
    no_glab = subprocess.run(
        ["bash", str(SCRIPT), "--verify-only", VERSION],
        capture_output=True,
        text=True,
        env=dict(env, PATH=stub_path.split(":")[0] + ":/usr/bin:/bin"),
        check=False,
    )
    if shutil.which("glab", path="/usr/bin:/bin") is None:
        assert no_glab.returncode != 0
        assert "no GitLab token" in no_glab.stderr


def test_the_release_host_is_configuration_only(stub_path: str, tmp_path: Path) -> None:
    """No host in the script: with none configured (and no git checkout to infer one from) it says what to set."""
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    copy = lonely / "install-release.sh"
    copy.write_text(SCRIPT.read_text())
    sandbox = Path(stub_path.split(os.pathsep, 1)[0]).parent
    env = {k: v for k, v in os.environ.items() if k not in ("STREAMSTACK_GITLAB_HOST", "CI_SERVER_HOST")}
    env.update(PATH=stub_path, GITLAB_TOKEN="secret-token", HOME=str(sandbox), GIT_CEILING_DIRECTORIES=str(tmp_path))
    result = subprocess.run(
        ["bash", str(copy), "--verify-only", VERSION],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        stdin=subprocess.DEVNULL,
        timeout=SCRIPT_DEADLINE_S,
    )
    assert result.returncode != 0
    assert "STREAMSTACK_GITLAB_HOST" in result.stderr
    env["CI_SERVER_HOST"] = "gitlab.example"  # GitLab CI configures it
    ok = subprocess.run(
        ["bash", str(copy), "--verify-only", VERSION],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        stdin=subprocess.DEVNULL,
        timeout=SCRIPT_DEADLINE_S,
    )
    assert ok.returncode == 0, ok.stderr
