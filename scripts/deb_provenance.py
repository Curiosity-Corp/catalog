#!/usr/bin/env python3
"""The in-toto provenance statement of the release package (a CI tool, not in the wheel).

    scripts/deb_provenance.py DEB-SHA256SUMS > deb-provenance.intoto.json

The subjects are the files of the checksum manifest (name and sha256); the predicate is SLSA provenance v1 built
only from the CI job's own variables: which commit, which pipeline and job built it, and the build command. The
statement is not signed on its own: CI adds its checksum to the manifest that Notation signs, so verifying that one
signature covers the package and where it came from (docs/release.md). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
BUILD_TYPE = "streamstack:deb-build/v1"
SUM_LINE = re.compile(r"^([0-9a-f]{64})  (\S+)$")
REQUIRED = ("CI_PROJECT_URL", "CI_COMMIT_SHA", "CI_PIPELINE_URL", "CI_JOB_URL")


class ProvenanceError(Exception):
    """The statement cannot be made honestly; the message says what is missing."""


def read_subjects(manifest: Path) -> list[dict[str, object]]:
    subjects: list[dict[str, object]] = []
    for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        match = SUM_LINE.match(line)
        if not match:
            raise ProvenanceError(f"{manifest}:{number}: not a sha256sum line")
        subjects.append({"name": match.group(2), "digest": {"sha256": match.group(1)}})
    if not subjects:
        raise ProvenanceError(f"{manifest} lists no files")
    return subjects


def statement(subjects: list[dict[str, object]], env: Mapping[str, str]) -> dict[str, object]:
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        raise ProvenanceError(f"{', '.join(missing)} must be set: provenance is only made inside a CI job")
    project, commit = env["CI_PROJECT_URL"], env["CI_COMMIT_SHA"]
    return {
        "_type": STATEMENT_TYPE,
        "subject": subjects,
        "predicateType": PREDICATE_TYPE,
        "predicate": {
            "buildDefinition": {
                "buildType": BUILD_TYPE,
                "externalParameters": {
                    "source": f"git+{project}.git",
                    "ref": env.get("CI_COMMIT_TAG") or env.get("CI_COMMIT_REF_NAME", ""),
                    "script": "scripts/build_deb.py",
                },
                "resolvedDependencies": [{"uri": f"git+{project}.git", "digest": {"gitCommit": commit}}],
            },
            "runDetails": {
                "builder": {"id": f"{env['CI_PIPELINE_URL']}#{env.get('CI_JOB_NAME', 'deb-build')}"},
                "metadata": {"invocationId": env["CI_JOB_URL"], "startedOn": env.get("CI_JOB_STARTED_AT", "")},
            },
        },
    }


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deb_provenance.py", description=(__doc__ or "").splitlines()[0])
    parser.add_argument("manifest", help="a sha256sum manifest (DEB-SHA256SUMS)")
    args = parser.parse_args(argv)
    try:
        doc = statement(read_subjects(Path(args.manifest)), os.environ if env is None else env)
    except (ProvenanceError, OSError) as exc:
        print(f"deb_provenance.py: {exc}", file=sys.stderr)
        return 1
    json.dump(doc, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
