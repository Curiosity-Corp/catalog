"""Deterministic, duration-balanced sharding of the e2e suite (issue #172).

GitLab's ``parallel: N`` starts N jobs that each see ``CI_NODE_INDEX`` (1..N) and ``CI_NODE_TOTAL`` (N). Every shard
collects the whole suite and keeps only its own part, so the split must be a pure function of the collected node ids and
the checked-in ``timings.json``: the same inputs give the same partition on every runner, and every test lands in
exactly one shard. A test that is not in the timing file (new, renamed, parametrised differently) is not dropped: it
gets the median known duration as its weight and is placed like any other.

The placement is the longest-processing-time greedy: heaviest test first, always onto the currently lightest shard,
ties broken by lowest shard index and then node id. This module imports nothing outside the standard library, so the
unit suite (which has no Playwright) can prove the partition (tests/test_e2e_sharding.py).
"""

from __future__ import annotations

import json
import os
import statistics
from collections.abc import Iterable, Mapping
from pathlib import Path

TIMINGS = Path(__file__).with_name("timings.json")
# Weight for a test with no recorded duration when the timing file is empty too: seconds, a typical e2e test.
FALLBACK_SECONDS = 4.0


class ShardError(ValueError):
    """The shard variables are not a valid ``index of total``."""


def load_timings(path: Path = TIMINGS) -> dict[str, float]:
    """The recorded duration, in seconds, of each e2e node id. A missing file is an empty record, never an error: every
    test is then unknown and weighs the same, so the split is still complete."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ShardError(f"cannot read the e2e timing file {path}: {exc}") from exc
    seconds = raw.get("seconds") if isinstance(raw, dict) else None
    if not isinstance(seconds, dict):
        raise ShardError(f"{path} must be an object with a 'seconds' object of node id -> duration")
    return {str(k): float(v) for k, v in seconds.items() if isinstance(v, int | float) and v >= 0}


def shard_from_env(env: Mapping[str, str] | None = None) -> tuple[int, int] | None:
    """``(index, total)`` from ``CI_NODE_INDEX`` / ``CI_NODE_TOTAL`` (1-based), or None when the run is not sharded."""
    env = os.environ if env is None else env
    total_raw, index_raw = env.get("CI_NODE_TOTAL", "").strip(), env.get("CI_NODE_INDEX", "").strip()
    if not total_raw and not index_raw:
        return None
    try:
        total, index = int(total_raw), int(index_raw)
    except ValueError as exc:
        raise ShardError(f"CI_NODE_INDEX={index_raw!r} / CI_NODE_TOTAL={total_raw!r} are not both integers") from exc
    if total < 1 or not 1 <= index <= total:
        raise ShardError(f"CI_NODE_INDEX={index} is not within 1..CI_NODE_TOTAL={total}")
    return index, total


def assign(node_ids: Iterable[str], total: int, timings: Mapping[str, float] | None = None) -> dict[str, int]:
    """Map every node id to a shard in ``1..total``, balancing the summed recorded duration."""
    if total < 1:
        raise ShardError(f"cannot split into {total} shards")
    timings = load_timings() if timings is None else timings
    ids = sorted(set(node_ids))
    known = [timings[i] for i in ids if i in timings]
    unknown_weight = statistics.median(known) if known else FALLBACK_SECONDS
    weight = {i: timings.get(i, unknown_weight) for i in ids}
    load = [0.0] * total
    placed: dict[str, int] = {}
    for node_id in sorted(ids, key=lambda i: (-weight[i], i)):
        lightest = min(range(total), key=lambda s: (load[s], s))
        load[lightest] += weight[node_id]
        placed[node_id] = lightest + 1
    return placed


def select(items: list, index: int, total: int, timings: Mapping[str, float] | None = None) -> tuple[list, list]:
    """Split collected pytest items into ``(kept, dropped)`` for shard ``index`` of ``total``, keeping the order."""
    placed = assign((item.nodeid for item in items), total, timings)
    kept = [item for item in items if placed[item.nodeid] == index]
    dropped = [item for item in items if placed[item.nodeid] != index]
    return kept, dropped
