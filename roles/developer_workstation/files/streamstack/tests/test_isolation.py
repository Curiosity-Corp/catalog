"""No test can reach the real Console, Canon bridge or audio sockets (a leaked deck.press once hit the live desk)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import REAL_HOME, REAL_RUNTIME_DIR

from streamstack.console import launcher


def test_runtime_dir_is_not_the_real_one(tmp_path_factory: pytest.TempPathFactory) -> None:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    assert runtime, "XDG_RUNTIME_DIR must be set to a throwaway directory"
    assert runtime != REAL_RUNTIME_DIR or not REAL_RUNTIME_DIR
    assert Path(runtime).resolve().is_relative_to(tmp_path_factory.getbasetemp().resolve())
    assert (Path(runtime).stat().st_mode & 0o777) == 0o700


def test_every_xdg_location_and_home_is_throwaway(tmp_path_factory: pytest.TempPathFactory) -> None:
    base = tmp_path_factory.getbasetemp().resolve()
    for name in ("HOME", "XDG_RUNTIME_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        value = os.environ.get(name, "")
        assert value and Path(value).resolve().is_relative_to(base), f"{name}={value!r} escapes the sandbox"
    assert os.environ["HOME"] != REAL_HOME or not REAL_HOME


def test_console_launcher_state_lives_in_the_sandbox(tmp_path_factory: pytest.TempPathFactory) -> None:
    assert launcher.runtime_dir().resolve().is_relative_to(tmp_path_factory.getbasetemp().resolve())


def test_no_session_bus_or_pulse_server_is_inherited(tmp_path: Path) -> None:
    for name in ("PULSE_RUNTIME_PATH", "DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY"):
        assert name not in os.environ
    # the conftest points PULSE_SERVER at a socket-less path inside the sandbox, never at the login's server
    assert os.environ["PULSE_SERVER"].removeprefix("unix:").startswith(str(tmp_path))
