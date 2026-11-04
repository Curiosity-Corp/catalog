#!/usr/bin/env python3
"""Fail the pipeline on known vulnerabilities in the pinned CI toolchain.

Runs ``pip-audit`` over ``ci/requirements.txt`` (exact pins, no resolution)
and applies ``ci/vulnerability-exceptions.yml``:

* a finding with no unexpired exception fails the job (exit 1);
* an exception that has expired fails the job, so it must be renewed in a
  reviewed merge request or the dependency bumped (exit 1);
* an exception that matches no current finding is reported but does not fail;
* if the vulnerability database cannot be reached the job exits 3, which the
  pipeline marks as an allowed warning rather than a pass or a failure.

``--from-json FILE`` reads a saved ``pip-audit --format json`` report instead of
calling pip-audit, which is how the tests exercise every branch offline.
Severity is not available from the advisory databases pip-audit reads, so every
finding is treated as blocking; see docs/supply-chain.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import yaml

MAX_EXCEPTION_DAYS = 90
NETWORK_ERROR = 3


def load_exceptions(path: Path, today: dt.date) -> tuple[dict[tuple[str, str], dict], list[str]]:
    problems: list[str] = []
    active: dict[tuple[str, str], dict] = {}
    if not path.is_file():
        return active, problems
    document = yaml.safe_load(path.read_text()) or {}
    for item in document.get("exceptions") or []:
        missing = [key for key in ("id", "package", "reason", "expires", "approved_by") if not str(item.get(key, "")).strip()]
        if missing:
            problems.append(f"exception {item.get('id', '?')} is missing {', '.join(missing)}")
            continue
        try:
            expires = dt.date.fromisoformat(str(item["expires"]))
        except ValueError:
            problems.append(f"exception {item['id']} has an invalid expires date")
            continue
        if expires < today:
            problems.append(f"exception {item['id']} for {item['package']} expired on {expires}")
            continue
        if (expires - today).days > MAX_EXCEPTION_DAYS:
            problems.append(f"exception {item['id']} expires more than {MAX_EXCEPTION_DAYS} days from now")
            continue
        active[(item["package"].lower().replace("_", "-"), item["id"])] = item
    return active, problems


def findings(report: dict) -> list[tuple[str, str, str, list[str]]]:
    rows = []
    for dependency in report.get("dependencies", []):
        for vulnerability in dependency.get("vulns", []):
            rows.append(
                (
                    dependency["name"].lower().replace("_", "-"),
                    dependency["version"],
                    vulnerability["id"],
                    vulnerability.get("fix_versions", []),
                )
            )
    return rows


def run_pip_audit(lock: Path) -> dict | None:
    result = subprocess.run(
        [sys.executable, "-m", "pip_audit", "--requirement", str(lock), "--no-deps", "--disable-pip", "--format", "json", "--progress-spinner", "off"],
        capture_output=True,
        text=True,
        check=False,
    )
    if "No module named pip_audit" in result.stderr:
        raise SystemExit("pip-audit is not installed; install ci/requirements.txt first")
    try:
        return json.loads(result.stdout)
    except ValueError:
        sys.stderr.write(result.stderr[-800:])
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lock", type=Path, default=Path("ci/requirements.txt"))
    parser.add_argument("--exceptions", type=Path, default=Path("ci/vulnerability-exceptions.yml"))
    parser.add_argument("--from-json", type=Path)
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args()

    report = json.loads(args.from_json.read_text()) if args.from_json else run_pip_audit(args.lock)
    if report is None:
        print("WARN  pip-audit produced no report (vulnerability database unreachable?); nothing was checked")
        return NETWORK_ERROR
    active, problems = load_exceptions(args.exceptions, args.today)
    used = set()
    failed = list(problems)
    for package, version, advisory, fixes in findings(report):
        if (package, advisory) in active:
            used.add((package, advisory))
            print(f"excepted  {package} {version} {advisory} (until {active[(package, advisory)]['expires']})")
        else:
            hint = f"; fixed in {', '.join(fixes)}" if fixes else "; no fixed version published"
            failed.append(f"{package} {version} has {advisory}{hint}")
    for key in sorted(set(active) - used):
        print(f"unused    exception for {key[1]} on {key[0]} matches no current finding; remove it")
    for message in failed:
        print(f"FAIL  {message}")
    if not failed:
        print(f"ok    {len(report.get('dependencies', []))} pinned packages, no unexcepted vulnerabilities")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
