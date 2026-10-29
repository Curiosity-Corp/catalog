"""Gate for the e2e server fixture (issue #124): it does not yield until the console is ready and its files are warm.

The e2e suite needs Playwright, which the unit job does not install. This proves the readiness code every CI shard
runs (tests/e2e/readiness.py) against the real console files and a fake server, and then checks by source that the
fixtures keep calling it: the ``rig`` fixture gates before it yields, and every browser context waits for a warmed
engine, so a refactor cannot quietly put the first Firefox navigation back on a cold server and a cold browser.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "tests" / "e2e"
CONSOLE = ROOT / "src" / "streamstack" / "data" / "console"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("e2e_readiness", E2E / "readiness.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


readiness = _load()


def disk_fetch(path: str) -> tuple[int, bytes]:
    # what the server does: "/" is index.html and "static/<x>" is <x> under the console directory
    if path and not path.startswith("static/"):
        return 404, b""  # the server answers only "/" and "/static/..."
    target = CONSOLE / (path.removeprefix("static/") if path else "index.html")
    return (200, target.read_bytes()) if target.is_file() else (404, b"")


def test_the_crawl_reaches_the_whole_module_graph_of_the_real_console() -> None:
    loaded = readiness.load_graph(disk_fetch)
    assert "" in loaded.files and "static/app.js" in loaded.files and "static/theme.css" in loaded.files
    # placeholder.js is no route's `load` any more (nothing imports it), so no browser ever fetches it
    pages = {p.name for p in (CONSOLE / "pages").glob("*.js")} - {"placeholder.js"}
    reached = {Path(p).name for p in loaded.files if p.startswith("static/pages/")}
    assert pages <= reached, f"pages the crawl never reached (lazy imports): {sorted(pages - reached)}"
    assert loaded.total_bytes > 100_000
    assert "console first load:" in loaded.summary()


def test_a_missing_file_of_the_page_fails_the_gate_with_its_name() -> None:
    def fetch(path: str) -> tuple[int, bytes]:
        if path == "static/app.js":
            return 500, b""
        return disk_fetch(path)

    with pytest.raises(readiness.NotReady, match=r"static/app\.js.*HTTP 500"):
        readiness.load_graph(fetch)


def test_the_gate_blocks_until_the_server_answers_and_only_then_reads_the_files() -> None:
    events: list[str] = []
    answers = iter([False, OSError("connection refused"), False, True])
    now = [0.0]

    def probe() -> bool:
        events.append("probe")
        outcome = next(answers)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def fetch(path: str) -> tuple[int, bytes]:
        events.append("fetch")
        return disk_fetch(path)

    def sleep(seconds: float) -> None:
        now[0] += seconds

    loaded = readiness.gate(fetch, probe, timeout=30, interval=1.0, clock=lambda: now[0], sleep=sleep)
    assert events[:4] == ["probe"] * 4 and set(events[4:]) == {"fetch"}, "no file is read before the server is ready"
    assert loaded.api_wait_s == pytest.approx(3.0)


def test_a_server_that_never_gets_ready_fails_instead_of_letting_a_test_navigate() -> None:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(readiness.NotReady, match="not ready after 5 s"):
        readiness.gate(disk_fetch, lambda: False, timeout=5, interval=1.0, clock=lambda: now[0], sleep=sleep)


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def _calls(node: ast.AST) -> list[tuple[int, str]]:
    found = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            found.append((n.lineno, f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")))
    return found


def test_the_rig_fixture_gates_on_readiness_before_it_yields() -> None:
    tree = ast.parse((E2E / "conftest.py").read_text(encoding="utf-8"))
    rig = _function(tree, "rig")
    gate = [line for line, name in _calls(rig) if name == "gate"]
    yields = [n.lineno for n in ast.walk(rig) if isinstance(n, ast.Yield)]
    assert gate and yields and max(gate) < min(yields), "rig must call readiness.gate(...) before `yield server`"


def test_every_context_waits_for_a_warm_engine_and_the_warm_up_navigates_to_the_console() -> None:
    tree = ast.parse((E2E / "conftest.py").read_text(encoding="utf-8"))
    context = _function(tree, "context")
    assert "warm_engine" in ast.unparse(context), "the context fixture must depend on the warmed engine"
    warm = _function(tree, "warm_engine")
    assert {"goto", "close"} <= {name for _, name in _calls(warm)}
