#!/usr/bin/env python3
"""Verify a collection SBOM against the artifact it describes.

Fails (exit 1) when the SBOM is not CycloneDX 1.5 shaped, has duplicate
bom-refs, does not match the artifact's digest or file list, or is missing a
category of component that ``build_collection_sbom.py`` must always emit:
source files, direct collection dependencies with a resolved version, the CI
toolchain, and the runtime sources the role fetches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from collections import Counter
from pathlib import Path

REQUIRED_CATEGORIES = (
    "source-file",
    "collection-dependency",
    "ci-tool",
    "runtime-release-download",
    "runtime-package-source",
)
KNOWN_CATEGORIES = set(REQUIRED_CATEGORIES) | {
    "ci-image",
    "runtime-other-contract",
    "runtime-download-source",
    "external-installer",
    "runtime-package-manager",
    "runtime-source-clone",
}


def category(component: dict) -> str:
    for item in component.get("properties", []):
        if item.get("name") == "curiosity:category":
            return item.get("value", "")
    return ""


def verify(sbom: dict, archive: Path | None) -> list[str]:
    problems = []
    if sbom.get("bomFormat") != "CycloneDX" or sbom.get("specVersion") != "1.5":
        problems.append("not a CycloneDX 1.5 document")
    root = (sbom.get("metadata") or {}).get("component") or {}
    for key in ("name", "version", "purl"):
        if not root.get(key):
            problems.append(f"metadata.component.{key} is missing")
    components = sbom.get("components") or []
    refs = Counter(item.get("bom-ref") for item in components)
    problems += [f"duplicate bom-ref {ref}" for ref, count in refs.items() if count > 1 or not ref]
    for item in components:
        if not item.get("name") or not item.get("type"):
            problems.append(f"component {item.get('bom-ref')} lacks a name or type")
        if category(item) not in KNOWN_CATEGORIES:
            problems.append(f"component {item.get('bom-ref')} has unknown category {category(item)!r}")
    counts = Counter(category(item) for item in components)
    for needed in REQUIRED_CATEGORIES:
        if not counts.get(needed):
            problems.append(f"no component with category {needed}")
    for item in components:
        if category(item) == "collection-dependency" and item.get("scope") != "excluded" and item.get("version") in ("", "unresolved", None):
            problems.append(f"direct dependency {item['name']} has no resolved version")

    if archive is not None:
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        recorded = {p.get("name"): p.get("value") for p in (sbom.get("metadata") or {}).get("properties", [])}
        if recorded.get("collection.archive.sha256") != digest:
            problems.append("SBOM archive digest does not match the artifact")
        with tarfile.open(archive, "r:gz") as tar:
            actual = {}
            for member in tar.getmembers():
                if member.isfile():
                    actual[member.name] = hashlib.sha256(tar.extractfile(member).read()).hexdigest()  # type: ignore[union-attr]
        listed = {
            item["name"]: item["hashes"][0]["content"]
            for item in components
            if category(item) == "source-file" and item.get("hashes")
        }
        for name in sorted(set(actual) - set(listed)):
            problems.append(f"{name} is in the artifact but not in the SBOM")
        for name in sorted(set(listed) - set(actual)):
            problems.append(f"{name} is in the SBOM but not in the artifact")
        for name in sorted(set(actual) & set(listed)):
            if actual[name] != listed[name]:
                problems.append(f"{name} has a different digest in the SBOM")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sbom", type=Path)
    parser.add_argument("--archive", type=Path, help="artifact to cross-check")
    args = parser.parse_args()
    problems = verify(json.loads(args.sbom.read_text()), args.archive)
    for problem in problems:
        print(f"FAIL {problem}")
    if not problems:
        print(f"ok   {args.sbom.name}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
