"""Crash-safe files: atomic writes, checked reads, a quarantine for damaged files, and a warning log.

Every file StreamStack keeps (settings, presets, looks, schedules, remembered modes, status and baselines) goes
through here, so a power cut, a full disk or two writers at once can never leave half a file behind, and a file that
is damaged anyway (a hand edit, a bad disk, an older crash) never stops the program.

Writing (`atomic_write_text`, `write_json`, `update_env_file`):

* the new content goes to a temporary file in the same directory, is flushed and fsynced, then renamed over the
  target with `os.replace` and the directory is fsynced, so a reader sees the whole old file or the whole new one;
* the previous good copy stays as `<name>.bak` (a hard link, so it costs nothing) and is never replaced by a copy
  that does not parse;
* any failure (ENOSPC, EIO, a signal in between) removes the temporary file and leaves the target untouched.

Reading (`read_text`, `read_json`):

* a missing file is not a problem: the default is returned and nothing is renamed;
* a file that cannot be decoded or fails its `validate` function is renamed to `<name>.corrupt-<timestamp>` (the
  newest few are kept for diagnosis), the `.bak` copy is restored when it is good, else the default is returned;
* either way a `StoreWarning` is recorded. The Console shows them on the System page and raises a toast.

Nothing in here raises for a damaged file; only `write_*` raises `OSError`, because a caller that failed to save has
to tell the person.
"""

from __future__ import annotations

import contextlib
import copy
import errno
import fcntl
import json
import logging
import os
import re
import shlex
import tempfile
import threading
import time
import zlib
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("streamstack.store")

BACKUP_SUFFIX = ".bak"
CORRUPT_MARK = ".corrupt-"
KEEP_CORRUPT = 5  # quarantined copies kept per file
MAX_WARNINGS = 50
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

Validate = Callable[[Any], Any]


class SchemaError(ValueError):
    """The content parsed but is not the shape the reader needs."""


# ---------------------------------------------------------------------------
# Validators (a validator returns the value to use, or raises ValueError)
# ---------------------------------------------------------------------------


def is_object(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SchemaError(f"expected a JSON object, found {type(data).__name__}")
    return data


def object_with(**types: type | tuple[type, ...]) -> Validate:
    """A validator for an object whose listed keys, when present, have the given types (bool is not a number)."""

    def check(data: Any) -> dict[str, Any]:
        obj = is_object(data)
        for key, wanted in types.items():
            if key not in obj:
                continue
            allowed = wanted if isinstance(wanted, tuple) else (wanted,)
            value = obj[key]
            if isinstance(value, bool) and bool not in allowed:
                raise SchemaError(f"{key} must not be true or false")
            if not isinstance(value, allowed):
                raise SchemaError(f"{key} has the wrong type ({type(value).__name__})")
        return obj

    return check


# ---------------------------------------------------------------------------
# The warning log
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreWarning:
    """One thing the store repaired or could not read, for the Console to show."""

    id: str
    path: str
    problem: str  # what was wrong, one line
    action: str  # "restored-backup", "defaults", "ignored-lines" or "unreadable"
    quarantined_to: str | None
    at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_lock = threading.RLock()
_warnings: list[StoreWarning] = []
_listeners: list[Callable[[StoreWarning], None]] = []


def short_path(path: Path) -> str:
    """The path with the home directory shortened, so the Console never shows more than it must."""
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home) :] if home != "/" and text.startswith(home + "/") else text


def record(path: Path, problem: str, action: str, quarantined_to: Path | None = None) -> StoreWarning:
    """Remember a warning (the same problem on the same file is kept once) and tell the listeners."""
    key = f"{path}|{action}|{problem}"
    ident = format(zlib.crc32(key.encode()), "08x")
    warning = StoreWarning(ident, short_path(path), problem, action, quarantined_to.name if quarantined_to else None)
    with _lock:
        if any(w.id == ident for w in _warnings):
            return next(w for w in _warnings if w.id == ident)
        _warnings.append(warning)
        del _warnings[:-MAX_WARNINGS]
        listeners = list(_listeners)
    log.warning("%s: %s (%s)", warning.path, problem, action)
    for listener in listeners:
        try:
            listener(warning)
        except Exception:
            log.exception("a store warning listener failed")
    return warning


