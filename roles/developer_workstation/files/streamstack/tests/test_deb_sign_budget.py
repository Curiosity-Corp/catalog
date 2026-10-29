"""deb-sign.sh budgets the signing pod's image pull apart from the signing itself (main pipeline 98029).

A cold node pulls the ~1 GB signing image through the Harbor proxy in minutes; that used to eat the whole
600-second signing deadline. These tests run the dispatcher against a fake kubectl and a no-op sleep, so each
loop turn is five simulated seconds.
"""

from __future__ import annotations

import base64
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "deb-sign.sh"

FAKE_KUBECTL = """#!/bin/sh
# turn N of the dispatcher's wait loop is counted on `get job`; the pod pulls until START, the job completes at DONE
state="$FAKE_DIR/turn"
case "$*" in
  *"get job"*"conditions"*)
    n=$(( $(cat "$state" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$state"
    [ "$FAKE_DONE" -gt 0 ] && [ "$n" -ge "$FAKE_DONE" ] && printf 'Complete'
    exit 0 ;;
  *"get pods"*'@.name=="sign"'*)
    n=$(cat "$state" 2>/dev/null || echo 0)
    if [ "$FAKE_START" -gt 0 ] && [ "$n" -ge "$FAKE_START" ]; then
      printf '{"running":{}}'
    else
      printf '{"waiting":{"reason":"PodInitializing"}}'
    fi
    exit 0 ;;
  *"get pods"*) exit 0 ;;
  *"logs"*"-c sign"*) cat "$FAKE_DIR/pod.log"; exit 0 ;;
  *"get job"*) echo "fake job yaml"; exit 0 ;;
  *) cat >/dev/null 2>&1 || true; exit 0 ;;
esac
"""


def rig(tmp_path: Path, start: int, done: int, pull: int = 900, sign: int = 600) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {"kubectl": FAKE_KUBECTL, "sleep": "#!/bin/sh\nexit 0\n"}.items():
        tool = bin_dir / name
        tool.write_text(body)
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    blob = base64.b64encode(b"signature").decode()
    (tmp_path / "pod.log").write_text(
        "".join(f"SIGN-BEGIN {n}\n{blob}\nSIGN-END\n" for n in ("DEB-SHA256SUMS.cose.sig", "InRelease", "Release.gpg"))
    )
    (tmp_path / "SUMS").write_text("abc  streamstack_0.0.0_all.deb\n")
    (tmp_path / "Release").write_text("Origin: StreamStack\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DIR": str(tmp_path),
        "FAKE_START": str(start),
        "FAKE_DONE": str(done),
        "PULL_TIMEOUT": str(pull),
        "SIGN_TIMEOUT": str(sign),
        "APT_KEY_FINGERPRINT": "F" * 40,
        "CI_PROJECT_ID": "1",
        "CI_PIPELINE_IID": "2",
        "CI_JOB_ID": "3",
    }
    return subprocess.run(
        ["sh", str(SCRIPT), str(tmp_path / "SUMS"), str(tmp_path / "Release"), str(tmp_path / "out")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_slow_image_pull_does_not_spend_the_signing_budget(tmp_path: Path) -> None:
    # the pod pulls for 300 simulated seconds, five times the 60-second signing budget, then signs in 20
    done = rig(tmp_path, start=60, done=64, pull=900, sign=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "sign container started after" in done.stdout
    assert (tmp_path / "out" / "InRelease").read_bytes() == b"signature"


def test_a_pod_that_never_starts_fails_with_the_pull_budget(tmp_path: Path) -> None:
    done = rig(tmp_path, start=0, done=0, pull=120, sign=600)
    assert done.returncode == 1
    assert "signing pod did not start within 120s" in done.stdout


def test_a_hung_signing_step_still_times_out_on_the_signing_budget(tmp_path: Path) -> None:
    done = rig(tmp_path, start=3, done=0, pull=900, sign=60)
    assert done.returncode == 1
    assert "=== signing job timed out ===" in done.stdout


def test_the_job_deadline_covers_both_budgets() -> None:
    assert "activeDeadlineSeconds: $((PULL_TIMEOUT + SIGN_TIMEOUT))" in SCRIPT.read_text(encoding="utf-8")
