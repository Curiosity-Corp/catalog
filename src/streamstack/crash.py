"""Crash reports: what happened, written as JSON to the state directory, with nothing secret in it.

`install(component)` is called once by every StreamStack entry point. From then on:

* an uncaught exception (main thread, any thread, or one raised inside a finaliser) writes
  `~/.local/state/streamstack/crashes/<time>-<component>-<pid>.json`;
* `faulthandler` writes the native stack of a fatal signal (SIGSEGV, SIGABRT) to `crashes/native-<component>-<pid>.log`;
  the next start of any StreamStack program turns the non-empty log of a dead process into a JSON report (kind
  "native"), and a clean exit removes the empty one;
* `record_exception(...)` files a handled-but-serious error without stopping the program (rate limited).

A report holds the version, Python and OS name, the component and pid, the exception type and message, the stack
(file path with the home directory shortened, line number, function and source line, never local variables) and a
small allow-listed summary of the Console state. Everything passes through `redact` first: tokens, stream keys,
passphrases, passwords, Authorization headers and URL credentials are replaced by `[redacted]`, and so is any value
stored under a name that suggests a secret. Reports never leave the machine; the Console shows them on the System page.
"""

from __future__ import annotations

import atexit
import contextlib
import faulthandler
import logging
import os
import platform
import re
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

from streamstack import __version__, config, store

log = logging.getLogger("streamstack.crash")

SCHEMA = 1
KEEP_REPORTS = 20
MAX_TEXT = 2000  # characters of any one message or stack line
MAX_FRAMES = 60
HANDLED_EVERY = 10.0  # seconds between two handled-error reports of the same kind and place
REDACTED = "[redacted]"
SECRET_NAME = re.compile(
    r"(token|secret|passw|passphrase|credential|authorization|api[_-]?key|stream[_-]?key|streamkey|private|cookie|session)",
    re.IGNORECASE,
)
# `name=value` / `name: value` pairs whose name looks secret, `Bearer x`, `scheme://user:pass@host`, RTMP-style stream
# URLs (the last path segment is the key) and SRT streamid / passphrase parameters.
SECRET_PAIR = re.compile(
    r"(?i)(\b[\w.-]*(?:token|secret|passw(?:or)?d|passphrase|api[_-]?key|stream[_-]?key|streamkey|authorization|cookie)[\w.-]*"
    r"\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&\"'}]+)"
)
BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")
URL_USERINFO = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", re.IGNORECASE)
STREAM_URL = re.compile(r"((?:rtmps?|srt|rist|whip)://[^\s/\"']+(?:/[^\s/\"'?]+)*/)([^\s/\"'?]+)", re.IGNORECASE)
QUERY_SECRET = re.compile(r"(?i)([?&](?:key|token|streamid|passphrase|secret|auth)=)[^&\s\"']+")

_state: dict[str, Any] = {
    "component": "streamstack",
    "provider": None,
    "native": None,
    "installed": False,
    "previous": None,
}
_handled_at: dict[str, float] = {}
_monotonic: Callable[[], float] = time.monotonic  # the rate limiter's clock; tests swap it instead of patching `time`
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact_text(text: str) -> str:
    """`text` with secrets replaced and the home directory shortened."""
    home = str(Path.home())
    if home and home != "/":
        text = text.replace(home, "~")
    text = URL_USERINFO.sub(rf"\1{REDACTED}@", text)
    text = STREAM_URL.sub(rf"\1{REDACTED}", text)
    text = QUERY_SECRET.sub(rf"\1{REDACTED}", text)
    text = BEARER.sub(rf"\1 {REDACTED}", text)
    text = SECRET_PAIR.sub(rf"\1{REDACTED}", text)
    return text[:MAX_TEXT]


def redact(value: Any, depth: int = 0) -> Any:
    """A JSON-safe copy of `value` with secrets removed (see `redact_text`; secret-looking keys lose their value)."""
    if depth > 8:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        return {
            str(k): REDACTED if SECRET_NAME.search(str(k)) and v not in (None, "", False) else redact(v, depth + 1)
            for k, v in list(value.items())[:200]
        }
    if isinstance(value, (list, tuple, set)):
        return [redact(v, depth + 1) for v in list(value)[:100]]
    return redact_text(repr(value))


# ---------------------------------------------------------------------------
# Where reports live
# ---------------------------------------------------------------------------


def crash_dir() -> Path:
    return config.state_dir() / "crashes"