def warnings() -> list[dict[str, Any]]:
    with _lock:
        return [w.as_dict() for w in _warnings]


def dismiss(ident: str | None = None) -> int:
    """Forget one warning by id, or all of them; returns how many were removed."""
    with _lock:
        before = len(_warnings)
        _warnings[:] = [w for w in _warnings if ident is not None and w.id != ident]
        return before - len(_warnings)


def subscribe(listener: Callable[[StoreWarning], None]) -> Callable[[], None]:
    """Call `listener` for each new warning; returns the function that unsubscribes it."""
    with _lock:
        _listeners.append(listener)

    def unsubscribe() -> None:
        with _lock, contextlib.suppress(ValueError):
            _listeners.remove(listener)

    return unsubscribe


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _resolve(path: Path | str) -> Path:
    """Follow a symlink (a config kept in a dotfiles repo) so the link survives and the real file is replaced."""
    target = Path(path)
    return Path(os.path.realpath(target)) if target.is_symlink() else target


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return  # a filesystem that cannot open a directory has nothing to sync either
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF):
            log.warning("could not sync directory %s: %s", directory, exc)
    finally:
        os.close(fd)


def _keep_backup(target: Path, still_good: Callable[[Path], bool] | None) -> None:
    """Hard-link the current file as `<name>.bak` (replacing the older one) when it is a good copy."""
    if not target.is_file() or (still_good is not None and not still_good(target)):
        return
    backup = target.with_name(target.name + BACKUP_SUFFIX)
    link = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.link")
    try:
        link.unlink(missing_ok=True)
        os.link(target, link)
        os.replace(link, backup)
    except OSError as exc:
        # no backup is better than no save: filesystems without hard links simply go without
        log.debug("no backup of %s: %s", target, exc)
    finally:
        # rename(2) does nothing when both names already link to one file, which leaves `link` behind
        link.unlink(missing_ok=True)


def atomic_write_bytes(
    path: Path | str,
    data: bytes,
    *,
    mode: int | None = None,
    backup: bool = False,
    still_good: Callable[[Path], bool] | None = None,
    durable: bool = True,
) -> Path:
    """Replace `path` with `data` so that a crash leaves the old content or the new, never a mix.

    `mode` is the permission of a new file (default: the existing file's, else 0o644). `backup` keeps the previous
    content as `<name>.bak` when `still_good(path)` (default: always) says it was a good copy. `durable=False` skips the
    fsyncs for files that are rewritten every second and worthless after a reboot (status heartbeats): they stay
    atomic, so a reader still never sees half a file. Raises OSError.
    """
    target = _resolve(path)
    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = target.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o644
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            if durable:
                os.fsync(handle.fileno())
        if backup:
            _keep_backup(target, still_good)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if durable:
        _fsync_dir(directory)
    return target


def atomic_write_text(path: Path | str, text: str, **options: Any) -> Path:
    return atomic_write_bytes(path, text.encode("utf-8"), **options)


