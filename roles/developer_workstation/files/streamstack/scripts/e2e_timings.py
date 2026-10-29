#!/usr/bin/env python3
"""Rebuild tests/e2e/timings.json, the per-test durations the e2e shards balance on (issue #172).

Inputs are the e2e results of successful pipelines, either the JUnit XML each shard writes (``e2e-report-N.xml``) or a
pipeline's GitLab test report (``glab api projects/2122/pipelines/<id>/test_report``, whose ``e2e`` suites are read).
Give the files of one run together: a run's tests are summed across its shards. Several runs are averaged after each
is scaled to its share of the run's total, so one slow runner (infrastructure#639) does not skew the weights.

    python3 scripts/e2e_timings.py run1-*.xml ::: run2.json ::: run3-*.xml

Runs are separated by ``:::``. The weights are rounded to milliseconds. A test missing from the file is still run:
tests/e2e/sharding.py gives it the median weight.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "tests" / "e2e" / "timings.json"


def node_id(classname: str, name: str) -> str:
    """``tests.e2e.test_boot.TestToken`` + ``test_x[chromium]`` -> the pytest node id."""
    parts = classname.split(".")
    module = next((i for i, p in enumerate(parts) if p.startswith("test_")), None)
    if module is None:
        raise ValueError(f"no test module in classname {classname!r}")
    path = "/".join(parts[: module + 1]) + ".py"
    return "::".join([path, *parts[module + 1 :], name])


def cases(path: Path) -> Iterator[tuple[str, float]]:
    if path.suffix == ".json":
        report = json.loads(path.read_text(encoding="utf-8"))
        for suite in report.get("test_suites", []):
            if suite.get("name", "").split()[0] == "e2e":
                for case in suite["test_cases"]:
                    yield node_id(case["classname"], case["name"]), float(case["execution_time"])
        return
    for case in ET.parse(path).getroot().iter("testcase"):  # noqa: S314
        yield node_id(case.get("classname", ""), case.get("name", "")), float(case.get("time", 0))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", help="report files; separate the runs with :::")
    parser.add_argument("-o", "--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    runs: list[list[Path]] = [[]]
    for f in args.files:
        if f == ":::":
            runs.append([])
        else:
            runs[-1].append(Path(f))
    totals: dict[str, float] = defaultdict(float)
    sums = []
    ran = [r for r in runs if r]
    per_run = []
    for run in ran:
        t: dict[str, float] = defaultdict(float)
        for path in run:
            for nid, seconds in cases(path):
                t[nid] += seconds
        if not t:
            print(f"no e2e test cases in {[str(p) for p in run]}", file=sys.stderr)
            return 1
        per_run.append(t)
        sums.append(sum(t.values()))
    mean_total = sum(sums) / len(sums)
    for t, total in zip(per_run, sums, strict=True):
        for nid, seconds in t.items():
            totals[nid] += seconds / total * mean_total / len(per_run)
    doc = {
        "about": "Seconds per e2e node id: weights for tests/e2e/sharding.py, rebuilt by scripts/e2e_timings.py.",
        "runs": len(ran),
        "seconds": {k: round(v, 3) for k, v in sorted(totals.items())},
    }
    args.out.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"{len(totals)} tests, {mean_total:.0f} s of test time over {len(ran)} runs -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
