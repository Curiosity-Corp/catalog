"""Jobs that only run on main or on a tag, checked statically on every merge request (issue #162).

v0.0.18's tag pipeline failed on three such jobs that no merge request pipeline had exercised: python-sbom asked for an
extra that no longer exists, apt-publish ran on an image without gpgv, and gitleaks blocked on a finding that merge
request pipelines had only warned about.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
EXTRAS = set(tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"])


def job(name: str) -> str:
    """The text of one top-level job."""
    match = re.search(rf"^{re.escape(name)}:\n((?:[ \t].*\n|\n)+)", CI, re.MULTILINE)
    assert match, f"{name} is not a job in .gitlab-ci.yml"
    return match.group(1)


def jobs() -> dict[str, str]:
    names = re.findall(r"^([A-Za-z0-9_.-]+):\n", CI, re.MULTILINE)
    return {name: job(name) for name in names if name not in {"variables", "workflow", "include", "stages"}}


def test_every_extra_the_pipeline_installs_is_defined() -> None:
    named: set[str] = set()
    for group in re.findall(r'-e "\.\[([^\]]+)\]"', CI):
        named.update(x.strip() for x in group.split(","))
    sbom = re.search(r'PYTHON_SBOM_EXTRAS: "([^"]*)"', CI)
    assert sbom, "PYTHON_SBOM_EXTRAS is not set"
    named.update(sbom.group(1).split())
    assert named, "no extras found; the patterns above no longer match .gitlab-ci.yml"
    assert named <= EXTRAS, f"extras not in pyproject.toml: {sorted(named - EXTRAS)}"


def test_every_job_that_verifies_apt_signatures_runs_where_gpgv_exists() -> None:
    verifying = {name: text for name, text in jobs().items() if "apt_repo.py verify" in text}
    assert "apt-publish" in verifying
    for name, text in verifying.items():
        image = re.search(r"^  image: (\S+)", text, re.MULTILINE)
        assert image, f"{name} has no image"
        # python:3.13 (trixie) has no gpgv; the bookworm variant does, and deb-build already relies on it.
        assert image.group(1).endswith("-bookworm"), f"{name} runs on {image.group(1)}, which has no gpgv"


def test_gitleaks_blocks_on_merge_requests_too() -> None:
    assert re.search(r'^  GITLEAKS_STRICT: "true"$', CI, re.MULTILINE)


def test_the_apt_key_fingerprint_is_a_file_not_a_ci_variable() -> None:
    # A `..._KEY_...: <40 hex>` line is what gitleaks' generic-api-key rule flagged on the v0.0.18 tag.
    assert not re.search(r"KEY\w*:\s*[0-9A-F]{40}", CI)
    assert re.fullmatch(r"[0-9A-F]{40}\n?", (ROOT / "packaging" / "apt-public.fingerprint").read_text())
    assert "packaging/apt-public.fingerprint" in (ROOT / "scripts" / "ci" / "deb-sign.sh").read_text()
