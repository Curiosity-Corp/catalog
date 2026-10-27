#!/usr/bin/env python3
"""Licence check and CycloneDX SBOM for the DeepFilterNet LADSPA build (`images/dfn-ladspa/Dockerfile`).

Reads `cargo metadata` and Cargo.lock of the pinned upstream source, takes the crates the plugin links (the normal
dependencies reachable from `deep-filter-ladspa` on x86_64 Linux), and

* fails (exit 1) when any crate's licence expression cannot be satisfied with the ALLOWED licences below, or states
  no licence at all, so a copyleft or unknown licence never ships in the plugin;
* writes `LICENSES.json` (every crate, its licence, its verdict) and a CycloneDX 1.5 SBOM
  (`libdeep_filter_ladspa.cdx.json`) with the crates, their crates.io checksums, the embedded DeepFilterNet3 model
  and the upstream source archive with its sha256.

Standard library only (the build image has python3 and nothing else to install); no network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
import uuid
from pathlib import Path
from typing import Any

# Permissive licences (SPDX ids). Weak or strong copyleft (MPL, LGPL, GPL, AGPL) is deliberately absent: a crate
# under one of them fails the build and a person decides, instead of it shipping unnoticed.
ALLOWED = frozenset(
    {
        "MIT",
        "Apache-2.0",
        "Apache-2.0 WITH LLVM-exception",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "Zlib",
        "0BSD",
        "Unlicense",
        "CC0-1.0",
        "BSL-1.0",
        "Unicode-DFS-2016",
        "Unicode-3.0",
    }
)

_TOKEN = re.compile(r"\(|\)|[^\s()]+")


class LicenceError(ValueError):
    """A licence expression that cannot be parsed."""


def satisfiable(expression: str, allowed: frozenset[str] = ALLOWED) -> bool:
    """True when the SPDX expression can be satisfied using only `allowed` licences.

    `A OR B` needs one side, `A AND B` needs both, `A WITH X` is its own id (`Apache-2.0 WITH LLVM-exception`).
    Cargo's older `A/B` means OR.
    """
    tokens = _TOKEN.findall(expression.replace("/", " OR "))
    if not tokens:
        raise LicenceError("empty licence expression")
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def is_word(word: str) -> bool:
        token = peek()
        return token is not None and token.upper() == word

    def take() -> str:
        nonlocal pos
        if pos >= len(tokens):
            raise LicenceError(f"unexpected end of {expression!r}")
        pos += 1
        return tokens[pos - 1]

    def atom() -> bool:
        word = take()
        if word == "(":
            value = either()
            if take() != ")":
                raise LicenceError(f"unbalanced parentheses in {expression!r}")
            return value
        name = word
        if is_word("WITH"):
            take()
            name = f"{word} WITH {take()}"
        return name.removesuffix("+") in allowed

    def both() -> bool:
        value = atom()
        while is_word("AND"):
            take()
            value = atom() and value
        return value

    def either() -> bool:
        value = both()
        while is_word("OR"):
            take()
            value = both() or value
        return value

    result = either()
    if pos != len(tokens):
        raise LicenceError(f"cannot parse {expression!r} at {tokens[pos]!r}")
    return result


def linked_packages(metadata: dict[str, Any], root_name: str) -> list[dict[str, Any]]:
    """The packages reachable from `root_name` through normal (non dev, non build) dependencies, root first."""
    by_id = {p["id"]: p for p in metadata["packages"]}
    nodes = {n["id"]: n for n in metadata["resolve"]["nodes"]}
    roots = [p["id"] for p in metadata["packages"] if p["name"] == root_name]
    if len(roots) != 1:
        raise ValueError(f"expected one package named {root_name!r}, found {len(roots)}")
    seen: dict[str, None] = {}
    stack = [roots[0]]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen[current] = None
        for dep in nodes[current].get("deps", []):
            if any(kind.get("kind") is None for kind in dep.get("dep_kinds", [])):
                stack.append(dep["pkg"])
    return [by_id[i] for i in seen]


def lock_checksums(lock_text: str) -> dict[tuple[str, str], str]:
    packages = tomllib.loads(lock_text).get("package", [])
    return {(p["name"], p["version"]): p["checksum"] for p in packages if "checksum" in p}


def audit(packages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """One row per crate, and the problems (a crate whose licence is unknown or not allowed)."""
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    for pkg in sorted(packages, key=lambda p: (p["name"], p["version"])):
        expression = (pkg.get("license") or "").strip()
        verdict = "no licence stated"
        if expression:
            try:
                verdict = "allowed" if satisfiable(expression) else "not allowed"
            except LicenceError as exc:
                verdict = f"unparsable: {exc}"
        if verdict != "allowed":
            problems.append(f"{pkg['name']} {pkg['version']}: {expression or '(none)'} ({verdict})")
        rows.append({"name": pkg["name"], "version": pkg["version"], "license": expression or None, "verdict": verdict})
    return rows, problems


def sbom(
    packages: list[dict[str, Any]],
    checksums: dict[tuple[str, str], str],
    *,
    root_name: str,
    so_sha256: str,
    version: str,
    source_url: str,
    source_sha256: str,
    serial: str | None = None,
) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    for pkg in sorted(packages, key=lambda p: (p["name"], p["version"])):
        if pkg["name"] == root_name:
            continue
        component: dict[str, Any] = {
            "type": "library",
            "bom-ref": f"pkg:cargo/{pkg['name']}@{pkg['version']}",
            "name": pkg["name"],
            "version": pkg["version"],
            "purl": f"pkg:cargo/{pkg['name']}@{pkg['version']}",
        }
        if pkg.get("license"):
            component["licenses"] = [{"expression": pkg["license"].replace("/", " OR ")}]
        if (digest := checksums.get((pkg["name"], pkg["version"]))) is not None:
            component["hashes"] = [{"alg": "SHA-256", "content": digest}]
        components.append(component)
    components.append(
        {
            "type": "machine-learning-model",
            "bom-ref": "model:deepfilternet3-ll",
            "name": "DeepFilterNet3 (low latency), embedded in libDF",
            "version": version,
            "licenses": [{"expression": "MIT OR Apache-2.0"}],
            "externalReferences": [{"type": "distribution", "url": source_url}],
        }
    )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial or uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "library",
                "bom-ref": "libdeep_filter_ladspa",
                "name": "libdeep_filter_ladspa",
                "version": version,
                "description": "DeepFilterNet LADSPA plugin built from the pinned upstream release",
                "licenses": [{"expression": "MIT OR Apache-2.0"}],
                "hashes": [{"alg": "SHA-256", "content": so_sha256}],
                "externalReferences": [
                    {"type": "vcs", "url": source_url, "hashes": [{"alg": "SHA-256", "content": source_sha256}]}
                ],
            },
        },
        "components": components,
    }


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--metadata", type=Path, required=True, help="`cargo metadata --format-version 1` output")
    parser.add_argument("--lock", type=Path, required=True, help="Cargo.lock of the source")
    parser.add_argument("--so", type=Path, required=True, help="the built libdeep_filter_ladspa.so")
    parser.add_argument("--out", type=Path, required=True, help="directory for LICENSES.json and the SBOM")
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--root", default="deep-filter-ladspa")
    args = parser.parse_args(argv)

    metadata = json.loads(args.metadata.read_text())
    packages = linked_packages(metadata, args.root)
    rows, problems = audit(packages)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "LICENSES.json").write_text(json.dumps(rows, indent=2) + "\n")
    document = sbom(
        packages,
        lock_checksums(args.lock.read_text()),
        root_name=args.root,
        so_sha256=sha256_file(args.so),
        version=args.version,
        source_url=args.source_url,
        source_sha256=args.source_sha256,
    )
    (args.out / "libdeep_filter_ladspa.cdx.json").write_text(json.dumps(document, indent=2) + "\n")
    print(f"{len(rows)} crates linked into the plugin; SBOM and LICENSES.json written to {args.out}")
    if problems:
        print("licence policy violations:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        return 1
    print("licence check: every crate is under an allowed licence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