def _parses(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return True


def write_json(
    path: Path | str,
    data: Any,
    *,
    mode: int | None = None,
    indent: int | None = 2,
    sort_keys: bool = True,
    backup: bool = True,
    durable: bool = True,
) -> Path:
    """Atomically write `data` as JSON (serialised first, so an unserialisable value touches nothing)."""
    text = json.dumps(data, indent=indent, sort_keys=sort_keys, separators=(",", ":") if indent is None else None)
    return atomic_write_text(
        path, text + "\n", mode=mode, backup=backup, still_good=_parses if backup else None, durable=durable
    )


@contextlib.contextmanager
def locked(path: Path | str) -> Iterator[None]:
    """Hold an exclusive lock for `path` across processes and threads (a sidecar `.<name>.lock` file)."""
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target.parent / f".{target.name}.lock", "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def quarantine(path: Path) -> Path | None:
    """Move a damaged file aside as `<name>.corrupt-<timestamp>`; None when it cannot be moved."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    target = path.with_name(f"{path.name}{CORRUPT_MARK}{stamp}")
    counter = 0
    while target.exists():
        counter += 1
        target = path.with_name(f"{path.name}{CORRUPT_MARK}{stamp}-{counter}")
    try:
        os.replace(path, target)
    except OSError as exc:
        log.warning("cannot move the damaged %s aside: %s", path, exc)
        return None
    old = sorted(path.parent.glob(f"{path.name}{CORRUPT_MARK}*"))
    for stale in old[:-KEEP_CORRUPT]:
        stale.unlink(missing_ok=True)
    return target


def _load(path: Path, validate: Validate | None, decode: Callable[[str], Any]) -> Any:
    """The decoded and validated content of `path`; raises ValueError for damage, OSError when unreadable."""
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise SchemaError("the file contains NUL bytes")
    value = decode(raw.decode("utf-8"))
    return validate(value) if validate else value


def _recover(
    path: Path,
    problem: str,
    validate: Validate | None,
    decode: Callable[[str], Any],
    default: Any,
) -> Any:
    moved = quarantine(path)
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if backup.is_file():
        try:
            value = _load(backup, validate, decode)
        except (OSError, ValueError):
            pass
        else:
            with contextlib.suppress(OSError):
                atomic_write_bytes(path, backup.read_bytes(), backup=False)
            record(path, problem, "restored-backup", moved)
            return value
    record(path, problem, "defaults", moved)
    return copy.deepcopy(default)


def _read(path: Path | str, default: Any, validate: Validate | None, decode: Callable[[str], Any]) -> Any:
    target = Path(path)
    try:
        return _load(target, validate, decode)
    except FileNotFoundError:
        return copy.deepcopy(default)
    except IsADirectoryError:
        record(target, "is a directory, not a file", "unreadable")
        return copy.deepcopy(default)
    except OSError as exc:
        record(target, f"cannot be read: {exc.strerror or exc}", "unreadable")
        return copy.deepcopy(default)
    except ValueError as exc:  # JSON errors, bad UTF-8 and failed validation are all ValueErrors
        problem = f"is damaged ({type(exc).__name__}: {str(exc)[:120]})"
        return _recover(_resolve(target), problem, validate, decode, default)


def _decode_json(text: str) -> Any:
    if not text.strip():
        raise SchemaError("the file is empty")
    return json.loads(text)


def read_json(path: Path | str, default: Any = None, *, validate: Validate | None = None) -> Any:
    """The parsed content of `path`, or a copy of `default` when absent or damaged (see the module notes)."""
    return _read(path, default, validate, _decode_json)


def read_text(path: Path | str, default: str = "", *, validate: Validate | None = None) -> str:
    """Like `read_json` for a text file: empty, NUL-holding or non-UTF-8 content counts as damage."""
    return _read(path, default, validate, lambda text: text)


# ---------------------------------------------------------------------------
# KEY=VALUE settings files
# ---------------------------------------------------------------------------


def update_env_file(path: Path | str, updates: dict[str, str | None], *, mode: int | None = None) -> None:
    """Set (or, with None, remove) keys in a KEY=VALUE file, keeping every other line and comment as it is.

    Locked, atomic, and keeps `<name>.bak`. Raises ValueError for a bad key and OSError when the file cannot be saved.
    """
    for key, value in updates.items():
        if not ENV_KEY.fullmatch(key):
            raise ValueError(f"{key!r} is not a valid setting name")
        if value is not None and any(ch in value for ch in "\n\r\x00"):
            raise ValueError(f"the value of {key} must be one line")
    target = _resolve(path)
    with locked(target):
        lines = read_text(target, "").splitlines()
        pending = dict(updates)
        out: list[str] = []
        for line in lines:
            name = line.strip().removeprefix("export ").partition("=")[0].strip()
            if "=" in line and not line.lstrip().startswith("#") and name in pending:
                value = pending.pop(name)
                if value is not None:
                    out.append(f"{name}={shlex.quote(value)}")
                continue
            out.append(line)
        out.extend(f"{key}={shlex.quote(value)}" for key, value in pending.items() if value is not None)
        text = "\n".join(out) + "\n" if out else ""
        atomic_write_text(target, text, mode=mode, backup=True, still_good=lambda p: bool(p.read_bytes().strip()))
