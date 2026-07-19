#!/usr/bin/env python3
"""Create a small CycloneDX SBOM for a built Ansible collection artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tarfile
from datetime import datetime, timezone
from pathlib import Path


def collection_info(archive: tarfile.TarFile) -> dict:
    member = archive.extractfile("MANIFEST.json")
    if member is None:
        raise SystemExit("collection artifact does not contain MANIFEST.json")
    return json.load(member)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    archive_digest = hashlib.sha256(args.archive.read_bytes()).hexdigest()
    with tarfile.open(args.archive, "r:gz") as archive:
        manifest = collection_info(archive)
        info = manifest["collection_info"]
        components = []
        for member in sorted(archive.getmembers(), key=lambda item: item.name):
            if not member.isfile():
                continue
            contents = archive.extractfile(member)
            if contents is None:
                continue
            digest = hashlib.sha256(contents.read()).hexdigest()
            components.append(
                {
                    "type": "file",
                    "name": member.name,
                    "bom-ref": f"file:{member.name}",
                    "hashes": [{"alg": "SHA-256", "content": digest}],
                }
            )

    source_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    timestamp = (
        datetime.fromtimestamp(int(source_epoch), timezone.utc)
        if source_epoch
        else datetime.now(timezone.utc)
    )
    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{archive_digest[:32]}",
        "version": 1,
        "metadata": {
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
            "component": {
                "type": "application",
                "group": info["namespace"],
                "name": info["name"],
                "version": info["version"],
                "purl": f"pkg:generic/{info['namespace']}/{info['name']}@{info['version']}",
            },
            "properties": [
                {"name": "collection.archive.sha256", "value": archive_digest}
            ],
        },
        "components": components,
    }
    args.output.write_text(json.dumps(bom, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
