"""The scripted call-app matrix on each browser's own test camera: every installed browser lists it and gets frames."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from streamstack import callmatrix

REPO = Path(__file__).resolve().parents[2]
# In its own process: the suite's session-wide Playwright driver holds an asyncio loop in this one, and a second
# sync Playwright cannot start inside it (the same cause as pipeline 96925).
RUN = (
    "import json; from streamstack import callmatrix; "
    "print(json.dumps([[r.name, r.status, r.note] for r in callmatrix.run_matrix(fake=True, seconds=2.0)]))"
)


@pytest.fixture(scope="module")
def results() -> dict[str, callmatrix.Result]:
    done = subprocess.run(
        [sys.executable, "-c", RUN], capture_output=True, text=True, timeout=300, check=False,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    rows = json.loads(done.stdout.strip().splitlines()[-1])
    return {name: callmatrix.Result(name, status, note) for name, status, note in rows}


def test_chromium_and_firefox_list_the_camera_and_get_frames(results) -> None:
    for name in ("Chromium", "Firefox"):
        assert results[name].status == "pass", (name, results[name])


def test_no_installed_browser_fails(results) -> None:
    assert [n for n, r in results.items() if r.status == "fail"] == []
    assert callmatrix.exit_code(list(results.values())) == 0
