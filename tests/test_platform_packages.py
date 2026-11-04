"""Platform package evidence (catalog #15): the probe script, and the docs that cite its results."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = sorted((ROOT / "docs/evidence").glob("platform-packages-*.json"))
DIFFERENCES = ROOT / "docs/platform-differences.md"
COMPATIBILITY = ROOT / "docs/compatibility.md"


def load():
    spec = importlib.util.spec_from_file_location("probe_platform_packages", ROOT / "scripts/probe_platform_packages.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_platform_packages"] = module
    spec.loader.exec_module(module)
    return module


probe = load()

SAMPLE = """OS ubuntu 24.04 noble amd64
PKG git 1:2.43.0-1ubuntu7
PKG nodejs MISSING
PKG virtual-thing (none)
"""


def test_role_package_list_is_static_and_unique():
    names = probe.role_packages()
    assert len(names) > 100
    assert len(names) == len(set(names))
    assert all(name == name.strip() and "{{" not in name and " " not in name for name in names)
    assert "git" in names and "build-essential" in names


def test_parse_probe_output_separates_available_and_missing():
    result = probe.parse_probe_output("ubuntu:24.04", SAMPLE, ["git", "nodejs", "virtual-thing"])
    assert result["codename"] == "noble" and result["architecture"] == "amd64"
    assert result["missing"] == ["nodejs", "virtual-thing"]
    assert result["available"] == 1
    assert result["candidates"] == {"git": "1:2.43.0-1ubuntu7"}


def test_parse_probe_output_refuses_partial_or_failed_probes():
    with pytest.raises(SystemExit, match="apt-get update failed"):
        probe.parse_probe_output("x", "APT_UPDATE_FAILED\n", ["git"])
    with pytest.raises(SystemExit, match="lacks"):
        probe.parse_probe_output("x", "OS debian 12 bookworm amd64\nPKG git 1\n", ["git", "make"])
    with pytest.raises(SystemExit, match="os-release"):
        probe.parse_probe_output("x", "PKG git 1\n", ["git"])


def test_check_file_flags_a_stale_list_and_a_bad_date(tmp_path):
    names = ["git", "make"]
    good = {"date": "2026-01-02", "packages": names, "results": [{"image": "x", "missing": ["make"]}]}
    path = tmp_path / "e.json"
    path.write_text(json.dumps(good))
    assert probe.check_file(path, names) == []
    stale = probe.check_file(path, names + ["cmake"])
    assert any("package list differs" in problem and "cmake" in problem for problem in stale)
    path.write_text(json.dumps({**good, "date": "yesterday"}))
    assert any("date" in problem for problem in probe.check_file(path, names))
    path.write_text(json.dumps({**good, "results": []}))
    assert "no results" in probe.check_file(path, names)


def test_probe_never_runs_the_role():
    source = (ROOT / "scripts/probe_platform_packages.py").read_text()
    assert "ansible-playbook" not in source and "apt-get install" not in source


# --- recorded evidence ---------------------------------------------------------

needs_evidence = pytest.mark.skipif(not EVIDENCE, reason="no recorded platform package evidence")


@needs_evidence
def test_every_recorded_result_is_dated_and_well_formed():
    for path in EVIDENCE:
        data = json.loads(path.read_text())
        assert data["date"] in path.name, f"{path.name}: file name and date disagree"
        assert data["results"], path.name
        for result in data["results"]:
            assert result["id"] in {"ubuntu", "debian"} and result["version"] and result["codename"]
            assert result["available"] + len(result["missing"]) == len(data["packages"]), result["image"]
            assert set(result["missing"]) <= set(data["packages"])


@needs_evidence
def test_the_differences_page_explains_every_missing_package_in_the_latest_evidence():
    latest = json.loads(EVIDENCE[-1].read_text())
    page = DIFFERENCES.read_text()
    assert latest["date"] in page, "docs/platform-differences.md must cite the date of the latest evidence"
    for result in latest["results"]:
        label = f"{result['id'].capitalize()} {result['version']}"
        assert label in page, f"{label} is not described in docs/platform-differences.md"
        for name in result["missing"]:
            assert f"`{name}`" in page, f"{name} is missing on {label} but not explained in docs/platform-differences.md"


@needs_evidence
def test_compatibility_matrix_cites_the_latest_evidence_date():
    latest = json.loads(EVIDENCE[-1].read_text())
    text = COMPATIBILITY.read_text()
    assert latest["date"] in text and "platform-differences.md" in text
