"""`streamstack verify-install`: version, packaged data and entry-point checks."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from streamstack import __version__, cli, config, verify_install


def _states(results: list[verify_install.Result]) -> list[str]:
    return [r.state for r in results]


def test_a_source_tree_passes_with_a_warning_about_missing_pip_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_install, "installed_version", lambda: None)
    results = verify_install.check_version(None)
    assert _states(results) == [verify_install.WARN]
    assert __version__ in results[0].message


def test_matching_pip_metadata_is_ok_and_a_stale_copy_fails() -> None:
    assert _states(verify_install.check_version(None, recorded=__version__)) == [verify_install.OK]
    stale = verify_install.check_version(None, recorded="0.3.1")
    assert _states(stale) == [verify_install.FAIL]
    assert "0.3.1" in stale[0].message
    assert "--force" in stale[0].hint


def test_expected_version_must_match_exactly_and_accepts_a_v_prefix() -> None:
    assert verify_install.FAIL in _states(verify_install.check_version("9.9.9", recorded=__version__))
    assert verify_install.FAIL not in _states(verify_install.check_version(f"v{__version__}", recorded=__version__))


def test_shipped_data_is_complete() -> None:
    results = verify_install.check_data()
    assert _states(results) == [verify_install.OK], results


def test_missing_console_file_and_an_empty_preset_directory_are_reported(tmp_path: Path) -> None:
    shutil.copytree(config.data_dir(), tmp_path / "data")
    root = tmp_path / "data"
    (root / "console/app.js").unlink()
    for entry in (root / "audio/presets").iterdir():
        entry.unlink()
    results = verify_install.check_data(root)
    assert _states(results) == [verify_install.FAIL]
    assert "console/app.js" in results[0].message
    assert "audio/presets/ (empty)" in results[0].message


def test_entry_points_resolve_and_the_tray_is_only_a_warning_without_gi() -> None:
    results = verify_install.check_entry_points()
    by_name = {r.message.split(":")[0].split(" ")[0]: r for r in results}
    assert set(by_name) == set(verify_install.EXPECTED_ENTRY_POINTS)
    assert verify_install.FAIL not in _states(results), results


def test_a_broken_entry_point_fails_and_a_broken_tray_warns() -> None:
    def importer(name: str) -> object:
        raise ImportError(f"cannot import {name}")

    by_state = {r.message.split(":")[0]: r.state for r in verify_install.check_entry_points(importer)}
    assert by_state["streamstack-tray"] == verify_install.WARN
    assert by_state["streamstack"] == verify_install.FAIL
    assert by_state["streamstack-console"] == verify_install.FAIL


def test_every_declared_script_is_expected() -> None:
    tomllib = pytest.importorskip("tomllib")  # 3.11+; the 3.13 job covers it

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text())["project"]["scripts"]
    assert scripts == verify_install.EXPECTED_ENTRY_POINTS


def test_signature_instructions_name_the_release_files_and_the_commands() -> None:
    text = verify_install.signature_instructions("0.0.8")
    for needle in ("RELEASE-SHA256SUMS.cose.sig", "notation blob verify", "sha256sum --check", "python-sbom.cdx.json"):
        assert needle in text
    assert "streamstack-0.0.8-py3-none-any.whl" in text


def test_signature_instructions_take_the_host_from_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI_SERVER_HOST", raising=False)
    monkeypatch.delenv("STREAMSTACK_GITLAB_HOST", raising=False)
    unset = verify_install.signature_instructions("0.0.8")
    assert "https://$STREAMSTACK_GITLAB_HOST/api/v4/projects/2122/packages/generic/streamstack-release/0.0.8/" in unset
    assert "export STREAMSTACK_GITLAB_HOST=" in unset
    monkeypatch.setenv("STREAMSTACK_GITLAB_HOST", "gitlab.example")
    configured = verify_install.signature_instructions("0.0.8")
    assert "https://gitlab.example/api/v4/projects/2122/packages/generic/streamstack-release/0.0.8/" in configured
    assert "export STREAMSTACK_GITLAB_HOST" not in configured


def test_command_exit_codes(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_install, "installed_version", lambda: None)
    assert cli.main(["verify-install"]) == 0
    out = capsys.readouterr().out
    assert "install verified" in out
    assert "notation blob verify" in out
    assert cli.main(["verify-install", "--expect-version", "9.9.9"]) == 1
    assert "install is damaged" in capsys.readouterr().out
