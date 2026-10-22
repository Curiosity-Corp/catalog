"""Every test runs against a throwaway HOME and XDG tree, never the real desktop."""

from __future__ import annotations

from pathlib import Path

import pytest

from streamstack import cli, system
from streamstack.director import adapters, faces


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name, sub in (("XDG_CONFIG_HOME", ".config"), ("XDG_STATE_HOME", ".local/state"), ("XDG_CACHE_HOME", ".cache")):
        monkeypatch.setenv(name, str(home / sub))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    monkeypatch.setenv("STREAMSTACK_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("STREAMSTACK_SETUP_SERVICE", "off")  # `streamstack setup` must not touch the real systemd
    for name in ("STREAMSTACK_SHARE", "STREAMSTACK_AUTO_VERBOSE", "STREAMSTACK_AUTO_CLAUDE", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    # No test may read the capture card plugged into the machine that runs it.
    monkeypatch.setattr(system, "find_camlink", lambda: "")
    monkeypatch.setattr(cli, "find_camlink", lambda: "")
    return home


@pytest.fixture(autouse=True)
def no_real_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto mode must not probe (let alone drive) the camera, lights or interface of the machine running the tests.

    Tests that want hardware actuators register fakes themselves; the adapters' own tests build them directly.
    """
    monkeypatch.setattr(adapters, "hardware_actuators", lambda cfg, remember, start_gain: [])
    monkeypatch.setattr(faces, "install", lambda cfg, finders=None: (None, faces.heuristic_row("disabled in tests")))


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Keep the browser suite out of the default unit run; it only runs when asked for by path (make e2e)."""
    e2e = Path(__file__).parent / "e2e"
    if collection_path != e2e:
        return None
    asked = [Path(a.split("::")[0]).resolve() for a in config.invocation_params.args if not a.startswith("-")]
    return None if any(a == e2e or e2e in a.parents for a in asked) else True
