"""store.py: atomic writes, checked reads, quarantine and backup, warnings, and simulated crashes."""

from __future__ import annotations

import errno
import json
import multiprocessing
import os
import threading
from pathlib import Path

import pytest

from streamstack import config, store


def names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name != "home")  # `home` is the isolated HOME fixture


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_write_json_round_trips_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "dir" / "state.json"
    store.write_json(target, {"b": 2, "a": [1, 2]})
    assert store.read_json(target) == {"a": [1, 2], "b": 2}
    assert target.read_text().endswith("\n")
    assert names(target.parent) == ["state.json"]


def test_new_file_gets_the_requested_mode_and_an_existing_one_keeps_its_own(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    store.write_json(target, {"k": 1}, mode=0o600)
    assert target.stat().st_mode & 0o777 == 0o600
    target.chmod(0o640)
    store.write_json(target, {"k": 2})
    assert target.stat().st_mode & 0o777 == 0o640


def test_a_symlinked_file_stays_a_link_and_the_real_file_is_replaced(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "streamstack.env"
    real.parent.mkdir()
    real.write_text("A=1\n")
    link = tmp_path / "streamstack.env"
    link.symlink_to(real)
    store.update_env_file(link, {"A": "2"})
    assert link.is_symlink()
    assert real.read_text() == "A=2\n"


def test_unserialisable_data_touches_nothing(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    store.write_json(target, {"ok": True})
    with pytest.raises(TypeError):
        store.write_json(target, {"bad": object()})
    assert json.loads(target.read_text()) == {"ok": True}
    assert names(tmp_path) == ["state.json"]


def test_the_previous_good_copy_is_kept_as_a_backup(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    store.write_json(target, {"n": 1})
    store.write_json(target, {"n": 2})
    assert json.loads(target.read_text()) == {"n": 2}
    assert json.loads((tmp_path / "state.json.bak").read_text()) == {"n": 1}


def test_a_damaged_file_never_replaces_the_good_backup(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    store.write_json(target, {"n": 1})
    store.write_json(target, {"n": 2})
    target.write_text('{"n": ')  # damaged behind our back
    store.write_json(target, {"n": 3})
    assert json.loads((tmp_path / "state.json.bak").read_text()) == {"n": 1}


# ---------------------------------------------------------------------------
# Simulated crashes: the target is always the whole old file or the whole new one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failing", ["fsync", "replace", "write"])
def test_a_failure_part_way_leaves_the_old_file_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    target = tmp_path / "state.json"
    store.write_json(target, {"old": True})

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, "simulated crash")

    if failing == "fsync":
        monkeypatch.setattr(os, "fsync", boom)
    elif failing == "replace":
        monkeypatch.setattr(os, "replace", boom)
    else:
        monkeypatch.setattr(os, "fchmod", boom)  # runs after the bytes are written, before the rename
    with pytest.raises(OSError, match="simulated crash"):
        store.write_json(target, {"new": True}, backup=False)
    monkeypatch.undo()
    assert json.loads(target.read_text()) == {"old": True}
    assert not [n for n in names(tmp_path) if n.endswith(".tmp")]


def test_a_full_disk_is_reported_and_the_old_file_survives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "state.json"
    store.write_json(target, {"old": True})
    real_fdopen = os.fdopen

    class FullDisk:
        def __init__(self, handle) -> None:
            self.handle = handle

        def __enter__(self) -> FullDisk:
            return self

        def __exit__(self, *exc: object) -> None:
            self.handle.close()

        def write(self, data: bytes) -> int:
            self.handle.write(data[: len(data) // 2])  # a partial write, then the disk fills up
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(os, "fdopen", lambda fd, mode="r", *a, **k: FullDisk(real_fdopen(fd, mode, *a, **k)))
    with pytest.raises(OSError) as caught:
        store.write_json(target, {"new": "x" * 4096})
    assert caught.value.errno == errno.ENOSPC
    monkeypatch.undo()
    assert json.loads(target.read_text()) == {"old": True}
    assert not [n for n in names(tmp_path) if n.endswith(".tmp")]


def test_a_crash_that_leaves_a_partial_temporary_file_is_harmless(tmp_path: Path) -> None:
    """A process killed mid-write leaves `.name.xyz.tmp`; the real file is untouched and the next write works."""
    target = tmp_path / "state.json"
    store.write_json(target, {"n": 1})
    (tmp_path / ".state.json.dead.tmp").write_text('{"n": ')
    assert store.read_json(target) == {"n": 1}
    store.write_json(target, {"n": 2})
    assert store.read_json(target) == {"n": 2}
    assert store.warnings() == []


# ---------------------------------------------------------------------------
# Reading damaged files
# ---------------------------------------------------------------------------


def test_a_missing_file_gives_the_default_without_a_warning(tmp_path: Path) -> None:
    assert store.read_json(tmp_path / "none.json", {"d": 1}) == {"d": 1}
    assert store.warnings() == []


def test_the_default_is_a_copy_so_callers_cannot_change_it(tmp_path: Path) -> None:
    default: dict[str, list[int]] = {"x": []}
    store.read_json(tmp_path / "none.json", default)["x"].append(1)
    assert default == {"x": []}


@pytest.mark.parametrize(
    "damage",
    [b'{"a": 1, "b"', b"", b"   \n", b"\x00\x00\x00", b"\xff\xfe not utf8", b"[1, 2]", b'"text"'],
    ids=["truncated", "empty", "blank", "nul", "not-utf8", "wrong-shape-list", "wrong-shape-string"],
)
def test_a_damaged_file_is_set_aside_and_the_default_used(tmp_path: Path, damage: bytes) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(damage)
    assert store.read_json(target, {"d": 1}, validate=store.is_object) == {"d": 1}
    assert not target.exists()
    moved = [n for n in names(tmp_path) if ".corrupt-" in n]
    assert len(moved) == 1
    assert (tmp_path / moved[0]).read_bytes() == damage  # kept for diagnosis, byte for byte
    (warning,) = store.warnings()
    assert warning["action"] == "defaults"
    assert warning["quarantined_to"] == moved[0]


def test_the_backup_is_restored_when_the_file_is_damaged(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    store.write_json(target, {"n": 1})
    store.write_json(target, {"n": 2})
    target.write_text('{"n": ')  # a torn write from an older crash
    assert store.read_json(target, {}) == {"n": 1}
    assert json.loads(target.read_text()) == {"n": 1}  # back on disk, so the next read is quiet
    assert [w["action"] for w in store.warnings()] == ["restored-backup"]
    assert store.read_json(target, {}) == {"n": 1}
    assert len(store.warnings()) == 1


def test_a_backup_that_is_damaged_too_falls_back_to_the_default(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{")
    (tmp_path / "state.json.bak").write_text("{also")
    assert store.read_json(target, {"d": 1}) == {"d": 1}
    assert store.warnings()[0]["action"] == "defaults"


def test_only_the_newest_few_damaged_copies_are_kept(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    for i in range(store.KEEP_CORRUPT + 3):
        target.write_text("{" * (i + 1))
        store.read_json(target, {})
    assert len([n for n in names(tmp_path) if ".corrupt-" in n]) == store.KEEP_CORRUPT


def test_a_validator_can_reject_a_wrong_value_type(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"mode": 3}')
    assert store.read_json(target, {}, validate=store.object_with(mode=str)) == {}
    assert store.warnings()


def test_an_unreadable_file_is_reported_not_renamed(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.mkdir()
    assert store.read_json(target, {"d": 1}) == {"d": 1}
    assert store.warnings()[0]["action"] == "unreadable"
    assert target.is_dir()


def test_the_same_problem_is_recorded_once_and_listeners_hear_each_new_one(tmp_path: Path) -> None:
    heard: list[str] = []
    off = store.subscribe(lambda w: heard.append(w.path))
    for _ in range(3):
        store.record(tmp_path / "a.json", "is damaged", "defaults")
    store.record(tmp_path / "b.json", "is damaged", "defaults")
    off()
    store.record(tmp_path / "c.json", "is damaged", "defaults")
    assert len(heard) == 2
    assert len(store.warnings()) == 3
    first = store.warnings()[0]["id"]
    assert store.dismiss(first) == 1
    assert store.dismiss() == 2
    assert store.warnings() == []


def test_warnings_show_the_home_directory_as_a_tilde(isolated_home: Path) -> None:
    store.record(isolated_home / ".config/streamstack/x.json", "is damaged", "defaults")
    assert store.warnings()[0]["path"] == "~/.config/streamstack/x.json"


def test_a_listener_that_raises_does_not_break_recording(tmp_path: Path) -> None:
    def bad(_warning: store.StoreWarning) -> None:
        raise RuntimeError("listener bug")

    off = store.subscribe(bad)
    try:
        store.record(tmp_path / "a.json", "is damaged", "defaults")
    finally:
        off()
    assert len(store.warnings()) == 1


# ---------------------------------------------------------------------------
# Concurrent writers
# ---------------------------------------------------------------------------


def test_concurrent_writers_and_readers_never_see_a_partial_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    store.write_json(target, {"writer": -1, "pad": ""})
    stop = threading.Event()
    errors: list[str] = []

    def writer(n: int) -> None:
        try:
            for i in range(40):
                store.write_json(target, {"writer": n, "i": i, "pad": "x" * 20000})
        except Exception as exc:
            errors.append(f"writer {n}: {exc!r}")

    def reader() -> None:
        while not stop.is_set():
            try:
                data = json.loads(target.read_text())  # raw read: a torn file would raise here
                assert data["pad"] in ("", "x" * 20000)
            except Exception as exc:
                errors.append(f"reader: {exc!r}")
                return

    readers = [threading.Thread(target=reader) for _ in range(3)]
    writers = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in readers:
        t.join()
    assert errors == []
    assert store.read_json(target)["writer"] in range(6)
    assert store.warnings() == []
    assert not [n for n in names(tmp_path) if n.endswith((".tmp", ".link"))]


def _bump(path: str, key: str, times: int) -> None:
    for _ in range(times):
        with store.locked(path):
            data = store.read_json(path, {})
            data[key] = data.get(key, 0) + 1
            # The test is about the lock, not the disk: on a busy CI runner 125 fsynced saves took over a minute.
            store.write_json(path, data, durable=False, backup=False)


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_the_lock_serialises_read_modify_write_across_processes(tmp_path: Path) -> None:
    target = tmp_path / "counts.json"
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_bump, args=(str(target), f"p{i}", 25)) for i in range(4)]
    shared = ctx.Process(target=_bump, args=(str(target), "shared", 25))
    for p in [*procs, shared]:
        p.start()
    for p in [*procs, shared]:
        p.join(timeout=60)
        assert p.exitcode == 0
    data = store.read_json(target)
    assert data == {"p0": 25, "p1": 25, "p2": 25, "p3": 25, "shared": 25}


# ---------------------------------------------------------------------------
# KEY=VALUE files
# ---------------------------------------------------------------------------


def test_update_env_file_changes_one_key_and_keeps_comments_and_order(tmp_path: Path) -> None:
    env = tmp_path / "streamstack.env"
    env.write_text("# my rig\nSTREAMSTACK_FPS=60\nexport OTHER='a b'\n\nSTREAMSTACK_WIDTH=1280\n")
    store.update_env_file(env, {"STREAMSTACK_FPS": "30", "NEW_KEY": "a value"})
    assert (
        env.read_text()
        == "# my rig\nSTREAMSTACK_FPS=30\nexport OTHER='a b'\n\nSTREAMSTACK_WIDTH=1280\nNEW_KEY='a value'\n"
    )
    assert config.read_env_file(env)["NEW_KEY"] == "a value"
    store.update_env_file(env, {"NEW_KEY": None})
    assert "NEW_KEY" not in env.read_text()


def test_update_env_file_is_not_fooled_by_a_commented_key(tmp_path: Path) -> None:
    env = tmp_path / "streamstack.env"
    env.write_text("# STREAMSTACK_FPS=60\n")
    store.update_env_file(env, {"STREAMSTACK_FPS": "30"})
    assert env.read_text() == "# STREAMSTACK_FPS=60\nSTREAMSTACK_FPS=30\n"


@pytest.mark.parametrize(("key", "value"), [("1BAD", "x"), ("A B", "x"), ("OK", "two\nlines"), ("OK", "nul\x00")])
def test_update_env_file_rejects_bad_names_and_values(tmp_path: Path, key: str, value: str) -> None:
    env = tmp_path / "streamstack.env"
    with pytest.raises(ValueError, match=r"setting name|one line"):
        store.update_env_file(env, {key: value})
    assert not env.exists()


def test_a_damaged_user_env_file_does_not_stop_load_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user = config.user_env_file()
    user.parent.mkdir(parents=True)
    user.write_bytes(b"STREAMSTACK_FPS=60\n\x00\x00garbage")
    settings = config.load_settings()
    assert settings["STREAMSTACK_FPS"] == config.DEFAULTS["STREAMSTACK_FPS"]
    assert store.warnings()[0]["action"] == "defaults"
    assert any(".corrupt-" in p.name for p in user.parent.iterdir())


def test_a_non_utf8_env_file_is_not_a_crash(tmp_path: Path) -> None:
    env = tmp_path / "x.env"
    env.write_bytes(b"STREAMSTACK_FPS=\xff\xfe\n")
    assert config.read_env_file(env) == {}


def test_unparseable_env_lines_are_skipped_with_one_warning(tmp_path: Path) -> None:
    env = tmp_path / "x.env"
    env.write_text("GOOD=1\nthis is not a setting\nBAD='unterminated\n")
    assert config.read_env_file(env) == {"GOOD": "1"}
    (warning,) = store.warnings()
    assert warning["action"] == "ignored-lines"
    assert "2 lines" in warning["problem"]


def test_set_user_setting_creates_a_private_file_and_load_settings_sees_it() -> None:
    config.set_user_setting("STREAMSTACK_TELEMETRY", "true")
    path = config.user_env_file()
    assert path.stat().st_mode & 0o777 == 0o600
    assert config.load_settings()["STREAMSTACK_TELEMETRY"] == "true"
