"""Every test runs against a throwaway HOME and XDG tree, never the real desktop."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name, sub in (("XDG_CONFIG_HOME", ".config"), ("XDG_STATE_HOME", ".local/state"), ("XDG_CACHE_HOME", ".cache")):
        monkeypatch.setenv(name, str(home / sub))
    monkeypatch.setenv("STREAMSTACK_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("NO_COLOR", "1")
    for name in ("STREAMSTACK_SHARE", "STREAMSTACK_AUTO_VERBOSE", "STREAMSTACK_AUTO_CLAUDE", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return home
