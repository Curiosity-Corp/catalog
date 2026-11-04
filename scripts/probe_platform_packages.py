#!/usr/bin/env python3
"""Record which of the role's apt packages each distribution's archive provides.

The role installs its main package set in one `apt` task
(`roles/developer_workstation/tasks/packages.yml`). One name missing from the
archive of a distribution fails that whole task, so the first question for a new
platform is which names it lacks. This script answers it without a disposable
machine: it starts a container per release, runs `apt-get update` and
`apt-cache policy`, and writes a dated JSON file.

    python scripts/probe_platform_packages.py list
    python scripts/probe_platform_packages.py probe ubuntu:22.04 debian:12 --output docs/evidence/platform-packages-YYYY-MM-DD.json
    python scripts/probe_platform_packages.py check docs/evidence/platform-packages-YYYY-MM-DD.json

`probe` needs podman or docker and network access to the distribution mirrors. It
only reads package metadata; nothing from the role runs. A name missing from a
plain distribution archive may still be provided by a repository the role adds
(Node.js, Docker, vendor repositories). The result therefore says "not in the
distribution archive", and `docs/platform-differences.md` records which of those
the role's repository tasks supply.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PACKAGES_TASK = ROOT / "roles/developer_workstation/tasks/packages.yml"
APT_MODULES = ("ansible.builtin.apt", "apt")
INSTALL_TASK = "Install all APT packages"


def role_packages(path: Path = PACKAGES_TASK) -> list[str]:
    """Static package names from the main install task (templated names are skipped)."""
    tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
    for task in tasks:
        if task.get("name", "").startswith(INSTALL_TASK):
            for module in APT_MODULES:
                if module in task:
                    names = task[module]["name"]
                    return [name for name in names if isinstance(name, str) and "{{" not in name]
    raise SystemExit(f"{path}: install task not found")


PROBE_SCRIPT = r"""
set -u
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1 || { echo "APT_UPDATE_FAILED"; exit 0; }
. /etc/os-release
echo "OS ${ID} ${VERSION_ID:-unknown} ${VERSION_CODENAME:-unknown} $(dpkg --print-architecture)"
for p in "$@"; do
  c=$(apt-cache policy "$p" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')
  echo "PKG $p ${c:-MISSING}"
done
"""


def runtime() -> str:
    for name in ("podman", "docker"):
        if shutil.which(name):
            return name
    raise SystemExit("podman or docker is required for `probe`")


def probe_image(image: str, packages: list[str], platform: str = "") -> dict:
    command = [runtime(), "run", "--rm"]
    if platform:
        command += ["--platform", platform]
    command += [image, "bash", "-c", PROBE_SCRIPT, "probe", *packages]
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=1800)
    return parse_probe_output(image, result.stdout, packages)


def parse_probe_output(image: str, output: str, packages: list[str]) -> dict:
    lines = output.splitlines()
    if "APT_UPDATE_FAILED" in lines:
        raise SystemExit(f"{image}: apt-get update failed; no result recorded")
    meta = next((line.split()[1:] for line in lines if line.startswith("OS ")), None)
    if not meta:
        raise SystemExit(f"{image}: no os-release line in the probe output")
    found = {}
    for line in lines:
        if line.startswith("PKG "):
            _, name, candidate = line.split(maxsplit=2)
            found[name] = None if candidate == "MISSING" or candidate == "(none)" else candidate
    unprobed = [name for name in packages if name not in found]
    if unprobed:
        raise SystemExit(f"{image}: probe output lacks {', '.join(unprobed[:5])}")
    return {
        "image": image,
        "id": meta[0],
        "version": meta[1],
        "codename": meta[2],
        "architecture": meta[3],
        "missing": sorted(name for name, candidate in found.items() if candidate is None),
        "available": len(found) - sum(candidate is None for candidate in found.values()),
        "candidates": {name: candidate for name, candidate in sorted(found.items()) if candidate},
    }


def check_file(path: Path, packages: list[str]) -> list[str]:
    """Problems with a recorded result: stale package list, missing date or unknown shape."""
    data = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    try:
        dt.date.fromisoformat(data.get("date", ""))
    except ValueError:
        problems.append("date is missing or not YYYY-MM-DD")
    if sorted(data.get("packages", [])) != sorted(packages):
        added = sorted(set(packages) - set(data.get("packages", [])))
        removed = sorted(set(data.get("packages", [])) - set(packages))
        problems.append(f"package list differs from the role (new: {added[:5]}, gone: {removed[:5]}); probe again")
    if not data.get("results"):
        problems.append("no results")
    for result in data.get("results", []):
        for name in result.get("missing", []):
            if name not in packages:
                problems.append(f"{result.get('image')}: missing package {name} is not in the role list")
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    probe = sub.add_parser("probe")
    probe.add_argument("images", nargs="+")
    probe.add_argument("--platform", default="", help="for example linux/arm64 (needs qemu binfmt)")
    probe.add_argument("--output", type=Path, required=True)
    check = sub.add_parser("check")
    check.add_argument("file", type=Path)
    args = parser.parse_args(argv)

    packages = role_packages()
    if args.command == "list":
        print("\n".join(packages))
        return 0
    if args.command == "check":
        problems = check_file(args.file, packages)
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    results = [probe_image(image, packages, args.platform) for image in args.images]
    document = {
        "date": dt.date.today().isoformat(),
        "source": "apt-cache policy in a container after apt-get update; distribution archives only",
        "packages": packages,
        "results": results,
    }
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for result in results:
        print(f"{result['image']}: {result['available']} available, {len(result['missing'])} not in the archive")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
