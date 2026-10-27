"""Every file StreamStack keeps survives damage: the owner sets it aside, warns, and carries on with defaults."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from streamstack import auto, config, store
from streamstack.console import launcher
from streamstack.console.lighting import PresetLibrary
from streamstack.console.video import BUILTIN_LOOKS, LookLibrary
from streamstack.director import settings

TORN = '{"half": '


def aside(path: Path) -> list[str]:
    return [p.name for p in path.parent.iterdir() if ".corrupt-" in p.name]


def test_lighting_presets_survive_a_torn_file_and_restore_the_backup(tmp_path: Path) -> None:
    library = PresetLibrary(tmp_path / "lighting.json")
    library.save("Warm", 60, 3200)
    library.save("Cool", 80, 6000)
    library.path.write_text(TORN)
    assert {p["name"] for p in library.listing() if not p["builtin"]} == {"Warm"}  # the copy before the last save
    assert aside(library.path)
    assert store.warnings()[0]["action"] == "restored-backup"
    library.save("Again", 50, 4000)  # and it keeps working
    assert {"Warm", "Again"} <= set(library.user())


def test_lighting_presets_fall_back_to_the_builtins_without_a_backup(tmp_path: Path) -> None:
    library = PresetLibrary(tmp_path / "lighting.json")
    library.path.write_text("[1, 2, 3]")  # valid JSON, wrong shape
    assert library.user() == {}
    assert store.warnings()[0]["action"] == "defaults"


def test_saved_looks_survive_a_truncated_file(tmp_path: Path) -> None:
    library = LookLibrary(tmp_path / "looks.json")
    library.path.write_bytes(b"")
    assert library.user() == {}
    assert aside(library.path)
    library.save("Mine", dict(next(iter(BUILTIN_LOOKS.values()))))
    assert "Mine" in library.user()


def test_retired_video_engine_setting_is_ignored_on_read_and_stripped_from_the_user_file() -> None:
    user = config.user_env_file()
    user.parent.mkdir(parents=True)
    user.write_text("# my rig\nSTREAMSTACK_VIDEO_ENGINE=legacy\nSTREAMSTACK_FPS=24\n")
    loaded = config.load_settings()
    assert "STREAMSTACK_VIDEO_ENGINE" not in loaded and loaded["STREAMSTACK_FPS"] == "24"
    assert config.retire_leftovers() == ["STREAMSTACK_VIDEO_ENGINE"]
    text = user.read_text()
    assert "STREAMSTACK_VIDEO_ENGINE" not in text and "# my rig" in text and "STREAMSTACK_FPS=24" in text
    # the store kept a copy
    assert "STREAMSTACK_VIDEO_ENGINE=legacy" in user.with_name("streamstack.env.bak").read_text()
    assert config.retire_leftovers() == []  # nothing left to do, nothing rewritten


def test_the_retired_engines_state_records_are_cleared_and_the_rest_of_the_state_dir_is_left() -> None:
    state = config.state_dir()
    state.mkdir(parents=True)
    for name in ("camlink.json", "camlink.json.bak", "framing-restore.json", "framing.json", "looks.json"):
        (state / name).write_text("{}")
    assert config.retire_leftovers() == ["camlink.json", "camlink.json.bak", "framing-restore.json"]
    assert sorted(p.name for p in state.iterdir()) == ["framing.json", "looks.json"]


def test_a_station_with_a_torn_env_file_still_starts(tmp_path: Path) -> None:
    user = config.user_env_file()
    user.parent.mkdir(parents=True)
    user.write_bytes(b"\x00" * 12 + b"STREAMSTACK_VIDEO_ENGINE=legacy\n")
    assert config.retire_leftovers() in ([], ["STREAMSTACK_VIDEO_ENGINE"])  # never raises
    assert "STREAMSTACK_VIDEO_ENGINE" not in config.load_settings()


def test_a_record_that_cannot_be_removed_is_a_warning_not_a_failed_start(caplog: pytest.LogCaptureFixture) -> None:
    state = config.state_dir()
    state.mkdir(parents=True)
    (state / "camlink.json").write_text("{}")
    state.chmod(0o500)
    try:
        with caplog.at_level("WARNING"):
            assert config.retire_leftovers() == []
    finally:
        state.chmod(0o700)
    if os.geteuid() != 0:
        assert "could not remove the retired record" in caplog.text


def test_director_settings_survive_damage_and_are_private(tmp_path: Path) -> None:
    settings.set_persona("executive")
    settings.set_persona("daylight-natural")
    path = settings.settings_path()
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text(TORN)
    assert settings.load() == {"persona": "executive"}  # restored from the copy kept by the last save
    path.write_text(TORN)
    path.with_name(path.name + ".bak").unlink()
    assert settings.load() == {}


def test_settings_write_failure_is_a_settings_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def full(*_a: object, **_k: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(settings.SettingsError, match="No space left"):
        settings.set_persona("executive")
    assert not settings.settings_path().exists()


def test_the_auto_baseline_survives_damage(tmp_path: Path) -> None:
    auto.remember("gain", "mic", 3.0)
    auto.remember("gain", "line", 5.0)
    auto.baseline_path().write_text(TORN)
    assert auto.load_baseline() == {"gain": {"mic": 3.0}}  # the copy kept by the last save
    auto.baseline_path().write_text(TORN)
    auto.baseline_path().with_name("baseline.json.bak").unlink()
    assert auto.load_baseline() == {}
    auto.remember("gain", "mic", 4.0)  # and remembering starts afresh
    assert auto.load_baseline() == {"gain": {"mic": 4.0}}


def test_the_console_runtime_file_is_private_and_a_torn_one_means_no_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    launcher.write_runtime(os.getpid(), 1, "tok")
    assert launcher.runtime_file().stat().st_mode & 0o777 == 0o600
    assert not launcher.runtime_file().with_name("streamstack-console.json.bak").exists()  # no copy of the token
    launcher.runtime_file().write_text(TORN)
    assert launcher.read_running() is None


def test_the_settings_file_of_the_whole_stack_loads_after_any_single_damage() -> None:
    user = config.user_env_file()
    user.parent.mkdir(parents=True)
    user.write_text("STREAMSTACK_FPS=24\n")
    config.set_user_setting("STREAMSTACK_WIDTH", "1280")
    user.write_bytes(b"\x00" * 10)
    loaded = config.load_settings()
    assert loaded["STREAMSTACK_FPS"] == "24"  # restored from the copy kept by the last save
    assert "STREAMSTACK_WIDTH" not in config.read_env_file(user)


def test_remembered_output_verdicts_survive_a_torn_file(tmp_path: Path) -> None:
    from streamstack.console.outputs import Outputs

    path = tmp_path / "outputs.json"
    first = Outputs(None, store=path)
    first._results = {"speakers": {"at": "2026-10-23T10:00:00", "result": "pending", "error": None}}
    first._save_results()
    first._save_results()  # the second save keeps the first as the last good copy
    path.write_text('{"results": {"speak')  # a crash tore the file
    again = Outputs(None, store=path)
    assert again._results == first._results  # the last good copy came back
    assert list(tmp_path.glob("outputs.json.corrupt-*"))
    assert [w["action"] for w in store.warnings()] == ["restored-backup"]
