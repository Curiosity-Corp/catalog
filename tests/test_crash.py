"""Crash reports: redaction, the hooks (in a real subprocess), rotation, native crashes and listing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from streamstack import crash

STREAM_KEY = "live_123456789_AbCdEfGhIjKlMnOpQrStUvWxYz"
TOKEN = "tok_9f8e7d6c5b4a39281706f5e4d3c2b1a0"


@pytest.fixture(autouse=True)
def fresh() -> None:
    crash.reset_for_tests()


def reports() -> list[Path]:
    return sorted(crash.crash_dir().glob("*.json"))


@pytest.mark.parametrize(
    "text",
    [
        f"rtmp://live.example.com/app/{STREAM_KEY}",
        f"rtmps://a.rtmp.youtube.com:443/live2/{STREAM_KEY}?foo=1",
        f"srt://host:9000?streamid=publish/{STREAM_KEY}&passphrase=hunter2hunter2",
        f"GET /api/state?token={TOKEN} 200",
        f"Authorization: Bearer {TOKEN}",
        f"STREAMSTACK_TELEMETRY_TOKEN={TOKEN}",
        f"stream_key: {STREAM_KEY}",
        "https://user:pa55word@example.com/x",
    ],
)
def test_secrets_never_survive_redaction(text: str) -> None:
    out = crash.redact_text(text)
    for secret in (STREAM_KEY, TOKEN, "hunter2hunter2", "pa55word"):
        assert secret not in out, out


def test_redaction_shortens_the_home_directory_and_walks_structures() -> None:
    home = str(Path.home())
    out = crash.redact({"path": f"{home}/x", "stream_key": STREAM_KEY, "nested": [{"api_token": TOKEN}], "n": 3})
    assert out["path"] == "~/x"
    assert out["stream_key"] == crash.REDACTED and out["nested"][0]["api_token"] == crash.REDACTED
    assert out["n"] == 3
    assert json.dumps(crash.redact({"k": object()}))  # anything becomes JSON-safe


def test_a_report_has_the_version_stack_and_redacted_state() -> None:
    crash.set_state_provider(lambda: {"stream_key": STREAM_KEY, "health": "ok"})
    try:
        raise RuntimeError(f"push failed for rtmp://live.example.com/app/{STREAM_KEY}")
    except RuntimeError as exc:
        path = crash.report_exception(type(exc), exc, exc.__traceback__, where="test")
    crash.set_state_provider(None)
    assert path is not None
    text = path.read_text()
    assert STREAM_KEY not in text
    data = json.loads(text)
    assert data["schema"] == 1 and data["version"] and data["error"]["type"] == "RuntimeError"
    assert data["stack"][-1]["function"] == "test_a_report_has_the_version_stack_and_redacted_state"
    assert data["state"] == {"stream_key": crash.REDACTED, "health": "ok"}


def test_a_failing_state_summary_does_not_lose_the_report() -> None:
    def broken() -> dict:
        raise ValueError("no state")

    crash.set_state_provider(broken)
    try:
        raise KeyError("x")
    except KeyError as exc:
        path = crash.report_exception(type(exc), exc, exc.__traceback__)
    crash.set_state_provider(None)
    assert path is not None and json.loads(path.read_text())["state"]["error"].startswith("state summary failed")


class Clock:
    """The limiter's clock, under the test's control: the real one made this test depend on the CI pod's speed."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_handled_errors_are_rate_limited_per_place(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(crash, "_monotonic", clock)
    exc = OSError("disk")
    assert crash.record_exception(exc, "place-a") is not None
    assert crash.record_exception(exc, "place-a") is None
    assert crash.record_exception(exc, "place-b") is not None
    assert len(reports()) == 2
    clock.now += crash.HANDLED_EVERY + 0.1
    assert crash.record_exception(exc, "place-a") is not None  # the window is over, the place may report again


