"""The DeepFilterNet plugin's build recipe: the pin is one fact in four places, and the licence gate does its job."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import pytest

from streamstack.audiochain import plugin

ROOT = Path(__file__).resolve().parent.parent
RECIPE = ROOT / "images" / "dfn-ladspa"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, RECIPE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sc = load("supply_chain")
DOCKERFILE = (RECIPE / "Dockerfile").read_text()
CI = (ROOT / ".gitlab-ci.yml").read_text()


# --- the pin -----------------------------------------------------------------------------------------------------


def test_the_dockerfile_builds_the_version_and_revision_the_installer_fetches() -> None:
    assert f"/tags/v{plugin.UPSTREAM_VERSION}.tar.gz" in DOCKERFILE
    assert f"--version {plugin.PACKAGE_VERSION}" in DOCKERFILE
    assert f"build revision: {plugin.BUILD_REVISION}" in DOCKERFILE
    assert re.search(rf'DFN_LADSPA_VERSION: "{re.escape(plugin.PACKAGE_VERSION)}"', CI)


def test_the_source_digest_is_the_same_everywhere_in_the_recipe() -> None:
    digests = set(re.findall(r"sha256[:=\s]+([0-9a-f]{64})", DOCKERFILE)) | set(
        re.findall(r"--source-sha256 ([0-9a-f]{64})", DOCKERFILE)
    )
    assert len(digests) == 1, digests


def test_the_recipe_never_builds_from_a_moving_ref() -> None:
    assert "--checksum=sha256:" in DOCKERFILE and "--locked" in DOCKERFILE
    assert not re.search(r"FROM \S+:latest|refs/heads|/archive/main", DOCKERFILE)
    assert re.search(r"rust:\d+\.\d+\.\d+-", DOCKERFILE)


def test_ci_builds_with_buildkit_and_publishes_the_package_the_installer_reads() -> None:
    assert "extends: .build:buildkit" in CI and "/jobs/build-buildkit.yml" in CI
    assert "streamstack-ladspa" in CI and plugin.PACKAGE == "streamstack-ladspa"
    for name in (plugin.PLUGIN_FILE, "SHA256SUMS", "libdeep_filter_ladspa.cdx.json", "LICENSES.json"):
        assert name in CI


# --- the licence policy ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "MIT",
        "MIT/Apache-2.0",
        "MIT OR Apache-2.0",
        "Apache-2.0 WITH LLVM-exception",
        "(MIT OR Apache-2.0) AND Unicode-DFS-2016",
        "Unlicense OR MIT",
        "GPL-3.0-only OR MIT",  # one allowed alternative is enough
        "MIT+",
    ],
)
def test_permissive_expressions_pass(expression: str) -> None:
    assert sc.satisfiable(expression)


@pytest.mark.parametrize(
    "expression",
    [
        "GPL-3.0-only",
        "MIT AND GPL-2.0-only",
        "AGPL-3.0-or-later OR LGPL-2.1-only",
        "MPL-2.0",
        "SSPL-1.0",
        "Proprietary",
    ],
)
def test_copyleft_and_unknown_expressions_fail(expression: str) -> None:
    assert not sc.satisfiable(expression)


@pytest.mark.parametrize("expression", ["", "(MIT", "MIT OR", "MIT )"])
def test_broken_expressions_are_errors_not_passes(expression: str) -> None:
    with pytest.raises(sc.LicenceError):
        sc.satisfiable(expression)


def pkg(name: str, version: str, licence: str | None) -> dict[str, object]:
    return {"id": f"{name} {version}", "name": name, "version": version, "license": licence}


def metadata() -> dict[str, object]:
    packages = [
        pkg("deep-filter-ladspa", "0.5.6", "MIT/Apache-2.0"),
        pkg("tract-core", "0.19.16", "MIT/Apache-2.0"),
        pkg("libm", "0.2.7", "MIT OR Apache-2.0"),
        pkg("criterion", "0.5.0", "GPL-3.0-only"),  # a dev dependency: not linked, must not count
        pkg("cc", "1.0.0", "GPL-3.0-only"),  # a build dependency: compiled, not shipped
    ]
    kind = lambda k: [{"kind": k, "target": None}]  # noqa: E731
    nodes = [
        {
            "id": "deep-filter-ladspa 0.5.6",
            "deps": [
                {"pkg": "tract-core 0.19.16", "dep_kinds": kind(None)},
                {"pkg": "criterion 0.5.0", "dep_kinds": kind("dev")},
                {"pkg": "cc 1.0.0", "dep_kinds": kind("build")},
            ],
        },
        {"id": "tract-core 0.19.16", "deps": [{"pkg": "libm 0.2.7", "dep_kinds": kind(None)}]},
        {"id": "libm 0.2.7", "deps": []},
        {"id": "criterion 0.5.0", "deps": []},
        {"id": "cc 1.0.0", "deps": []},
    ]
    return {"packages": packages, "resolve": {"nodes": nodes}}


def test_only_the_crates_linked_into_the_plugin_are_audited() -> None:
    names = [p["name"] for p in sc.linked_packages(metadata(), "deep-filter-ladspa")]
    assert sorted(names) == ["deep-filter-ladspa", "libm", "tract-core"]


def test_a_crate_with_no_licence_or_a_copyleft_one_is_a_problem() -> None:
    rows, problems = sc.audit([pkg("a", "1", "MIT"), pkg("b", "2", None), pkg("c", "3", "GPL-3.0-only")])
    assert [r["verdict"] for r in rows] == ["allowed", "no licence stated", "not allowed"]
    assert len(problems) == 2 and "b 2" in problems[0] and "c 3" in problems[1]


def run_main(tmp_path: Path, meta: dict[str, object]) -> int:
    (tmp_path / "meta.json").write_text(json.dumps(meta))
    (tmp_path / "Cargo.lock").write_text(
        '[[package]]\nname = "libm"\nversion = "0.2.7"\nchecksum = "' + "ab" * 32 + '"\n'
        '[[package]]\nname = "tract-core"\nversion = "0.19.16"\nchecksum = "' + "cd" * 32 + '"\n'
    )
    (tmp_path / "plugin.so").write_bytes(b"\x7fELFplugin")
    return int(
        sc.main(
            [
                "--metadata",
                str(tmp_path / "meta.json"),
                "--lock",
                str(tmp_path / "Cargo.lock"),
                "--so",
                str(tmp_path / "plugin.so"),
                "--out",
                str(tmp_path / "out"),
                "--version",
                "0.5.6-1",
                "--source-url",
                "https://example.invalid/v0.5.6.tar.gz",
                "--source-sha256",
                "e" * 64,
            ]
        )
    )


def test_the_sbom_lists_the_crates_with_their_checksums_the_model_and_the_plugin_hash(tmp_path: Path) -> None:
    assert run_main(tmp_path, metadata()) == 0
    bom = json.loads((tmp_path / "out" / "libdeep_filter_ladspa.cdx.json").read_text())
    assert bom["bomFormat"] == "CycloneDX" and bom["specVersion"] == "1.5"
    by_name = {c["name"]: c for c in bom["components"]}
    assert by_name["libm"]["purl"] == "pkg:cargo/libm@0.2.7"
    assert by_name["libm"]["hashes"] == [{"alg": "SHA-256", "content": "ab" * 32}]
    assert by_name["libm"]["licenses"] == [{"expression": "MIT OR Apache-2.0"}]
    assert by_name["tract-core"]["licenses"] == [{"expression": "MIT OR Apache-2.0"}]  # cargo's A/B is OR
    assert "criterion" not in by_name and "cc" not in by_name
    assert any(c["type"] == "machine-learning-model" for c in bom["components"])
    root = bom["metadata"]["component"]
    assert root["version"] == "0.5.6-1" and len(root["hashes"][0]["content"]) == 64
    licences = json.loads((tmp_path / "out" / "LICENSES.json").read_text())
    assert {row["name"] for row in licences} == {"deep-filter-ladspa", "libm", "tract-core"}


def test_a_copyleft_crate_fails_the_build_but_still_writes_the_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    meta = metadata()
    meta["packages"][1]["license"] = "GPL-3.0-only"  # type: ignore[index]
    assert run_main(tmp_path, meta) == 1
    assert "tract-core 0.19.16" in capsys.readouterr().err
    assert (tmp_path / "out" / "LICENSES.json").exists()


def test_cargo_profile_settings_go_through_config_not_environment_variables() -> None:
    """`CARGO_PROFILE_RELEASE_LTO_*` is read as profile `release`, key `lto_*`, and breaks the build (pipeline 96222);
    a custom profile such as `release-lto` can only be set with `--config` or a config file."""
    assert "CARGO_PROFILE_" not in DOCKERFILE
    build = next(line for line in DOCKERFILE.splitlines() if line.startswith("RUN cargo build"))
    assert "--locked" in build and "--profile release-lto" in build
    assert "--config profile.release-lto.codegen-units=1" in build
