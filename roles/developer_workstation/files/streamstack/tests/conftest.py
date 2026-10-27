"""Every test runs against a throwaway HOME and XDG tree, never the real desktop."""

from __future__ import annotations

from pathlib import Path

import pytest

from streamstack import cli, store, system
from streamstack.audiochain import runtime as chain_runtime
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
def clean_store_warnings() -> None:
    """The store's warning log is process-wide; no test inherits another's."""
    store.dismiss()


@pytest.fixture(autouse=True)
def no_real_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto mode must not probe (let alone drive) the camera, lights or interface of the machine running the tests.

    Tests that want hardware actuators register fakes themselves; the adapters' own tests build them directly.
    """
    monkeypatch.setattr(adapters, "hardware_actuators", lambda cfg, remember, start_gain: [])
    monkeypatch.setattr(faces, "install", lambda cfg, finders=None: (None, faces.heuristic_row("disabled in tests")))


@pytest.fixture(autouse=True)
def no_real_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may probe the devices of the machine running it: the command lines fall back to the name rules.

    Hardware tests build their own `Found` from a recording or a `FakeMachine` and patch this back in.
    """
    from streamstack.hardware import found, voice

    monkeypatch.setattr(found, "probe_now", lambda cfg=None, catalogue=None: None)
    monkeypatch.setattr(voice, "probe_now", lambda cfg=None, catalogue=None: None)
    monkeypatch.setattr(cli, "probe_now", lambda cfg=None, catalogue=None: None)


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Keep the browser suite out of the default unit run; it only runs when asked for by path (make e2e)."""
    e2e = Path(__file__).parent / "e2e"
    if collection_path != e2e:
        return None
    asked = [Path(a.split("::")[0]).resolve() for a in config.invocation_params.args if not a.startswith("-")]
    return None if any(a == e2e or e2e in a.parents for a in asked) else True


@pytest.fixture(autouse=True)
def no_real_audio_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may start a `pipewire -c` host on the machine running the tests (tests that want one use fakes)."""
    monkeypatch.setattr(chain_runtime, "start_host", lambda conf, log_path=None: None)
