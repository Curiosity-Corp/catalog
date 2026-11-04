"""ARM64 readiness that needs no ARM hardware (catalog #14)."""

from __future__ import annotations

from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())


def deb_arch(ansible_architecture: str) -> str:
    return jinja2.Template(DEFAULTS["workstation_deb_arch"]).render(ansible_architecture=ansible_architecture)


def test_apt_architecture_names_follow_debian_not_the_kernel():
    assert deb_arch("x86_64") == "amd64"
    assert deb_arch("aarch64") == "arm64", "apt rejects [arch=aarch64]"
    assert deb_arch("riscv64") == "riscv64", "an unmapped architecture is passed through, not hidden"


def test_no_apt_source_derives_its_arch_from_a_partial_mapping():
    """`ansible_architecture | replace('x86_64', 'amd64')` leaves `aarch64` untouched on ARM."""
    offenders = []
    for path in (ROLE / "tasks").glob("*.yml"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if "[arch=" in line and "workstation_deb_arch" not in line and "'arm64'" not in line and "$(" not in line:
                offenders.append(f"{path.name}:{number}")
    assert offenders == []


def test_repository_lines_render_with_a_valid_arch_on_arm64():
    rendered = []
    for path in (ROLE / "tasks").glob("repos*.yml"):
        for line in path.read_text().splitlines():
            if "[arch={{ workstation_deb_arch }}" in line:
                rendered.append(jinja2.Template(line.strip().strip('"').removeprefix("repo: ").strip('"')).render(workstation_deb_arch=deb_arch("aarch64")))
    assert rendered and all("[arch=arm64 " in line or "[arch=arm64]" in line for line in rendered)
