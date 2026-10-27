"""The virtual cameras per host profile: what the rig's profile wants, what is there, and how to close the gap.

The rig profile (`[rig.loopback]` in rigs.toml) declares the v4l2loopback devices: role, `video_nr`, `card_label`,
`exclusive_caps`, and the shared `max_buffers`. From it this module

* writes the modprobe.d options line and the modules-load.d file (the same text the package ships; a test pins it),
* reports per role whether the node exists under the right card label, found by label and never by number,
* creates a missing node on a loaded module with `v4l2loopback-ctl add`, never touching a node that exists (a node
  that is in use, or named wrongly, is reported with what to do; reloading the module needs its users stopped).

Nothing here reboots, unloads a module or changes a node that exists. `apply` is idempotent: a second run finds the
files identical and the nodes present and does nothing.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from streamstack import system
from streamstack.hardware.plan import Plan
from streamstack.hardware.schema import LoopbackSpec, RigProfile

MODPROBE = Path("etc/modprobe.d/streamstack-v4l2loopback.conf")
MODULES_LOAD = Path("etc/modules-load.d/streamstack-v4l2loopback.conf")
HEADER = (
    "# StreamStack virtual cameras (docs/adr/0004-virtual-camera-and-mic.md), written from the hardware profile.\n"
    "# Regenerate with `streamstack loopback apply`; the devices and labels come from the rig profile (ADR 0003).\n"
)

Runner = Callable[[list[str]], tuple[int, str]]


class LoopbackError(Exception):
    """The virtual cameras cannot be set up as asked; the message says what to do."""


@dataclass(frozen=True)
class RoleState:
    role: str
    label: str
    video_nr: int
    path: str | None  # the node found by label, or None
    state: str  # "ok" | "missing" | "mislabelled" (the declared number exists under another name)
    detail: str = ""


@dataclass(frozen=True)
class Status:
    spec: LoopbackSpec
    module_loaded: bool
    options_match: bool | None
    roles: tuple[RoleState, ...]

    @property
    def ok(self) -> bool:
        return all(r.state == "ok" for r in self.roles) and self.options_match is not False

    def lines(self) -> list[str]:
        out = [f"v4l2loopback module: {'loaded' if self.module_loaded else 'not loaded'}"]
        if self.options_match is not None:
            out.append(f"modprobe options: {'match the profile' if self.options_match else 'differ from the profile'}")
        out.append(f"wanted options: {self.spec.modprobe_options()}")
        for r in self.roles:
            where = r.path or f"/dev/video{r.video_nr} (declared)"
            out.append(f"{r.role}: {r.state}  {r.label!r} at {where}" + (f"  {r.detail}" if r.detail else ""))
        return out


def spec_of(rig: RigProfile) -> LoopbackSpec:
    if rig.loopback is None:
        raise LoopbackError(f"The rig profile {rig.id} declares no virtual cameras ([rig.loopback]).")
    return rig.loopback


def modprobe_text(spec: LoopbackSpec) -> str:
    return f"{HEADER}options v4l2loopback {spec.modprobe_options()}\n"


def modules_load_text() -> str:
    return "v4l2loopback\n"


def status(plan: Plan, rig: RigProfile, video_labels: dict[int, str] | None = None) -> Status:
    """Compare the profile's devices with the Plan's view of the machine. `video_labels` ({number: card label}) lets
    a caller say what the declared numbers are called now, to tell "missing" from "mislabelled"."""
    spec = spec_of(rig)
    found = {"camera": plan.loopback.camera_path, "program": plan.loopback.program_path}
    states: list[RoleState] = []
    for dev in spec.devices:
        path = found.get(dev.role)
        if path is not None:
            states.append(RoleState(dev.role, dev.card_label, dev.video_nr, path, "ok"))
            continue
        other = (video_labels or {}).get(dev.video_nr)
        if other is not None:
            states.append(
                RoleState(
                    dev.role,
                    dev.card_label,
                    dev.video_nr,
                    None,
                    "mislabelled",
                    f"/dev/video{dev.video_nr} is called {other!r}; reload the module with the profile's options",
                )
            )
        else:
            states.append(RoleState(dev.role, dev.card_label, dev.video_nr, None, "missing"))
    return Status(spec, plan.loopback.module_loaded, plan.loopback.options_match, tuple(states))


def _write_if_changed(path: Path, text: str) -> bool:
    """Write atomically unless the file already has exactly this text. True when it wrote."""
    try:
        if path.read_text() == text:
            return False
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise LoopbackError(f"cannot read {path}: {exc.strerror or exc}") from exc
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".streamstack-")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(text)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise
    except PermissionError as exc:
        raise LoopbackError(f"no permission to write {path}; run as root (or use --root with a directory)") from exc
    except OSError as exc:
        raise LoopbackError(f"cannot write {path}: {exc.strerror or exc}") from exc
    return True


def _default_run(argv: list[str]) -> tuple[int, str]:
    result = system.run(argv, timeout=20)
    if result is None:
        return 127, f"{argv[0]} could not be run"
    return result.returncode, (result.stderr or result.stdout or "").strip()


def apply(
    plan: Plan,
    rig: RigProfile,
    *,
    root: Path = Path("/"),
    dry_run: bool = False,
    run: Runner = _default_run,
    which: Callable[[str], str | None] = shutil.which,
    create_nodes: bool = True,
) -> list[str]:
    """Bring the files (and, on a loaded module, the missing nodes) in line with the rig profile. Returns what it
    did, or with `dry_run` what it would do. Never changes a node that exists."""
    spec = spec_of(rig)
    done: list[str] = []
    for rel, text in ((MODPROBE, modprobe_text(spec)), (MODULES_LOAD, modules_load_text())):
        path = root / rel
        try:
            same = path.read_text() == text
        except FileNotFoundError:
            same = False
        except OSError as exc:
            raise LoopbackError(f"cannot read {path}: {exc.strerror or exc}") from exc
        if same:
            continue
        if not dry_run:
            _write_if_changed(path, text)
        done.append(f"{'would write' if dry_run else 'wrote'} {path}")
    current = status(plan, rig)
    if create_nodes and current.module_loaded:
        for state in current.roles:
            if state.state != "missing":
                continue
            dev = spec.device(state.role)
            if dev is None:
                continue
            target = f"/dev/video{dev.video_nr}"
            if (root / target.lstrip("/")).exists() and root == Path("/"):
                done.append(f"left {target} alone: it exists under another name")
                continue
            if which("v4l2loopback-ctl") is None:
                done.append(f"cannot create {target}: v4l2loopback-ctl is not installed (reload the module instead)")
                continue
            argv = [
                "v4l2loopback-ctl",
                "add",
                "-n",
                dev.card_label,
                "-x",
                "1" if dev.exclusive_caps else "0",
                target,
            ]
            if dry_run:
                done.append(f"would run {' '.join(argv)}")
                continue
            code, output = run(argv)
            if code != 0:
                raise LoopbackError(f"{' '.join(argv)} failed: {output or f'exit {code}'}")
            done.append(f"created {target} as {dev.card_label!r}")
    if not done:
        done.append("nothing to do: the files and the nodes already match the profile")
    return done
