#!/usr/bin/env python3
"""Find the external software sources the role reaches out to at run time.

The collection ships no binaries: it installs vendor packages, GitHub release
assets and a few upstream installers when it runs. This module reads the role's
defaults and task files (from a source tree or from a built artifact) and
returns what it finds, so the SBOM and the dependency register are derived from
the code instead of a hand-kept list.

It is a static scan, not an execution trace: URLs that are built from variables
are reported only by their literal host, and tasks that are skipped on a given
host still count. That over-reports rather than under-reports, which is the
safe direction for an inventory.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable, Iterator

import yaml

APT_MODULES = {"apt_repository", "deb822_repository", "apt_key"}
DOWNLOAD_MODULES = {"get_url", "uri", "unarchive"}
COMMAND_MODULES = {"command", "shell", "raw"}
PACKAGE_MANAGER_MODULES = {"pip", "pipx", "npm", "gem", "cargo"}
CLONE_MODULES = {"git"}
URL = re.compile(r"https?://([A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9])(?::\d+)?(/[^\s\"'`)}\]|;]*)?")
INSTALLER_PATH = re.compile(r"(\.sh|/install|/installer|install\.[a-z]+)$", re.I)
PIPE_TO_SHELL = re.compile(r"\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b")


def module_name(key: str) -> str:
    return key.rsplit(".", 1)[-1]


def iter_tasks(node) -> Iterator[dict]:
    """Yield every task dictionary, descending into block/rescue/always."""
    if isinstance(node, list):
        for item in node:
            yield from iter_tasks(item)
    elif isinstance(node, dict):
        if any(key in node for key in ("block", "rescue", "always")):
            for key in ("block", "rescue", "always"):
                yield from iter_tasks(node.get(key) or [])
        yield node


def strings(node) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from strings(value)


def hosts_in(text: str) -> list[tuple[str, str]]:
    found = []
    for match in URL.finditer(text):
        host, path = match.group(1).lower(), match.group(2) or ""
        if "{" in host or host in {"example.org", "example.com", "localhost"} or host.endswith(".example.edu"):
            continue
        found.append((host, path))
    return found


def scan_tasks(name: str, text: str) -> list[dict]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    results = []
    for task in iter_tasks(document):
        for key, args in task.items():
            module = module_name(str(key))
            if module in APT_MODULES:
                kind = "apt-source"
            elif module in DOWNLOAD_MODULES:
                kind = "download"
            elif module in COMMAND_MODULES:
                kind = "command-fetch"
            elif module in PACKAGE_MANAGER_MODULES:
                kind = "package-manager"
            elif module in CLONE_MODULES:
                kind = "source-clone"
            else:
                continue
            body = "\n".join(strings(args))
            piped = bool(PIPE_TO_SHELL.search(body))
            for host, path in hosts_in(body):
                row_kind = kind
                if kind in {"download", "command-fetch"} and (INSTALLER_PATH.search(path) or piped):
                    row_kind = "installer-script"
                results.append(
                    {
                        "host": host,
                        "kind": row_kind,
                        "file": name,
                        "task": str(task.get("name", "")),
                        "pipes_to_shell": piped,
                    }
                )
            if kind == "package-manager" and isinstance(args, dict) and args.get("name"):
                results.append(
                    {
                        "host": {"pip": "pypi.org", "pipx": "pypi.org", "npm": "registry.npmjs.org"}.get(module, module),
                        "kind": "package-manager",
                        "file": name,
                        "task": str(task.get("name", "")),
                        "pipes_to_shell": False,
                    }
                )
    return results


def scan_defaults(text: str) -> dict:
    document = yaml.safe_load(text) or {}
    repositories = document.get("latest_github_repositories") or {}
    contracts = {**(document.get("latest_release_contracts") or {}), **(document.get("workstation_application_contracts") or {})}
    releases = []
    for tool, repository in sorted(repositories.items()):
        contract = contracts.get(tool, {})
        releases.append(
            {
                "name": tool,
                "repository": repository,
                "installer": contract.get("installer", ""),
                "verification": contract.get("verification", ""),
            }
        )
    other = []
    for tool, contract in sorted(contracts.items()):
        if tool not in repositories:
            other.append(
                {
                    "name": tool,
                    "installer": contract.get("installer", ""),
                    "verification": contract.get("verification", ""),
                }
            )
    return {"github_releases": releases, "other_contracts": other}


def scan_role_files(files: Iterable[tuple[str, str]]) -> dict:
    """Scan ``(path, text)`` pairs from roles/*/defaults and roles/*/tasks."""
    releases: list[dict] = []
    other: list[dict] = []
    fetches: list[dict] = []
    for path, text in sorted(files):
        if "/defaults/" in path and path.endswith(("main.yml", "main.yaml")):
            parsed = scan_defaults(text)
            releases.extend(parsed["github_releases"])
            other.extend(parsed["other_contracts"])
        elif "/tasks/" in path and path.endswith((".yml", ".yaml")):
            fetches.extend(scan_tasks(path, text))
    sources: dict[str, dict] = {}
    for row in fetches:
        entry = sources.setdefault(row["host"], {"host": row["host"], "kinds": set(), "files": set(), "pipes_to_shell": False})
        entry["kinds"].add(row["kind"])
        entry["files"].add(row["file"])
        entry["pipes_to_shell"] = entry["pipes_to_shell"] or row["pipes_to_shell"]
    hosts = [
        {
            "host": entry["host"],
            "kinds": sorted(entry["kinds"]),
            "files": sorted(entry["files"]),
            "pipes_to_shell": entry["pipes_to_shell"],
        }
        for entry in sorted(sources.values(), key=lambda item: item["host"])
    ]
    return {"github_releases": releases, "other_contracts": other, "hosts": hosts}


def scan_tree(root: Path) -> dict:
    files = []
    for role in sorted((root / "roles").glob("*")):
        for path in sorted(role.glob("defaults/main.y*ml")) + sorted(role.glob("tasks/*.y*ml")):
            files.append((path.relative_to(root).as_posix(), path.read_text(errors="replace")))
    return scan_role_files(files)


if __name__ == "__main__":
    import json

    print(json.dumps(scan_tree(Path(sys.argv[1] if len(sys.argv) > 1 else ".")), indent=2))
