"""A held plain value is released by a snapshot built after the action, never by an older one that equals it (#169).

The store released a settled hold as soon as any snapshot carried the held value. A snapshot built before the action
that happened to equal it (the same control set to that value earlier) released the hold early, and the snapshot built
between the actions then showed the older value again until the next one. The test runs the real browser modules under
Node with a fake clock, so the order is exact instead of timing-dependent.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "src" / "streamstack" / "data" / "console"
SCRIPT = Path(__file__).parent / "fixtures" / "plain_hold_race.mjs"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


@needs_node
@pytest.mark.parametrize("name", ["mute", "bypass", "gain"])
def test_a_stale_snapshot_that_equals_the_held_value_does_not_release_the_hold(name: str) -> None:
    done = subprocess.run(["node", str(SCRIPT), str(CONSOLE)], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    case = json.loads(done.stdout)[name]
    seen, after, before = case["seen"], case["after"], case["before"]
    assert seen["clicked"] == after
    assert seen["staleButEqual"] == after, "an older snapshot that equals the value released the hold"
    assert seen["staleOld"] == after, "an older snapshot showed through the hold"
    assert seen["carried"] == after
    assert seen["later"] == before, "the hold outlived the snapshot that carried the change"
    assert seen["acked"] == after


def settle_calls() -> list[tuple[Path, str]]:
    calls = []
    for path in sorted(CONSOLE.rglob("*.js")):
        if path.name == "store.js":
            continue
        text = path.read_text(encoding="utf-8")
        calls += [(path, m.group(0)) for m in re.finditer(r"store\.settle\((?:[^()]|\([^()]*\))*\)", text)]
    return calls


def test_the_gate_sees_the_settle_calls() -> None:
    assert any(p.name == "actions.js" for p, _ in settle_calls())


@pytest.mark.parametrize(("path", "call"), settle_calls() or [(Path("none"), "")], ids=lambda v: str(v)[-60:])
def test_every_settle_call_releases_by_seq(path: Path, call: str) -> None:
    """Gate: a `store.settle` that does not pass the answer's `seq`, or that opts out with `bySeq: false`, releases a
    plain value on agreement with a stale snapshot. If one must (document why here), name it in this test."""
    assert "seq" in call, f"{path.name}: {call} does not pass the answer's seq"
    assert "bySeq: false" not in call, f"{path.name}: {call} opts out of the seq release"
