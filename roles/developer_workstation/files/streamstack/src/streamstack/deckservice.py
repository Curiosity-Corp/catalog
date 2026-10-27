"""The systemd user unit behind the Stream Deck: where it is, installing it, starting and stopping it.

The unit runs `streamstack deck run`. It is installed the same way as the lights unit (see lightservice.py): a packaged
`streamstack-deck.service` wins, otherwise `streamstack deck enable` writes the one in `data/systemd/` into
`~/.config/systemd/user`. Nothing here needs root.
"""

from __future__ import annotations

from pathlib import Path

from streamstack import config, system
from streamstack.lightservice import SYSTEM_UNIT_DIRS, Runner, ServiceError, streamstack_command, user_unit_dir

UNIT = "streamstack-deck.service"

__all__ = ["UNIT", "ServiceError", "disable", "enable", "install_unit", "installed_unit", "is_active", "render_unit"]


def installed_unit(extra_dirs: tuple[Path, ...] = ()) -> Path | None:
    for directory in (user_unit_dir(), *extra_dirs, *SYSTEM_UNIT_DIRS):
        candidate = directory / UNIT
        if candidate.is_file():
            return candidate
    return None


def render_unit(command: str | None = None) -> str:
    template = (config.data_dir() / "systemd" / UNIT).read_text()
    return template.replace("@STREAMSTACK@", command or streamstack_command())


def install_unit(run: Runner = system.run) -> str:
    """Ensure the unit exists: "packaged", "present", "installed" or "updated". Raises ServiceError on a write error."""
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


def _systemctl(run: Runner, *args: str) -> None:
    result = run(["systemctl", "--user", *args, UNIT])
    if result is None:
        raise ServiceError("systemctl --user did not answer. Is a systemd user session running?")
    if result.returncode != 0:
        raise ServiceError(f"systemctl {args[0]} {UNIT} failed: {(result.stderr or result.stdout).strip()}")


def enable(run: Runner = system.run) -> str:
    """Install the unit if needed, then enable it and start it now."""
    install_unit(run)
    _systemctl(run, "enable", "--now")
    return "started"


def disable(run: Runner = system.run) -> str:
    _systemctl(run, "disable", "--now")
    return "stopped"
