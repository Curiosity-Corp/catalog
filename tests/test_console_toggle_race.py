"""A toggle key pressed again must undo its last press, however late the server's snapshots are (#167).

The B key sent `on: !shown`. A snapshot built before the first press, delivered after the second, agreed with the
second press and released its hold early; the snapshot built between the presses then showed the first value again, and
a third press inverted that: it posted the state the server already had and did nothing (the e2e A/B test waited 30 s
for a bypass that never came). Toggles now invert what the person last asked for (`store.intent`). The test runs the
real browser modules under Node, so the ordering is exact instead of timing-dependent.
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
SCRIPT = Path(__file__).parent / "fixtures" / "toggle_race.mjs"
OPS = (CONSOLE / "core" / "ops.js").read_text(encoding="utf-8")
TOGGLES = re.findall(r"^export const (toggle\w+) = ", OPS, flags=re.M)

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


@needs_node
def test_three_presses_alternate_when_the_snapshots_arrive_late() -> None:
    done = subprocess.run(["node", str(SCRIPT), str(CONSOLE)], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    for name, asked in result.items():
        assert asked == [True, False, True], f"{name} posted {asked}"
    # the gate: a toggle added to ops.js without a case in the fixture would go unchecked
    assert sorted(result) == sorted(TOGGLES)


@pytest.mark.parametrize("name", TOGGLES)
def test_a_toggle_never_inverts_the_value_on_screen(name: str) -> None:
    """Gate for every entry point (a key, the palette, a button) that goes through ops.js."""
    line = next(ln for ln in OPS.splitlines() if ln.startswith(f"export const {name} = "))
    assert "store.state" not in line and "store.intent(" in line, line