def _frames(tb: TracebackType | None) -> list[dict[str, Any]]:
    out = []
    for frame in traceback.extract_tb(tb)[-MAX_FRAMES:]:
        out.append(
            {
                "file": redact_text(frame.filename),
                "line": frame.lineno,
                "function": frame.name,
                "source": redact_text(frame.line or ""),
            }
        )
    return out


def set_state_provider(provider: Callable[[], dict[str, Any]] | None) -> None:
    """Register the function whose (allow-listed) result goes into every report as `state`."""
    _state["provider"] = provider


def context() -> dict[str, Any]:
    """The allow-listed Console summary registered with `install(state=...)`, redacted; {} when it fails."""
    provider: Callable[[], dict[str, Any]] | None = _state["provider"]
    if provider is None:
        return {}
    try:
        return redact(provider())
    except Exception as exc:  # the crash report must never fail because the summary did
        return {"error": f"state summary failed: {type(exc).__name__}"}


def _write(report: dict[str, Any]) -> Path | None:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(report["time"]))
    path = crash_dir() / f"{stamp}-{report['component']}-{report['pid']}-{report['kind']}.json"
    counter = 0
    while path.exists():
        counter += 1
        path = path.with_name(f"{path.stem}-{counter}.json")
    try:
        store.write_json(path, report, backup=False)
    except (OSError, TypeError, ValueError) as exc:
        print(f"streamstack: could not write a crash report to {path}: {exc}", file=sys.stderr)
        return None
    _prune()
    return path


def _prune() -> None:
    reports = sorted(p for p in crash_dir().glob("*.json") if p.is_file())
    for stale in reports[:-KEEP_REPORTS]:
        stale.unlink(missing_ok=True)


def _base(kind: str, component: str | None) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "kind": kind,
        "time": time.time(),
        "version": __version__,
        "python": platform.python_version(),
        "system": f"{platform.system()} {platform.machine()}",
        "component": component or _state["component"],
        "pid": os.getpid(),
        "thread": threading.current_thread().name,
    }


def report_exception(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
    *,
    kind: str = "exception",
    component: str | None = None,
    where: str | None = None,
) -> Path | None:
    """Write a report for one exception; returns its path (None when it could not be written)."""
    report = _base(kind, component)
    report.update(
        {
            "error": {"type": exc_type.__name__, "message": redact_text(str(exc))},
            "stack": _frames(tb),
            "where": where,
            "state": context(),
        }
    )
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        report["error"]["cause"] = {"type": type(cause).__name__, "message": redact_text(str(cause))}
    return _write(report)


def record_exception(exc: BaseException, where: str, component: str | None = None) -> Path | None:
    """File a handled error that deserves attention, at most once per HANDLED_EVERY seconds for the same place."""
    key = f"{type(exc).__name__}@{where}"
    now = _monotonic()
    with _lock:
        if now - _handled_at.get(key, -HANDLED_EVERY) < HANDLED_EVERY:
            return None
        _handled_at[key] = now
        del_old = [k for k, t in _handled_at.items() if now - t > 600]
        for k in del_old:
            del _handled_at[k]
    path = report_exception(type(exc), exc, exc.__traceback__, kind="handled", component=component, where=where)
    with _lock:
        # The window runs from when the report was filed, not from when it was claimed: a stalled disk must not use
        # the window up before the next error arrives (a starved CI runner did exactly that, #159).
        _handled_at[key] = _monotonic()
    return path


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


NATIVE_LOG = re.compile(r"native-(?P<component>[\w-]+)-(?P<pid>\d+)\.log")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _collect_native(directory: Path) -> None:
    """Turn a faulthandler log left by a process that died into a JSON report; tidy empty logs of dead processes."""
    for log_file in sorted(directory.glob("native-*.log")):
        match = NATIVE_LOG.fullmatch(log_file.name)
        if match is None or _pid_alive(int(match["pid"])):
            continue
        try:
            text = log_file.read_text(errors="replace").strip()
            stamp = log_file.stat().st_mtime
        except OSError:
            continue
        if text:
            report = _base("native", match["component"])
            report["time"], report["pid"] = stamp, int(match["pid"])
            lines = text.splitlines()
            report.update(
                {
                    "error": {"type": "FatalSignal", "message": redact_text(lines[0])},
                    "stack": [
                        {"file": "", "line": 0, "function": "", "source": redact_text(line)}
                        for line in lines[:MAX_FRAMES]
                    ],
                    "where": "the process ended in a native crash (faulthandler)",
                    "state": {},
                }
            )
            if _write(report) is None:
                continue  # keep the log; the next start tries again
        with contextlib.suppress(OSError):
            log_file.unlink()


