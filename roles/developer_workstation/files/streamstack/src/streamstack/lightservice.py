"""The systemd user unit behind camera follow: where it is, installing it, starting and stopping it.

The unit runs `streamstack lights run`. A `make install` or a package puts `streamstack-lights.service` in the
system's user-unit directory; a pip install has no such step, so `streamstack setup` writes the same unit (from
`data/systemd/`, with the absolute path of this `streamstack`) into `~/.config/systemd/user` when no packaged copy
exists. Nothing here needs root.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess

from streamstack import config, system

UNIT = "streamstack-lights.service"
SYSTEM_UNIT_DIRS = (
    Path("/usr/local/lib/systemd/user"),
    Path("/usr/lib/systemd/user"),
    Path("/lib/systemd/user"),
    Path("/etc/systemd/user"),
)

Runner = Callable[[list[str]], "CompletedProcess[str] | None"]


class ServiceError(RuntimeError):
    """systemd refused, or is not there; the message says what to do."""


def user_unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd/user"


def installed_unit(extra_dirs: tuple[Path, ...] = ()) -> Path | None:
    """The unit file in the user's or the system's unit directories, or None."""
    for directory in (user_unit_dir(), *extra_dirs, *SYSTEM_UNIT_DIRS):
        candidate = directory / UNIT
        if candidate.is_file():
            return candidate
    return None


def streamstack_command() -> str:
    """The absolute command that starts this install of StreamStack (the unit must not rely on PATH)."""
    found = shutil.which("streamstack")
    if found:
        return found
    return f"{sys.executable} -m streamstack"


def render_unit(command: str | None = None) -> str:
    template = (config.data_dir() / "systemd" / UNIT).read_text()
    return template.replace("@STREAMSTACK@", command or streamstack_command())


def install_unit(run: Runner = system.run) -> str:
    """Make sure the unit exists: "packaged", "present", "installed" or "updated" (the command it runs moved).

    Raises ServiceError when the unit file cannot be written.
    """
    existing = installed_unit()
    if existing is not None and existing.parent != user_unit_dir():
        return "packaged"
    target = user_unit_dir() / UNIT
    text = render_unit()
    previous = target.read_text() if target.is_file() else None
    if previous == text:
        return "present"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    except OSError as exc:
        raise ServiceError(f"Cannot write {target}: {exc.strerror or exc}") from exc
    run(["systemctl", "--user", "daemon-reload"])
    return "updated" if previous is not None else "installed"


def is_active(run: Runner = system.run) -> bool:
    result = run(["systemctl", "--user", "is-active", UNIT])
    return bool(result and result.stdout.strip() == "active")


def enable(run: Runner = system.run) -> str:
    """Install the unit if needed, then enable it and start it now."""
    install_unit(run)
    result = run(["systemctl", "--user", "enable", "--now", UNIT])
    if result is None:
        raise ServiceError("systemctl --user did not answer. Is a systemd user session running?")
    if result.returncode != 0:
        raise ServiceError(f"systemctl enable {UNIT} failed: {(result.stderr or result.stdout).strip()}")
    return "started"


def disable(run: Runner = system.run) -> str:
    result = run(["systemctl", "--user", "disable", "--now", UNIT])
    if result is None:
        raise ServiceError("systemctl --user did not answer. Is a systemd user session running?")
    if result.returncode != 0:
        raise ServiceError(f"systemctl disable {UNIT} failed: {(result.stderr or result.stdout).strip()}")
    return "stopped"
