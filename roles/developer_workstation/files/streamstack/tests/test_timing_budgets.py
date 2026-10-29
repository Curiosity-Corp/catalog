"""Every cost budget in the test suite takes the best of several runs (#71, #164).

A single timed run fails whenever a busy CI runner preempts it, and that turned main and merge request pipelines red
three times. Contention only ever adds time, so the cheapest of several runs is the real cost: a regression raises
every run and still fails. This gate finds tests that time code with a benchmark clock and checks they take `min()`.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
CLOCKS = {"perf_counter", "perf_counter_ns", "process_time", "process_time_ns", "thread_time", "thread_time_ns"}


def single_run_budgets(source: str) -> list[str]:
    """Test functions in `source` that read a benchmark clock but never take the best of several runs."""
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        calls = [n.func for n in ast.walk(node) if isinstance(n, ast.Call)]
        timed = any(isinstance(f, ast.Attribute) and f.attr in CLOCKS for f in calls)
        best_of = any(isinstance(f, ast.Name) and f.id == "min" for f in calls)
        if timed and not best_of:
            offenders.append(node.name)
    return offenders


def test_every_cost_budget_takes_the_best_of_several_runs() -> None:
    offenders = [
        f"{path.name}::{name}"
        for path in sorted(TESTS.rglob("test_*.py"))
        for name in single_run_budgets(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"time several runs and assert on min(): {offenders}"


def test_the_gate_catches_a_single_wall_clock_run() -> None:
    single = (
        "def test_cost():\n"
        "    started = time.perf_counter()\n"
        "    work()\n"
        "    assert time.perf_counter() - started < 0.001\n"
    )
    best_of = (
        "def test_cost():\n"
        "    costs = []\n"
        "    for _ in range(5):\n"
        "        started = time.thread_time()\n"
        "        work()\n"
        "        costs.append(time.thread_time() - started)\n"
        "    assert min(costs) < 0.001\n"
    )
    assert single_run_budgets(single) == ["test_cost"]
    assert single_run_budgets(best_of) == []