def _remove_native_log() -> None:
    """At a clean exit an empty native log has nothing to say."""
    native = _state["native"]
    if native is None:
        return
    with contextlib.suppress(OSError):
        if Path(native.name).stat().st_size == 0:
            Path(native.name).unlink()


def install(component: str, state: Callable[[], dict[str, Any]] | None = None) -> None:
    """Hook uncaught exceptions and fatal signals for this process. Safe to call twice; never raises."""
    _state["component"] = component
    if state is not None:
        _state["provider"] = state
    if _state["installed"]:
        return
    try:
        directory = crash_dir()
        directory.mkdir(parents=True, exist_ok=True)
        _collect_native(directory)
        native: TextIO = open(directory / f"native-{component}-{os.getpid()}.log", "w")  # noqa: SIM115 - held for life
        _state["native"] = native
        faulthandler.enable(file=native, all_threads=True)
        atexit.register(_remove_native_log)
    except (OSError, ValueError, RuntimeError) as exc:
        log.warning("native crash log not available: %s", exc)
    previous_hook = sys.excepthook

    def excepthook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            report_exception(exc_type, exc, tb, where="uncaught exception in the main thread")
        previous_hook(exc_type, exc, tb)

    sys.excepthook = excepthook
    previous_thread_hook = threading.excepthook

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is not None and not issubclass(args.exc_type, SystemExit) and args.exc_value is not None:
            name = args.thread.name if args.thread else "unknown"
            report_exception(
                args.exc_type, args.exc_value, args.exc_traceback, where=f"uncaught exception in thread {name}"
            )
        previous_thread_hook(args)

    threading.excepthook = thread_hook
    previous_unraisable = sys.unraisablehook

    def unraisable(args: Any) -> None:
        if args.exc_type is not None and args.exc_value is not None:
            report_exception(
                args.exc_type,
                args.exc_value,
                args.exc_traceback,
                kind="unraisable",
                where=redact_text(f"{args.err_msg or 'unraisable exception'}"),
            )
        previous_unraisable(args)

    sys.unraisablehook = unraisable
    _state["previous"] = (previous_hook, previous_thread_hook, previous_unraisable)
    _state["installed"] = True


def uninstall() -> None:
    """Put the previous hooks back and stop faulthandler (for tests and embedding)."""
    previous = _state["previous"]
    if previous is not None:
        sys.excepthook, threading.excepthook, sys.unraisablehook = previous
    with contextlib.suppress(RuntimeError, ValueError):
        faulthandler.disable()
    atexit.unregister(_remove_native_log)
    native = _state["native"]
    if native is not None:
        _remove_native_log()
        with contextlib.suppress(OSError):
            native.close()
    _state.update(component="streamstack", installed=False, native=None, previous=None, provider=None)


# ---------------------------------------------------------------------------
# Reading reports back (the Console)
# ---------------------------------------------------------------------------


def list_reports(limit: int = 10) -> list[dict[str, Any]]:
    """Summaries of the newest reports: id, time, component, kind, error type, message and version."""
    out: list[dict[str, Any]] = []
    for path in sorted(crash_dir().glob("*.json"), reverse=True)[:limit]:
        data = store.read_json(path, None, validate=store.is_object)
        if not isinstance(data, dict):
            continue
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        out.append(
            {
                "id": path.stem,
                "time": data.get("time"),
                "component": data.get("component"),
                "kind": data.get("kind"),
                "type": error.get("type"),
                "message": redact_text(str(error.get("message", ""))),
                "version": data.get("version"),
            }
        )
    return out


REPORT_ID = re.compile(r"[A-Za-z0-9_.-]{1,120}")


def read_report(report_id: str) -> dict[str, Any] | None:
    """One whole report by id (re-redacted on the way out), or None."""
    if not REPORT_ID.fullmatch(report_id) or ".." in report_id:
        return None
    data = store.read_json(crash_dir() / f"{report_id}.json", None, validate=store.is_object)
    return redact(data) if isinstance(data, dict) else None


def clear_reports() -> int:
    """Delete every JSON report; returns how many."""
    count = 0
    for path in crash_dir().glob("*.json"):
        with contextlib.suppress(OSError):
            path.unlink()
            count += 1
    return count


def reset_for_tests() -> None:
    """Forget what `record_exception` rate-limited on, and put the clock back."""
    global _monotonic
    _handled_at.clear()
    _monotonic = time.monotonic
