"""Gate for the sharded e2e job (issue #172): the shards partition the suite exactly.

The e2e suite needs Playwright, which the unit job does not install, so the suite cannot be collected here. The gate
instead feeds the same selection code every CI shard runs (tests/e2e/sharding.py) three kinds of node ids: the real,
parametrised ids recorded in tests/e2e/timings.json, every test function found in tests/e2e/test_*.py by its source
(which may or may not be in the timing file), and made-up ids that are in neither. For every shard count in use and a
spread of others, each id must be in exactly one shard, so no test can silently drop out.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "tests" / "e2e"


def _load_sharding() -> ModuleType:
    spec = importlib.util.spec_from_file_location("e2e_sharding", E2E / "sharding.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sharding = _load_sharding()


def _declared_ids() -> set[str]:
    """``file::[Class::]test_name`` for every test function in the e2e sources, parametrisation not expanded."""
    ids: set[str] = set()
    for path in sorted(E2E.glob("test_*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
                ids.add(f"{rel}::{node.name}")
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                ids.update(
                    f"{rel}::{node.name}::{f.name}"
                    for f in node.body
                    if isinstance(f, ast.FunctionDef) and f.name.startswith("test")
                )
    return ids


def _ci_shard_count() -> int:
    ci = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
    job = re.search(r"^e2e:\n(.*?)(?=^\S)", ci, re.S | re.M)
    assert job, "the e2e job is missing from .gitlab-ci.yml"
    found = re.search(r"^  parallel: (\d+)\s*$", job.group(1), re.M)
    assert found, "the e2e job must set a plain `parallel: N` so every shard is run"
    return int(found.group(1))


TIMINGS = sharding.load_timings()
DECLARED = _declared_ids()
UNKNOWN = {f"tests/e2e/test_new_page.py::test_unrecorded_{i}[chromium]" for i in range(37)}
UNIVERSE = sorted(set(TIMINGS) | DECLARED | UNKNOWN)
COUNTS = sorted({1, 2, 3, 4, 5, 7, 8, 16, 64, _ci_shard_count()})


def test_the_ci_job_runs_more_than_one_shard() -> None:
    assert _ci_shard_count() > 1


def test_the_universe_covers_recorded_declared_and_unknown_tests() -> None:
    assert TIMINGS and DECLARED
    assert set(TIMINGS) - DECLARED, "the timing file should carry parametrised ids the sources do not spell out"
    assert UNIVERSE and not (UNKNOWN & set(TIMINGS))


@pytest.mark.parametrize("total", COUNTS)
def test_every_test_is_in_exactly_one_shard(total: int) -> None:
    items = [SimpleNamespace(nodeid=i) for i in UNIVERSE]
    seen: list[str] = []
    for index in range(1, total + 1):
        kept, dropped = sharding.select(items, index, total, TIMINGS)
        assert len(kept) + len(dropped) == len(items)
        seen.extend(item.nodeid for item in kept)
    assert sorted(seen) == UNIVERSE
    assert len(seen) == len(set(seen))


@pytest.mark.parametrize("total", COUNTS)
def test_unrecorded_tests_are_assigned_to_a_valid_shard(total: int) -> None:
    placed = sharding.assign(UNKNOWN, total, TIMINGS)
    assert set(placed) == UNKNOWN
    assert all(1 <= shard <= total for shard in placed.values())


def test_an_empty_or_missing_timing_file_still_assigns_every_test(tmp_path: Path) -> None:
    assert sharding.load_timings(tmp_path / "absent.json") == {}
    placed = sharding.assign(UNIVERSE, 4, {})
    assert set(placed) == set(UNIVERSE) and set(placed.values()) == {1, 2, 3, 4}


def test_the_split_is_deterministic_and_ignores_collection_order() -> None:
    first = sharding.assign(UNIVERSE, 4, TIMINGS)
    assert sharding.assign(reversed(UNIVERSE), 4, TIMINGS) == first
    assert sharding.assign(UNIVERSE, 4, TIMINGS) == first


def test_the_shards_are_balanced_by_recorded_duration() -> None:
    total = _ci_shard_count()
    placed = sharding.assign(TIMINGS, total, TIMINGS)
    load = [sum(TIMINGS[i] for i, s in placed.items() if s == n) for n in range(1, total + 1)]
    # the longest-processing-time greedy keeps the heaviest shard within one longest test of the mean
    assert max(load) - sum(load) / total <= max(TIMINGS.values())
    assert min(load) > 0.8 * max(load)


def test_each_ci_shard_stays_well_under_twenty_minutes_at_the_slowest_recorded_run() -> None:
    # the recorded weights are the mean of runs from 910 s to 2089 s; a runner stalled to 3x that still fits
    total = _ci_shard_count()
    placed = sharding.assign(TIMINGS, total, TIMINGS)
    heaviest = max(sum(TIMINGS[i] for i, s in placed.items() if s == n) for n in range(1, total + 1))
    assert heaviest * 3 < 20 * 60


def test_the_node_variables_are_validated() -> None:
    assert sharding.shard_from_env({}) is None
    assert sharding.shard_from_env({"CI_NODE_INDEX": "2", "CI_NODE_TOTAL": "4"}) == (2, 4)
    for bad in ({"CI_NODE_INDEX": "5", "CI_NODE_TOTAL": "4"}, {"CI_NODE_INDEX": "0", "CI_NODE_TOTAL": "4"}):
        with pytest.raises(sharding.ShardError):
            sharding.shard_from_env(bad)
    with pytest.raises(sharding.ShardError):
        sharding.shard_from_env({"CI_NODE_TOTAL": "4"})


def test_the_e2e_conftest_applies_the_shard_selection() -> None:
    source = (E2E / "conftest.py").read_text(encoding="utf-8")
    assert "def pytest_collection_modifyitems" in source and "sharding.select(items" in source
    assert "e2e-report-${CI_NODE_INDEX}.xml" in (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
