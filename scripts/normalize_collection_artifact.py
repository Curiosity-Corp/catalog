#!/usr/bin/env python3
"""Normalize an Ansible collection archive for reproducible distribution."""

from __future__ import annotations

import argparse
import copy
import gzip
import os
import tarfile
import tempfile
from pathlib import Path


def normalized_member(member: tarfile.TarInfo, epoch: int) -> tarfile.TarInfo:
    result = copy.copy(member)
    result.mtime = epoch
    result.uid = 0
    result.gid = 0
    result.uname = ""
    result.gname = ""
    result.pax_headers = {}
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))

    temporary = tempfile.NamedTemporaryFile(
        dir=args.archive.parent,
        prefix=f".{args.archive.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary_path = Path(temporary.name)
    temporary.close()

    try:
        with tarfile.open(args.archive, "r:gz") as source:
            members = sorted(source.getmembers(), key=lambda item: item.name)
            with temporary_path.open("wb") as raw_output:
                with gzip.GzipFile(
                    fileobj=raw_output,
                    mode="wb",
                    filename="",
                    mtime=epoch,
                ) as compressed_output:
                    with tarfile.open(
                        fileobj=compressed_output,
                        mode="w",
                        format=tarfile.PAX_FORMAT,
                    ) as destination:
                        for member in members:
                            normalized = normalized_member(member, epoch)
                            contents = source.extractfile(member)
                            destination.addfile(normalized, contents)
                            if contents is not None:
                                contents.close()
        os.replace(temporary_path, args.archive)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


if __name__ == "__main__":
    main()
