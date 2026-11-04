#!/usr/bin/env python3
"""Check docs/dependency-register.yml against what the role actually installs.

The register records, for every external tool and every external source the
role uses: owner, source, license, update channel, verification method and
fallback, plus a criticality, a status and a review date. This script keeps it
honest:

* every application contract in the role defaults has a ``tools`` entry, and
  every ``tools`` entry still has a contract (no stale rows);
* every host the role downloads from or adds as a package repository, as found
  by ``scripts/external_sources.py``, has a ``sources`` entry;
* every entry has all required fields after merging the ``installer_types``
  defaults, and the installer and verification method agree with the contract;
* ``abandoned`` entries still in use, ``security-critical`` entries without a
  fallback, and apt repositories without signature verification are errors;
  ``watch`` and ``at-risk`` entries are listed as alerts;
* ``--max-age-days N`` turns a review date older than N days into an error (the
  scheduled monthly job); ``--online`` asks GitHub whether an upstream
  repository was archived (allowed to fail on network errors, which are reported,
  not hidden).

Exit status is 1 when there is any error. Alerts alone do not fail.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import external_sources  # noqa: E402

REQUIRED = ("owner", "owner_type", "source", "license", "update_channel", "verification", "fallback", "criticality", "status", "last_reviewed")
OWNER_TYPES = {"individual", "community", "company", "foundation"}
CRITICALITY = {"standard", "security-critical"}
STATUS = {"active", "watch", "at-risk", "abandoned"}
GITHUB_REPO = re.compile(r"^https://github\.com/([^/]+/[^/]+?)/?$")


def fetch_github_archived(repository: str) -> bool | None:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "curiosity-dependency-register"},
    )
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return bool(json.load(response).get("archived"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def effective(entry: dict, installer_types: dict, installer: str | None) -> dict:
    merged = dict(installer_types.get(installer or entry.get("installer", ""), {}))
    merged.update(entry)
    return merged


def check(
    register: dict,
    scan: dict,
    today: dt.date,
    max_age_days: int | None = None,
    archived_lookup: Callable[[str], bool | None] | None = None,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    alerts: list[str] = []
    if register.get("schema") != "curiosity.workstation.dependency-register/v1":
        errors.append("schema must be curiosity.workstation.dependency-register/v1")
    installer_types = register.get("installer_types") or {}
    tools = register.get("tools") or {}
    sources = register.get("sources") or {}

    contracts = {}
    for item in scan["github_releases"]:
        contracts[item["name"]] = {"installer": item["installer"], "verification": item["verification"], "repository": item["repository"]}
    for item in scan["other_contracts"]:
        contracts[item["name"]] = {"installer": item["installer"], "verification": item["verification"]}

    for name in sorted(set(contracts) - set(tools)):
        errors.append(f"tools: {name} is installed by the role but has no register entry")
    for name in sorted(set(tools) - set(contracts)):
        errors.append(f"tools: {name} has a register entry but the role no longer installs it")
    hosts = {item["host"]: item for item in scan["hosts"]}
    for host in sorted(set(hosts) - set(sources)):
        errors.append(f"sources: {host} is used by the role but has no register entry")
    for host in sorted(set(sources) - set(hosts)):
        errors.append(f"sources: {host} has a register entry but the role no longer uses it")

    def review(kind: str, name: str, record: dict) -> None:
        label = f"{kind}: {name}"
        for field in REQUIRED:
            if not str(record.get(field, "")).strip():
                errors.append(f"{label} is missing {field}")
        if record.get("owner_type") and record["owner_type"] not in OWNER_TYPES:
            errors.append(f"{label} owner_type {record['owner_type']!r} is not one of {sorted(OWNER_TYPES)}")
        if record.get("criticality") and record["criticality"] not in CRITICALITY:
            errors.append(f"{label} criticality {record['criticality']!r} is not one of {sorted(CRITICALITY)}")
        status = record.get("status")
        if status and status not in STATUS:
            errors.append(f"{label} status {status!r} is not one of {sorted(STATUS)}")
        if status == "abandoned":
            errors.append(f"{label} is abandoned upstream but still installed: replace it or drop it (see its fallback)")
        elif status in {"watch", "at-risk"}:
            alerts.append(f"{label} is marked {status}: {record.get('note', 'no note')}")
        if record.get("criticality") == "security-critical" and str(record.get("verification", "")) in {"", "none"}:
            errors.append(f"{label} is security-critical and has no verification method")
        try:
            reviewed = dt.date.fromisoformat(str(record.get("last_reviewed")))
        except ValueError:
            errors.append(f"{label} last_reviewed is not an ISO date")
        else:
            age = (today - reviewed).days
            if age < 0:
                errors.append(f"{label} last_reviewed is in the future")
            elif max_age_days is not None and age > max_age_days:
                errors.append(f"{label} was last reviewed {age} days ago (limit {max_age_days})")

    for name, entry in sorted(tools.items()):
        contract = contracts.get(name)
        if contract is None:
            continue
        record = effective(entry, installer_types, contract["installer"])
        review("tools", name, record)
        if entry.get("installer", contract["installer"]) != contract["installer"]:
            errors.append(f"tools: {name} lists installer {entry['installer']!r} but the role uses {contract['installer']!r}")
        if record.get("verification") != contract["verification"]:
            errors.append(f"tools: {name} lists verification {record.get('verification')!r} but the role uses {contract['verification']!r}")
        if contract["installer"] not in installer_types:
            errors.append(f"tools: {name} uses installer {contract['installer']!r} that has no installer_types defaults")
        if "repository" in contract and not str(record.get("source", "")).endswith(contract["repository"]):
            errors.append(f"tools: {name} source {record.get('source')!r} does not match repository {contract['repository']}")

    for host, entry in sorted(sources.items()):
        if host not in hosts:
            continue
        record = dict(entry)
        review("sources", host, record)
        kinds = set(hosts[host]["kinds"])
        if "apt-source" in kinds and "apt-signature" not in str(record.get("verification", "")):
            errors.append(f"sources: {host} is an apt repository but its verification is not apt-signature")
        if hosts[host]["pipes_to_shell"]:
            errors.append(f"sources: {host} is piped into a shell; use a pinned, verified download instead")

    if archived_lookup:
        looked = set()
        for name, entry in sorted(tools.items()):
            match = GITHUB_REPO.match(str(entry.get("source", "")))
            if not match or match.group(1) in looked:
                continue
            looked.add(match.group(1))
            archived = archived_lookup(match.group(1))
            if archived is None:
                alerts.append(f"tools: could not ask GitHub about {match.group(1)} (network or rate limit); not checked")
            elif archived:
                status = effective(entry, installer_types, contracts.get(name, {}).get("installer")).get("status")
                if status in {"at-risk", "abandoned"}:
                    alerts.append(f"tools: {name} upstream {match.group(1)} is archived (already recorded as {status})")
                else:
                    errors.append(f"tools: {name} upstream {match.group(1)} is archived but the register says {status}")
    return errors, alerts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--register", type=Path, default=None)
    parser.add_argument("--max-age-days", type=int)
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args()
    register_path = args.register or args.root / "docs/dependency-register.yml"
    register = yaml.safe_load(register_path.read_text())
    errors, alerts = check(
        register,
        external_sources.scan_tree(args.root),
        args.today,
        args.max_age_days,
        fetch_github_archived if args.online else None,
    )
    for alert in alerts:
        print(f"ALERT {alert}")
    for error in errors:
        print(f"FAIL  {error}")
    if not errors:
        print(f"ok    {len(register.get('tools') or {})} tools and {len(register.get('sources') or {})} sources recorded")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
