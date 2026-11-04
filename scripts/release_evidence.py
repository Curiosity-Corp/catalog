#!/usr/bin/env python3
"""Build and verify the evidence that travels with a collection artifact.

``build ARCHIVE SBOM OUTDIR`` writes, next to copies of nothing (the artifact and
SBOM stay where they are):

* ``SHA256SUMS``: ``sha256sum -c`` compatible digests of the artifact, the SBOM
  and, when given, the verification report;
* ``toolchain.json``: the Python and package versions, lock-file digests and
  CI image this build ran with;
* ``provenance.intoto.json``: an in-toto Statement (SLSA provenance v1 shaped)
  whose subjects are the artifact and SBOM and whose predicate records the
  commit, ref, pipeline/job, runner, lock-file digests and SBOM digest. It is
  *unsigned* here: signing happens on the key holder's machine (see
  docs/release-signing.md), never in CI.

``verify DIR`` re-checks those files without trusting any of them alone: the
sums, the attestation subjects, the SBOM digest the attestation names, an
optional expected commit, and, when a detached ``.asc``/``.sig`` signature sits
next to a file and ``--fingerprint`` is given, that gpg reports a valid
signature by exactly that key.

Nothing here reads a secret. CI values come from predefined GitLab variables
that are not secret; the clone URL (which can embed a job token) is never used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
BUILD_TYPE = "urn:curiosity:catalog:gitlab-ci:collection-build:v1"
LOCK_FILES = ("ci/requirements.txt", "ci/requirements.yml", ".gitlab-ci.yml", "galaxy.yml")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def toolchain(root: Path, env: dict[str, str]) -> dict:
    packages = sorted(
        ({"name": dist.metadata["Name"], "version": dist.version} for dist in metadata.distributions() if dist.metadata["Name"]),
        key=lambda item: item["name"].lower(),
    )
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": f"{platform.system().lower()}-{platform.machine()}",
        "ci_image": env.get("CI_JOB_IMAGE", "not running in CI"),
        "source_date_epoch": env.get("SOURCE_DATE_EPOCH", ""),
        "locks": {name: sha256(root / name) for name in LOCK_FILES if (root / name).is_file()},
        "packages": packages,
    }


def provenance(archive: Path, sbom: Path, tools: dict, env: dict[str, str]) -> dict:
    commit = env.get("CI_COMMIT_SHA", "")
    project_url = env.get("CI_PROJECT_URL", "")
    dependencies = [{"name": name, "digest": {"sha256": digest}} for name, digest in sorted(tools["locks"].items())]
    dependencies.append({"name": sbom.name, "digest": {"sha256": sha256(sbom)}})
    if commit:
        dependencies.insert(0, {"uri": f"git+{project_url}@{env.get('CI_COMMIT_REF_NAME', '')}", "digest": {"gitCommit": commit}})
    return {
        "_type": STATEMENT_TYPE,
        "subject": [
            {"name": archive.name, "digest": {"sha256": sha256(archive)}},
            {"name": sbom.name, "digest": {"sha256": sha256(sbom)}},
        ],
        "predicateType": PREDICATE_TYPE,
        "predicate": {
            "buildDefinition": {
                "buildType": BUILD_TYPE,
                "externalParameters": {
                    "project": project_url,
                    "ref": env.get("CI_COMMIT_REF_NAME", ""),
                    "tag": env.get("CI_COMMIT_TAG", ""),
                    "pipelineSource": env.get("CI_PIPELINE_SOURCE", ""),
                },
                "internalParameters": {
                    "image": tools["ci_image"],
                    "sourceDateEpoch": tools["source_date_epoch"],
                    "python": tools["python"],
                },
                "resolvedDependencies": dependencies,
            },
            "runDetails": {
                "builder": {
                    "id": env.get("CI_RUNNER_DESCRIPTION", "") or "unknown-runner",
                    "version": {"runner": env.get("CI_RUNNER_VERSION", "")},
                },
                "metadata": {
                    "invocationId": env.get("CI_JOB_URL", ""),
                    "pipelineId": env.get("CI_PIPELINE_ID", ""),
                    "startedOn": env.get("CI_JOB_STARTED_AT", ""),
                },
                "byproducts": [{"name": "toolchain.json"}],
            },
            "signed": False,
        },
    }


def cmd_build(args: argparse.Namespace) -> int:
    env = dict(os.environ)
    out: Path = args.outdir
    out.mkdir(parents=True, exist_ok=True)
    tools = toolchain(args.root, env)
    (out / "toolchain.json").write_text(json.dumps(tools, indent=2, sort_keys=True) + "\n")
    statement = provenance(args.archive, args.sbom, tools, env)
    (out / "provenance.intoto.json").write_text(json.dumps(statement, indent=2, sort_keys=True) + "\n")
    lines = [f"{sha256(path)}  {path.name}" for path in (args.archive, args.sbom, *(args.extra or []))]
    (out / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    print(f"wrote {out}/SHA256SUMS, toolchain.json, provenance.intoto.json")
    return 0


def gpg_verify(signature: Path, target: Path, fingerprint: str) -> str | None:
    result = subprocess.run(
        ["gpg", "--batch", "--status-fd", "1", "--verify", str(signature), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    valid = [line.split() for line in result.stdout.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
    # VALIDSIG <signing-key-fpr> ... <primary-key-fpr>: accept the owner's primary key too.
    fingerprints = {token.upper() for fields in valid for token in (fields[2], fields[-1]) if re.fullmatch(r"[0-9A-Fa-f]{40}", token)}
    if result.returncode != 0 or not valid:
        return f"{signature.name}: gpg reports no valid signature"
    if fingerprint.replace(" ", "").upper() not in fingerprints:
        return f"{signature.name}: valid signature, but not by the expected key"
    return None


def verify_dir(directory: Path, commit: str, fingerprint: str) -> list[str]:
    problems: list[str] = []
    sums = directory / "SHA256SUMS"
    if not sums.is_file():
        return ["SHA256SUMS is missing"]
    listed = {}
    for line in sums.read_text().splitlines():
        match = re.match(r"^([0-9a-f]{64})  (\S+)$", line)
        if not match:
            problems.append(f"malformed SHA256SUMS line: {line[:60]!r}")
            continue
        listed[match.group(2)] = match.group(1)
    for name, digest in sorted(listed.items()):
        path = directory / name
        if not path.is_file():
            problems.append(f"{name} is listed in SHA256SUMS but missing")
        elif sha256(path) != digest:
            problems.append(f"{name} does not match SHA256SUMS")
    if not any(name.endswith(".tar.gz") for name in listed):
        problems.append("SHA256SUMS does not list a collection artifact")
    statement_path = directory / "provenance.intoto.json"
    if not statement_path.is_file():
        problems.append("provenance.intoto.json is missing")
    else:
        statement = json.loads(statement_path.read_text())
        if statement.get("_type") != STATEMENT_TYPE or statement.get("predicateType") != PREDICATE_TYPE:
            problems.append("provenance is not an in-toto/SLSA v1 statement")
        for subject in statement.get("subject", []):
            path = directory / subject["name"]
            if not path.is_file():
                problems.append(f"attested subject {subject['name']} is missing")
            elif sha256(path) != subject["digest"]["sha256"]:
                problems.append(f"attested subject {subject['name']} was modified after the build")
        names = {subject["name"] for subject in statement.get("subject", [])}
        if not any(name.endswith(".tar.gz") for name in names):
            problems.append("provenance has no collection artifact subject")
        resolved = statement.get("predicate", {}).get("buildDefinition", {}).get("resolvedDependencies", [])
        commits = [item["digest"]["gitCommit"] for item in resolved if "gitCommit" in item.get("digest", {})]
        if commit and commit not in commits:
            problems.append(f"provenance does not name commit {commit}")
        if not commits:
            problems.append("provenance does not record a source commit")
    if not (directory / "toolchain.json").is_file():
        problems.append("toolchain.json is missing")
    signatures = sorted(list(directory.glob("*.asc")) + list(directory.glob("*.sig")))
    for signature in signatures:
        target = directory / signature.name.rsplit(".", 1)[0]
        if not target.is_file():
            problems.append(f"{signature.name} has no file to verify")
        elif fingerprint:
            error = gpg_verify(signature, target, fingerprint)
            if error:
                problems.append(error)
    if fingerprint and not signatures:
        problems.append("a fingerprint was given but no .asc/.sig signature is present")
    return problems


def cmd_verify(args: argparse.Namespace) -> int:
    problems = verify_dir(args.directory, args.commit or "", args.fingerprint or "")
    for problem in problems:
        print(f"FAIL {problem}")
    if not problems:
        signed = any(args.directory.glob("*.asc")) or any(args.directory.glob("*.sig"))
        print("ok   checksums and provenance agree" + ("; signatures verified" if signed and args.fingerprint else "; unsigned (bootstrap path)"))
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("archive", type=Path)
    build.add_argument("sbom", type=Path)
    build.add_argument("outdir", type=Path)
    build.add_argument("--root", type=Path, default=Path("."))
    build.add_argument("--extra", type=Path, nargs="*", help="more files to list in SHA256SUMS")
    build.set_defaults(func=cmd_build)
    check = sub.add_parser("verify")
    check.add_argument("directory", type=Path)
    check.add_argument("--commit", help="expected source commit")
    check.add_argument("--fingerprint", help="expected 40-hex signing key fingerprint")
    check.set_defaults(func=cmd_verify)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
