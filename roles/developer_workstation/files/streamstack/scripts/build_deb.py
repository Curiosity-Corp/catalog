#!/usr/bin/env python3
"""Build the StreamStack .deb from the tree (a developer/CI tool; it is not part of the wheel).

    scripts/build_deb.py [--out DIR]     writes DIR/streamstack_<version>_all.deb and DIR/DEB-SHA256SUMS

The package is exactly what ``make install PREFIX=/usr`` lays down (the package under /usr/lib/streamstack, the four
launchers, the user units, udev rules, modprobe/modules-load drop-ins, the WirePlumber drop-in, desktop files and
icons), so a .deb install and a ``make install`` are the same tree. The build is reproducible: file times are pinned to
SOURCE_DATE_EPOCH (default: the last commit's time), owners are root, and compression is fixed, so two builds of one
commit are byte-identical. CI builds it (docs/release.md); nothing is built or signed on a workstation.

Standard library only. Needs ``make`` and ``dpkg-deb`` (any Debian-based CI image has both).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT_FILE = Path("src/streamstack/__init__.py")
PACKAGE = "streamstack"
ARCH = "all"
MAINTAINER = "StreamStack Release <streamstack-release@curiosity-corp.invalid>"
DEPENDS = (
    "python3 (>= 3.11)",
    "python3-gi",
    "ffmpeg",
    "v4l-utils",
    "pulseaudio-utils",
    "alsa-utils",
)
RECOMMENDS = (
    "pipewire",
    "zam-plugins",
    "v4l2loopback-dkms",
    "gir1.2-ayatanaappindicator3-0.1",
    "libnotify-bin",
)
SUGGESTS = ("gphoto2", "qpwgraph", "pavucontrol")
DESCRIPTION = (
    "Camera, lighting and voice for Teams, Zoom and streams on Linux\n"
    " StreamStack drives a Canon or UVC camera, Litra lights and a studio microphone chain, and exposes them to\n"
    " call applications as loopback devices, with a local Console and an optional auto-cinematographer.\n"
    " It is standard-library Python; the media tools come from the distribution."
)
POSTINST = """#!/bin/sh
set -e
# udev rules for the Litra lights and the loopback cameras
if [ "$1" = "configure" ] && command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules 2>/dev/null || true
fi
exit 0
"""
POSTRM = """#!/bin/sh
set -e
if { [ "$1" = "remove" ] || [ "$1" = "purge" ]; } && command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules 2>/dev/null || true
fi
exit 0
"""
VERSION_LINE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
DEB_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class DebError(Exception):
    """The package could not be built; the message says why."""


def read_version(root: Path) -> str:
    match = VERSION_LINE.search((root / INIT_FILE).read_text(encoding="utf-8"))
    if not match or not DEB_VERSION.match(match.group(1)):
        raise DebError(f"no MAJOR.MINOR.PATCH __version__ in {INIT_FILE}")
    return match.group(1)


def source_date_epoch(root: Path) -> int:
    """SOURCE_DATE_EPOCH when set, else the last commit's time (the same commit always gives the same value)."""
    given = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if given:
        if not given.isdigit():
            raise DebError(f"SOURCE_DATE_EPOCH={given!r} is not a number of seconds")
        return int(given)
    done = subprocess.run(
        ["git", "-C", str(root), "log", "-1", "--format=%ct"], capture_output=True, text=True, check=False
    )
    if done.returncode != 0 or not done.stdout.strip().isdigit():
        raise DebError("cannot read the commit time; set SOURCE_DATE_EPOCH")
    return int(done.stdout.strip())


def control_text(version: str, installed_kib: int) -> str:
    return (
        f"Package: {PACKAGE}\n"
        f"Version: {version}\n"
        "Section: video\n"
        "Priority: optional\n"
        f"Architecture: {ARCH}\n"
        f"Maintainer: {MAINTAINER}\n"
        f"Installed-Size: {installed_kib}\n"
        f"Depends: {', '.join(DEPENDS)}\n"
        f"Recommends: {', '.join(RECOMMENDS)}\n"
        f"Suggests: {', '.join(SUGGESTS)}\n"
        f"Description: {DESCRIPTION}\n"
    )


def _run(argv: Sequence[str], env: dict[str, str] | None = None) -> None:
    done = subprocess.run(list(argv), capture_output=True, text=True, check=False, env=env)
    if done.returncode != 0:
        raise DebError(f"{' '.join(argv[:3])} failed: {(done.stderr or done.stdout).strip()}")


def stage_tree(root: Path, stage: Path, epoch: int) -> None:
    """`make install PREFIX=/usr` into ``stage``, then normalise modes and times for a reproducible archive."""
    for tool in ("make", "dpkg-deb"):
        if shutil.which(tool) is None:
            raise DebError(f"{tool} is required to build the package")
    _run(["make", "-C", str(root), "install", f"DESTDIR={stage}", "PREFIX=/usr"])
    for path in sorted(stage.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o755)
        else:
            path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
        os.utime(path, (epoch, epoch))
    stage.chmod(0o755)
    os.utime(stage, (epoch, epoch))


def write_control_files(stage: Path, version: str, epoch: int) -> None:
    files = sorted(p for p in stage.rglob("*") if p.is_file() and not p.is_symlink())
    installed_kib = sum(p.stat().st_size for p in files) // 1024 + 1
    debian = stage / "DEBIAN"
    debian.mkdir()
    (debian / "control").write_text(control_text(version, installed_kib), encoding="utf-8")
    sums = "".join(
        f"{hashlib.md5(p.read_bytes(), usedforsecurity=False).hexdigest()}  {p.relative_to(stage)}\n" for p in files
    )
    (debian / "md5sums").write_text(sums, encoding="utf-8")
    conffiles = [f"/{p.relative_to(stage)}" for p in files if p.relative_to(stage).parts[0] == "etc"]
    if conffiles:
        (debian / "conffiles").write_text("\n".join(conffiles) + "\n", encoding="utf-8")
    for name, text in (("postinst", POSTINST), ("postrm", POSTRM)):
        (debian / name).write_text(text, encoding="utf-8")
        (debian / name).chmod(0o755)
    for path in [debian, *debian.iterdir()]:
        os.utime(path, (epoch, epoch))


def build(root: Path, out_dir: Path) -> Path:
    version = read_version(root)
    epoch = source_date_epoch(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    deb = out_dir / f"{PACKAGE}_{version}_{ARCH}.deb"
    with tempfile.TemporaryDirectory(prefix="streamstack-deb-") as tmp:
        stage = Path(tmp) / "pkg"
        stage.mkdir()
        stage_tree(root, stage, epoch)
        write_control_files(stage, version, epoch)
        env = dict(os.environ, SOURCE_DATE_EPOCH=str(epoch))
        _run(
            ["dpkg-deb", "--root-owner-group", "-Zxz", "-z6", "--uniform-compression", "--build", str(stage), str(deb)],
            env=env,
        )
    digest = hashlib.sha256(deb.read_bytes()).hexdigest()
    (out_dir / "DEB-SHA256SUMS").write_text(f"{digest}  {deb.name}\n", encoding="utf-8")
    return deb


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_deb.py", description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    parser.add_argument("--out", default="deb", help="output directory (default: deb)")
    args = parser.parse_args(argv)
    try:
        deb = build(Path(args.root), Path(args.out))
    except (DebError, OSError) as exc:
        print(f"build_deb.py: {exc}", file=sys.stderr)
        return 1
    print(f"built {deb} ({deb.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