def test_a_slow_write_cannot_reopen_the_window_of_a_report_still_being_filed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flake of pipeline 98297: a starved CI pod stalled the first report's write for over HANDLED_EVERY seconds."""
    clock = Clock()
    monkeypatch.setattr(crash, "_monotonic", clock)
    real_write = crash.store.write_json

    def stalled(*args, **kwargs):
        clock.now += crash.HANDLED_EVERY * 3  # the disk, not the program, is slow
        return real_write(*args, **kwargs)

    exc = OSError("disk")
    monkeypatch.setattr(crash.store, "write_json", stalled)
    assert crash.record_exception(exc, "place-a") is not None
    assert crash.record_exception(exc, "place-a") is None


def test_only_the_newest_reports_are_kept() -> None:
    for i in range(crash.KEEP_REPORTS + 5):
        crash.record_exception(RuntimeError("x"), f"place-{i}")
    assert len(reports()) == crash.KEEP_REPORTS


def test_a_full_disk_costs_the_report_not_the_program(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    def full(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(crash.store, "write_json", full)
    assert crash.record_exception(RuntimeError("x"), "disk-full") is None
    assert "could not write a crash report" in capsys.readouterr().err


def test_list_read_and_clear() -> None:
    crash.record_exception(RuntimeError(f"token={TOKEN}"), "listing")
    (summary,) = crash.list_reports()
    assert summary["type"] == "RuntimeError" and TOKEN not in summary["message"]
    assert crash.read_report(summary["id"])["error"]["type"] == "RuntimeError"
    assert crash.read_report("../../etc/passwd") is None and crash.read_report("missing") is None
    (crash.crash_dir() / "broken.json").write_text("{not json")
    assert [r["id"] for r in crash.list_reports()] == [summary["id"]]  # a damaged report is skipped, not fatal
    assert crash.clear_reports() >= 1 and crash.list_reports() == []


def run_python(code: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(home), "XDG_STATE_HOME": str(home / ".local/state")}
    env["PYTHONPATH"] = os.pathsep.join([str(Path(crash.__file__).parents[1]), env.get("PYTHONPATH", "")])
    return subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def test_an_uncaught_exception_in_a_real_process_leaves_a_redacted_report(isolated_home: Path) -> None:
    result = run_python(
        "from streamstack import crash\n"
        "crash.install('probe', state=lambda: {'stream_key': 'k' * 20, 'ok': True})\n"
        f"raise RuntimeError('boom rtmp://live.example.com/app/{STREAM_KEY}')\n",
        isolated_home,
    )
    assert result.returncode == 1 and "RuntimeError" in result.stderr  # the normal traceback still prints
    (path,) = sorted((isolated_home / ".local/state/streamstack/crashes").glob("*.json"))
    data = json.loads(path.read_text())
    assert data["component"] == "probe" and data["error"]["type"] == "RuntimeError"
    assert STREAM_KEY not in path.read_text() and data["state"]["stream_key"] == crash.REDACTED
    assert not list(path.parent.glob("native-*.log"))  # a clean exit leaves no empty native log


def test_an_uncaught_thread_exception_is_reported_too(isolated_home: Path) -> None:
    result = run_python(
        "import threading\n"
        "from streamstack import crash\n"
        "crash.install('probe')\n"
        "t = threading.Thread(target=lambda: 1 / 0, name='worker'); t.start(); t.join()\n",
        isolated_home,
    )
    assert result.returncode == 0
    (path,) = sorted((isolated_home / ".local/state/streamstack/crashes").glob("*.json"))
    data = json.loads(path.read_text())
    assert data["error"]["type"] == "ZeroDivisionError" and "worker" in data["where"]


def test_a_fatal_signal_becomes_a_report_on_the_next_start(isolated_home: Path) -> None:
    crashed = run_python(
        "import ctypes\nfrom streamstack import crash\ncrash.install('probe')\nctypes.string_at(0)\n", isolated_home
    )
    assert crashed.returncode < 0  # killed by SIGSEGV
    directory = isolated_home / ".local/state/streamstack/crashes"
    assert list(directory.glob("native-probe-*.log"))
    assert run_python("from streamstack import crash\ncrash.install('next')\n", isolated_home).returncode == 0
    (path,) = sorted(directory.glob("*.json"))
    data = json.loads(path.read_text())
    assert data["kind"] == "native" and data["component"] == "probe" and data["error"]["type"] == "FatalSignal"
    assert not list(directory.glob("native-probe-*.log"))  # the log became the report
