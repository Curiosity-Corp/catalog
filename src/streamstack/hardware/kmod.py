"""The v4l2loopback kernel module across kernel updates (#86): is it there for the kernel we run, and for the next one?

StreamStack Camera is a v4l2loopback node (ADR 0004). The module is not part of the kernel: Debian and Ubuntu build
it per kernel (DKMS, or a prebuilt `linux-modules-extra` package that carries it under `ubuntu/dkms/`). After a kernel
update that did not build it, the next boot has **no module, so no /dev/video42 or /dev/video43, so no camera in any
call app**, and nothing says why. This module answers, read-only and from files (no `modprobe`, no loading):

* `kernel-module`   does the running kernel have a v4l2loopback module? (`modules.dep` is what `modprobe` reads)
* `kernel-pending`  does every *newer* installed kernel have one, so the next reboot keeps the camera?
* `kernel-version`  is the loaded module the one on disk, and is it at least the version with the known fixes?

Every row that is not ok names the fix. The packaged kernel hook (`packaging/kernel/`) runs the same logic after every
kernel install, so the module is built before the reboot, not discovered after it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from streamstack import system

MODULES = Path("/lib/modules")
LOADED_VERSION = Path("/sys/module/v4l2loopback/version")
# 0.15.4 carries two concurrency fixes in the frame copy (upstream release notes, checked 2026-10-27).
RECOMMENDED = (0, 15, 4)
# Ubuntu 26.04 ships 0.15.3 (kernel prebuilt and the archive's v4l2loopback-dkms), so nothing newer can be installed
# from the distro: below 0.15.3 is a warning with a fix, 0.15.3 is a note that says what 0.15.4 would change.
MINIMUM = (0, 15, 3)
GROUP = "video"


def release_key(release: str) -> tuple[int, ...]:
    """Sort key for a kernel release such as 7.0.0-38-generic: its numbers, so 7.0.0-100 sorts after 7.0.0-38."""
    return tuple(int(n) for n in re.findall(r"\d+", release.split("-generic")[0].split("+")[0]))


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.match(r"\s*v?(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(g) for g in match.groups() if g is not None) if match else None


def installed_kernels(root: Path = MODULES) -> list[str]:
    """Kernel releases that have a module tree and could be booted, oldest first."""
    try:
        names = [p.name for p in root.iterdir() if (p / "modules.dep").is_file() or (p / "kernel").is_dir()]
    except OSError:
        return []
    return sorted(names, key=release_key)


def has_module(release: str, root: Path = MODULES) -> bool | None:
    """True when `modules.dep` of `release` lists v4l2loopback; None when it cannot be read (tree missing)."""
    try:
        text = (root / release / "modules.dep").read_text(errors="replace")
    except OSError:
        return None
    return any(line.split(":", 1)[0].rsplit("/", 1)[-1].startswith("v4l2loopback.ko") for line in text.splitlines())


def has_headers(release: str, root: Path = MODULES) -> bool:
    return (root / release / "build" / "Makefile").exists() or (root / release / "build").is_symlink()


@dataclass(frozen=True)
class DkmsEntry:
    version: str
    release: str
    state: str


def parse_dkms_status(text: str) -> list[DkmsEntry]:
    """`dkms status` lines: "v4l2loopback/0.15.3, 7.0.0-38-generic, x86_64: installed" (or the older comma form)."""
    out = []
    for line in text.splitlines():
        match = re.match(
            r"\s*v4l2loopback[/,]\s*([\w.~+-]+),\s*([\w.+~-]+),\s*[\w_]+\s*(?:\([^)]*\))?:\s*(\w[\w -]*)", line
        )
        if match:
            out.append(DkmsEntry(match.group(1), match.group(2), match.group(3).strip()))
    return out


@dataclass
class KernelFacts:
    running: str
    kernels: list[str]
    modules: dict[str, bool | None]  # release -> has a v4l2loopback module
    headers: dict[str, bool]
    loaded: str | None  # version of the loaded module, None when it is not loaded
    dkms: list[DkmsEntry] = field(default_factory=list)
    dkms_present: bool = False


def gather(
    *,
    root: Path = MODULES,
    running: str | None = None,
    loaded_file: Path = LOADED_VERSION,
    run: Callable[[list[str]], str | None] | None = None,
) -> KernelFacts:
    running = running or os.uname().release
    kernels = installed_kernels(root)
    if running not in kernels:
        kernels.append(running)
    try:
        loaded = loaded_file.read_text().strip() or None
    except OSError:
        loaded = None

    def default_run(argv: list[str]) -> str | None:
        result = system.run(argv, timeout=15)
        return result.stdout if result is not None and result.returncode == 0 else None

    status = (run or default_run)(["dkms", "status"])
    return KernelFacts(
        running=running,
        kernels=kernels,
        modules={k: has_module(k, root) for k in kernels},
        headers={k: has_headers(k, root) for k in kernels},
        loaded=loaded,
        dkms=parse_dkms_status(status or ""),
        dkms_present=status is not None,
    )


def build_fix(release: str, facts: KernelFacts) -> str:
    if not facts.headers.get(release, False):
        return (
            f"sudo apt install linux-headers-{release} v4l2loopback-dkms (or linux-modules-extra-{release}), "
            f"then sudo dkms autoinstall -k {release}"
        )
    return f"sudo dkms autoinstall -k {release} (package v4l2loopback-dkms)"


def _row(state: str, cid: str, title: str, detail: str = "", hint: str = "") -> dict[str, str]:
    return {"id": f"v4l2loopback-{cid}", "group": GROUP, "state": state, "title": title, "detail": detail, "hint": hint}


def checks(facts: KernelFacts) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    run_has = facts.modules.get(facts.running)
    if run_has is False:
        rows.append(
            _row(
                "fail",
                "kernel",
                f"kernel {facts.running} has no v4l2loopback module, so StreamStack Camera cannot exist",
                "a kernel update installed without rebuilding the module (DKMS)",
                build_fix(facts.running, facts) + "; then sudo modprobe v4l2loopback (docs/camera-delivery.md)",
            )
        )
    elif run_has:
        rows.append(_row("ok", "kernel", f"kernel {facts.running} has a v4l2loopback module"))
    newer = [k for k in facts.kernels if release_key(k) > release_key(facts.running)]
    missing = [k for k in newer if facts.modules.get(k) is False]
    if missing:
        rows.append(
            _row(
                "warn",
                "pending",
                f"after the next reboot into {missing[-1]} StreamStack Camera will not exist: its kernel has no "
                "v4l2loopback module",
                "the newer kernel is installed but DKMS did not build the module for it",
                build_fix(missing[-1], facts),
            )
        )
    elif newer:
        rows.append(_row("ok", "pending", f"the newer kernel {newer[-1]} has the module, so a reboot keeps the camera"))
    rows.extend(_version_rows(facts))
    return rows


def _version_rows(facts: KernelFacts) -> list[dict[str, str]]:
    if facts.loaded is None:
        return []
    have = parse_version(facts.loaded)
    rows = []
    if have is not None and have < RECOMMENDED:
        want = ".".join(map(str, RECOMMENDED))
        if have < MINIMUM:
            rows.append(
                _row(
                    "warn",
                    "version",
                    f"v4l2loopback {facts.loaded} is loaded; {want} fixes two concurrency bugs in the frame copy",
                    "a camera that freezes or tears under load can be this bug",
                    f"update v4l2loopback-dkms to {want} or later, then reload the module when no call is running",
                )
            )
        else:
            rows.append(
                _row(
                    "ok",
                    "version",
                    f"v4l2loopback {facts.loaded} is loaded ({want} upstream fixes two frame-copy concurrency bugs)",
                    "the distribution ships 0.15.3; if a camera freezes or tears under load, building upstream "
                    f"{want} with DKMS is the first thing to try",
                )
            )
    on_disk = [
        e
        for e in facts.dkms
        if e.release == facts.running and e.state.startswith("installed") and parse_version(e.version)
    ]
    if on_disk and have is not None:
        built = parse_version(on_disk[-1].version)
        if built is not None and built > have:
            rows.append(
                _row(
                    "warn",
                    "reload",
                    f"v4l2loopback {on_disk[-1].version} is installed but {facts.loaded} is loaded",
                    "the module was updated on disk while a call app may still hold the node",
                    "reload it when no call is running: stop the Console, sudo modprobe -r v4l2loopback && "
                    "sudo modprobe v4l2loopback, start the Console",
                )
            )
    if not rows:
        rows.append(_row("ok", "version", f"v4l2loopback {facts.loaded} is loaded"))
    return rows


def guard_checks(**kwargs) -> list[dict[str, str]]:
    """The rows for this host. Never raises: a probe that cannot run returns nothing."""
    try:
        return checks(gather(**kwargs))
    except OSError:
        return []
