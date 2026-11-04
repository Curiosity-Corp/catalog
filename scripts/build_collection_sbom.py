#!/usr/bin/env python3
"""Create a CycloneDX SBOM for a built Ansible collection artifact.

The SBOM separates four kinds of component with the ``curiosity:category``
property so a consumer can tell what ships from what is fetched or used to
build:

* ``source-file``: every file inside the artifact, with its SHA-256;
* ``collection-dependency``: direct collection dependencies from MANIFEST.json,
  with the declared constraint and the version CI resolves (``ci/requirements.yml``);
* ``ci-tool`` and ``ci-image``: the pinned Python toolchain and base image that
  built the artifact (scope ``excluded``: not part of what is installed);
* ``runtime-release-download``, ``runtime-download-source``, ``runtime-other-contract``, ``runtime-package-source``,
  ``external-installer``, ``runtime-package-manager`` and ``runtime-source-clone``:
  what the role fetches when it runs on a workstation, found by
  ``scripts/external_sources.py``. Versions are resolved at run time, so these
  components carry no version, only the source, installer and verification method.

The output is deterministic for a given archive and ``SOURCE_DATE_EPOCH``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import external_sources  # noqa: E402

KIND_CATEGORY = {
    "apt-source": "runtime-package-source",
    "download": "runtime-download-source",
    "installer-script": "external-installer",
    "command-fetch": "external-installer",
    "package-manager": "runtime-package-manager",
    "source-clone": "runtime-source-clone",
}
IMAGE = re.compile(r"^image:\s*(?P<name>[^:@\s]+)(?::(?P<tag>[^@\s]+))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?\s*$", re.M)


def collection_info(archive: tarfile.TarFile) -> dict:
    member = archive.extractfile("MANIFEST.json")
    if member is None:
        raise SystemExit("collection artifact does not contain MANIFEST.json")
    return json.load(member)


def prop(name: str, value: str) -> dict:
    return {"name": f"curiosity:{name}", "value": value}


def parse_lock(path: Path) -> list[tuple[str, str]]:
    packages = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)", line)
        if match:
            packages.append((match.group(1), match.group(2)))
    return sorted(packages, key=lambda item: item[0].lower())


def resolved_collections(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    document = yaml.safe_load(path.read_text()) or {}
    return {item["name"]: str(item.get("version", "")) for item in document.get("collections", []) if isinstance(item, dict)}


def ci_image(path: Path) -> dict | None:
    if not path.is_file():
        return None
    match = IMAGE.search(path.read_text())
    return match.groupdict() if match else None


def build(archive_path: Path, lock: Path, collections: Path, ci_file: Path) -> dict:
    archive_digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    role_files: list[tuple[str, str]] = []
    components: list[dict] = []
    with tarfile.open(archive_path, "r:gz") as archive:
        manifest = collection_info(archive)
        info = manifest["collection_info"]
        for member in sorted(archive.getmembers(), key=lambda item: item.name):
            if not member.isfile():
                continue
            contents = archive.extractfile(member)
            if contents is None:
                continue
            data = contents.read()
            components.append(
                {
                    "type": "file",
                    "name": member.name,
                    "bom-ref": f"file:{member.name}",
                    "hashes": [{"alg": "SHA-256", "content": hashlib.sha256(data).hexdigest()}],
                    "properties": [prop("category", "source-file")],
                }
            )
            if re.match(r"^roles/[^/]+/(defaults|tasks)/[^/]+\.ya?ml$", member.name):
                role_files.append((member.name, data.decode("utf-8", errors="replace")))

    # Direct collection dependencies, with the version CI resolves for them.
    pins = resolved_collections(collections)
    for name, constraint in sorted((info.get("dependencies") or {}).items()):
        resolved = pins.get(name, "")
        version = resolved or "unresolved"
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "bom-ref": f"collection:{name}",
                "purl": f"pkg:generic/{name}@{version}?repository_url=https://galaxy.ansible.com",
                "properties": [
                    prop("category", "collection-dependency"),
                    prop("declared-constraint", str(constraint)),
                    prop("resolved-by", str(collections) if resolved else "not pinned"),
                ],
            }
        )
    for name, version in sorted(pins.items()):
        if name not in (info.get("dependencies") or {}):
            components.append(
                {
                    "type": "library",
                    "name": name,
                    "version": version,
                    "bom-ref": f"collection:{name}",
                    "purl": f"pkg:generic/{name}@{version}?repository_url=https://galaxy.ansible.com",
                    "scope": "excluded",
                    "properties": [prop("category", "collection-dependency"), prop("declared-constraint", "transitive; pinned for CI")],
                }
            )

    # CI toolchain.
    if lock.is_file():
        for name, version in parse_lock(lock):
            components.append(
                {
                    "type": "library",
                    "name": name,
                    "version": version,
                    "bom-ref": f"pypi:{name.lower()}",
                    "purl": f"pkg:pypi/{name.lower().replace('_', '-')}@{version}",
                    "scope": "excluded",
                    "properties": [prop("category", "ci-tool")],
                }
            )
    image = ci_image(ci_file)
    if image:
        reference = image["name"] + (f":{image['tag']}" if image.get("tag") else "")
        components.append(
            {
                "type": "container",
                "name": image["name"],
                "version": image.get("tag") or "",
                "bom-ref": f"image:{reference}",
                "scope": "excluded",
                "properties": [prop("category", "ci-image")]
                + ([prop("digest", image["digest"])] if image.get("digest") else [prop("digest", "not pinned")]),
            }
        )

    # What the role fetches when it runs.
    found = external_sources.scan_role_files(role_files)
    for release in found["github_releases"]:
        components.append(
            {
                "type": "application",
                "name": release["name"],
                "bom-ref": f"release:{release['name']}",
                "externalReferences": [{"type": "vcs", "url": f"https://github.com/{release['repository']}"}],
                "properties": [
                    prop("category", "runtime-release-download"),
                    prop("installer", release["installer"]),
                    prop("verification", release["verification"]),
                    prop("version", "resolved at run time"),
                ],
            }
        )
    for contract in found["other_contracts"]:
        components.append(
            {
                "type": "application",
                "name": contract["name"],
                "bom-ref": f"contract:{contract['name']}",
                "properties": [
                    prop("category", "runtime-other-contract"),
                    prop("installer", contract["installer"]),
                    prop("verification", contract["verification"]),
                    prop("version", "resolved at run time"),
                ],
            }
        )
    for source in found["hosts"]:
        for kind in source["kinds"]:
            category = KIND_CATEGORY[kind]
            components.append(
                {
                    "type": "data",
                    "name": source["host"],
                    "bom-ref": f"source:{kind}:{source['host']}",
                    "properties": [prop("category", category), prop("found-in", ",".join(source["files"]))]
                    + ([prop("pipes-to-shell", "true")] if source["pipes_to_shell"] else []),
                }
            )

    source_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    timestamp = datetime.fromtimestamp(int(source_epoch), timezone.utc) if source_epoch else datetime.now(timezone.utc)
    components.sort(key=lambda item: item["bom-ref"])
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{archive_digest[:8]}-{archive_digest[8:12]}-{archive_digest[12:16]}-{archive_digest[16:20]}-{archive_digest[20:32]}",
        "version": 1,
        "metadata": {
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
            "component": {
                "type": "application",
                "group": info["namespace"],
                "name": info["name"],
                "version": info["version"],
                "bom-ref": f"collection:{info['namespace']}.{info['name']}",
                "purl": f"pkg:generic/{info['namespace']}/{info['name']}@{info['version']}",
            },
            "properties": [prop("sbom-scope", "source files, direct collection dependencies, CI toolchain, runtime sources"), {"name": "collection.archive.sha256", "value": archive_digest}],
        },
        "components": components,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--lock", type=Path, default=Path("ci/requirements.txt"))
    parser.add_argument("--collections", type=Path, default=Path("ci/requirements.yml"))
    parser.add_argument("--ci-file", type=Path, default=Path(".gitlab-ci.yml"))
    args = parser.parse_args()
    bom = build(args.archive, args.lock, args.collections, args.ci_file)
    args.output.write_text(json.dumps(bom, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
