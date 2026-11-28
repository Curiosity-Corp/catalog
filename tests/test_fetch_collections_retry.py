"""scripts/fetch-collections.sh survives a transient Galaxy reset (catalog#182).

On 2026-11-28 galaxy.ansible.com reset connections (`<urlopen error [Errno
104] Connection reset by peer>`) and turned ready merge requests red. These
tests replay that failure with a stand-in `ansible-galaxy` on PATH that fails
the way the real one did for its first N fetches, then succeeds, and check
that the one fetch job retries through it, gives up with a clear message
after its bounded tries, treats a hang as a failed try, and makes no fetch at
all when the cache already verifies.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RESET = (
    "ERROR! Unknown error when attempting to call Galaxy at "
    "'https://galaxy.ansible.com/api/': <urlopen error [Errno 104] Connection reset by peer>"
)

# The stand-in: `collection install|download` fails like Galaxy did while the
# counter in $FAKE_STATE/fails is above zero (or sleeps when $FAKE_HANG is
# set), then writes what the real command writes. `collection verify
# --offline` passes when the target holds a marker for that requirement.
FAKE_GALAXY = r"""#!/bin/sh
set -eu
state="$FAKE_STATE"
echo "$*" >> "$state/calls"
[ "$1" = collection ] || { echo "unexpected: $*" >&2; exit 2; }
sub="$2"; shift 2
dest=""; reqs=""; spec=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -p) dest="$2"; shift 2 ;;
    -r) reqs="$2"; shift 2 ;;
    --timeout) shift 2 ;;
    --*) shift ;;
    *) spec="$1"; shift ;;
  esac
done
key="${reqs:-$spec}"
case "$sub" in
  verify)
    grep -qxF "$key" "$ANSIBLE_COLLECTIONS_PATH/.fake-installed" 2>/dev/null ;;
  install|download)
    fails="$(cat "$state/fails")"
    if [ "$fails" -gt 0 ]; then
      echo $((fails - 1)) > "$state/fails"
      if [ -n "${FAKE_HANG:-}" ]; then exec sleep 30; fi
      echo "$FAKE_RESET" >&2
      exit 1
    fi
    mkdir -p "$dest"
    echo "$key" >> "$dest/.fake-installed"
    [ "$sub" = download ] && echo "collections: []" > "$dest/requirements.yml"
    exit 0 ;;
esac
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A copy of the files the fetch reads, with the stand-in on PATH."""
    work = tmp_path / "repo"
    (work / "scripts").mkdir(parents=True)
    (work / "ci").mkdir()
    for rel in ("scripts/fetch-collections.sh", "scripts/check-collection-floor.sh", "galaxy.yml", "requirements.yml", "ci/requirements.yml"):
        shutil.copy2(ROOT / rel, work / rel)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "ansible-galaxy"
    fake.write_text(FAKE_GALAXY)
    fake.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    (state / "fails").write_text("0\n")
    return work


def run(repo: Path, fails: int, **env: str) -> subprocess.CompletedProcess[str]:
    state = repo.parent / "state"
    (state / "fails").write_text(f"{fails}\n")
    full_env = {
        **os.environ,
        "PATH": f"{repo.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "FAKE_STATE": str(state),
        "FAKE_RESET": RESET,
        "FETCH_BACKOFF": "0",
        **env,
    }
    return subprocess.run(
        ["sh", "scripts/fetch-collections.sh"], cwd=repo, env=full_env, capture_output=True, text=True, timeout=120
    )


def fetch_calls(repo: Path) -> list[str]:
    calls = (repo.parent / "state" / "calls").read_text().splitlines()
    return [c for c in calls if c.split()[1] in ("install", "download")]


def test_a_reset_on_every_fetch_is_retried_through(repo: Path) -> None:
    """The first try of the first fetch resets; the job still succeeds."""
    result = run(repo, fails=2)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("Errno 104") == 2
    assert "install ci/requirements.yml attempt 1 failed (exit 1); retrying" in result.stderr
    assert "install ci/requirements.yml attempt 2 failed (exit 1); retrying" in result.stderr
    # Two failed tries plus one good one for the pin, then floor and user once.
    assert len(fetch_calls(repo)) == 5
    for path in (".ci-collections", ".ci-collections-floor", ".ci-collections-user"):
        assert (repo / path / ".fake-installed").is_file()
    assert (repo / ".ci-collections-user" / "requirements.yml").is_file()


def test_retries_are_bounded_and_say_why(repo: Path) -> None:
    result = run(repo, fails=99, FETCH_ATTEMPTS="3")
    assert result.returncode != 0
    assert "install ci/requirements.yml failed after 3 attempts (exit 1)" in result.stderr
    assert len(fetch_calls(repo)) == 3


def test_a_hung_try_is_cut_off_and_retried(repo: Path) -> None:
    result = run(repo, fails=1, FAKE_HANG="1", FETCH_TRY_TIMEOUT="2")
    assert result.returncode == 0, result.stderr
    assert "no answer from galaxy.ansible.com within 2 s" in result.stderr


def test_a_verified_cache_makes_no_fetch_for_the_pins(repo: Path) -> None:
    assert run(repo, fails=0).returncode == 0
    (repo.parent / "state" / "calls").unlink()
    result = run(repo, fails=0)
    assert result.returncode == 0, result.stderr
    assert "from cache, verified offline" in result.stdout
    # Only the user download, which follows new releases, goes out again.
    assert [c.split()[1] for c in fetch_calls(repo)] == ["download"]
