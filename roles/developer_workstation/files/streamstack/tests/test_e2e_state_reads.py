"""Guard: an e2e test does not sample the server's state right after a UI action without waiting for it.

The page posts on its own connection and a fader drag posts a throttled stream, so the server lags the page by a few
requests. Reading ``rig.state()`` straight after a click or a drag, and comparing it with what the page shows, is a race
(main pipelines 96021 and 96223, and 95914 before them). Wait with ``rig.wait_for_state(path, expected)``, ``poll`` or
``poll_settled`` instead. A read that is deliberately immediate (a precondition, a catalogue that no action changes, a
check that nothing was sent) says why on its own line with ``# state-read: <reason>``.

Two shapes are flagged, in every ``tests/e2e/test_*.py``:

* a statement that reads ``.state()`` directly after a statement that is a UI action (click, press, drag, tap, hold...);
* a ``.state()`` read anywhere after the first ``drag_fader`` in a test.

Reads inside a ``lambda`` are the polling probes themselves and are always fine.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

E2E = Path(__file__).parent / "e2e"
ACTIONS = {
    "click", "dblclick", "press", "fill", "check", "uncheck", "select_option", "type", "tap", "hold", "drag_fader",
    "set_checked", "dispatch_event",
}  # fmt: skip
MARK = "# state-read:"
COMPOUND = (ast.If, ast.While, ast.For, ast.With, ast.Try, ast.FunctionDef)


def direct_state_reads(node: ast.AST) -> list[ast.Call]:
    """``x.state()`` calls under ``node`` that are not inside a lambda (a polling probe)."""
    found: list[ast.Call] = []

    def walk(current: ast.AST) -> None:
        if isinstance(current, ast.Lambda):
            return
        if (
            isinstance(current, ast.Call)
            and isinstance(current.func, ast.Attribute)
            and current.func.attr == "state"
            and not current.args
        ):
            found.append(current)
        for child in ast.iter_child_nodes(current):
            walk(child)

    walk(node)
    return found


def action_name(stmt: ast.stmt) -> str | None:
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        func = stmt.value.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        return name if name in ACTIONS else None
    return None


def offences(source: str) -> list[tuple[int, str]]:
    lines = source.splitlines()
    tree = ast.parse(source)
    out: dict[int, str] = {}

    def excused(stmt: ast.stmt) -> bool:
        return any(MARK in lines[n - 1] for n in range(stmt.lineno - 1, (stmt.end_lineno or stmt.lineno) + 1) if n >= 1)

    def own(stmt: ast.stmt) -> ast.AST:
        """The statement without its nested bodies, so a read in an ``if`` body is judged on its own line."""
        return stmt.test if isinstance(stmt, (ast.If, ast.While)) else stmt

    for owner in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(owner, field, None)
            if not isinstance(body, list):
                continue
            for previous, stmt in itertools.pairwise(body):
                if (
                    isinstance(stmt, ast.stmt)
                    and action_name(previous)
                    and direct_state_reads(own(stmt))
                    and not excused(stmt)
                ):
                    out[stmt.lineno] = f"state read right after {action_name(previous)}()"
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef) or not func.name.startswith("test_"):
            continue
        drags = [
            n.lineno for n in ast.walk(func) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "drag_fader"
        ]
        if not drags:
            continue
        for stmt in ast.walk(func):
            simple = isinstance(stmt, ast.stmt) and not isinstance(stmt, COMPOUND)
            if simple and stmt.lineno > min(drags) and direct_state_reads(stmt) and not excused(stmt):
                out.setdefault(stmt.lineno, "state read after drag_fader()")
    return sorted((number, why) for number, why in out.items())


def test_no_e2e_test_samples_server_state_straight_after_a_ui_action() -> None:
    bad = [
        f"{path.name}:{number}: {why}"
        for path in sorted(E2E.glob("test_*.py"))
        for number, why in offences(path.read_text())
    ]
    assert not bad, (
        "wait with rig.wait_for_state(path, expected) / poll / poll_settled, or say why the read is immediate with "
        f"'# state-read: <reason>' on the line above: {bad}"
    )


def test_the_guard_flags_the_racy_shapes_and_accepts_the_waits() -> None:
    racy = "def test_x(rig, page):\n    page.click()\n    assert rig.state()['a'] == 1\n"
    assert offences(racy) == [(3, "state read right after click()")]
    dragged = (
        "def test_x(rig, app):\n    app.drag_fader(f, 1)\n    x = poll(rig, lambda s: s['a'])\n"
        "    y = rig.state()['b']\n"
    )
    assert offences(dragged) == [(4, "state read after drag_fader()")]
    fine = (
        "def test_x(rig, page):\n    before = rig.state()['a']\n    page.click()\n"
        "    assert poll(rig, lambda s: s['a'] == 1)\n    assert rig.wait_for_state('a', 1)\n"
        "    page.click()\n    # state-read: nothing is posted until Apply\n    assert rig.state()['a'] == before\n"
    )
    assert offences(fine) == []
