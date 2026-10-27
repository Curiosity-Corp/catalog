"""Regenerate the shipped preset assets: the .cube LUTs and the look thumbnails.

    python -m streamstack.presets.build            # write them into the package data
    python -m streamstack.presets.build --check    # exit 1 when the shipped files are out of date

Run it after changing ``luts.py``, ``thumbs.py`` or ``data/presets/looks.json``; the unit tests run the check.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from streamstack.presets import catalog
from streamstack.presets.luts import LUTS, Lut3D, build_cube
from streamstack.presets.schema import LOOK_NEUTRAL
from streamstack.presets.thumbs import HEIGHT, WIDTH, decode_png, png, render

THUMB_TOLERANCE = 2  # per channel; the scene uses sin and pow, which may differ in the last bit between platforms


def luts_dir() -> Path:
    return catalog.presets_dir() / "luts"


def expected_cubes() -> dict[str, str]:
    return {f"{name}.cube": build_cube(name) for name in LUTS}


def expected_thumbs() -> dict[str, bytes]:
    """Thumbnail file name -> PNG bytes, rendered from the generated LUTs (not the files on disk)."""
    cache: dict[str, Lut3D] = {}
    out: dict[str, bytes] = {}
    for look in catalog.load().looks:
        lut = None
        if look["lut"]:
            lut = cache.setdefault(look["lut"], Lut3D.parse(build_cube(look["lut"])))
        values = {**LOOK_NEUTRAL, **look["values"]}
        out[look["thumb"].rsplit("/", 1)[1]] = png(render(values, lut))
    return out


def problems() -> list[str]:
    """What is wrong with the shipped files, one line each (empty when they are current)."""
    found: list[str] = []
    cubes, thumbs = expected_cubes(), expected_thumbs()
    for name, text in cubes.items():
        path = luts_dir() / name
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            found.append(f"stale or missing LUT {path}")
    for path in sorted(luts_dir().glob("*.cube")):
        if path.name not in cubes:
            found.append(f"orphan LUT {path}")
    for name, data in thumbs.items():
        path = catalog.thumbs_dir() / name
        try:
            width, height, pixels = decode_png(path.read_bytes())
        except (OSError, ValueError, KeyError):
            found.append(f"missing or unreadable thumbnail {path}")
            continue
        _, _, wanted = decode_png(data)
        if (width, height) != (WIDTH, HEIGHT) or any(
            abs(a - b) > THUMB_TOLERANCE for a, b in zip(pixels, wanted, strict=True)
        ):
            found.append(f"stale thumbnail {path}")
    for path in sorted(catalog.thumbs_dir().glob("*.png")):
        if path.name not in thumbs:
            found.append(f"orphan thumbnail {path}")
    return found


def write() -> None:
    luts_dir().mkdir(parents=True, exist_ok=True)
    catalog.thumbs_dir().mkdir(parents=True, exist_ok=True)
    cubes, thumbs = expected_cubes(), expected_thumbs()
    for directory, keep, pattern in ((luts_dir(), cubes, "*.cube"), (catalog.thumbs_dir(), thumbs, "*.png")):
        for path in directory.glob(pattern):
            if path.name not in keep:
                path.unlink()
    for name, text in cubes.items():
        (luts_dir() / name).write_text(text, encoding="utf-8")
    for name, data in thumbs.items():
        (catalog.thumbs_dir() / name).write_bytes(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m streamstack.presets.build", description=__doc__)
    parser.add_argument("--check", action="store_true", help="only check that the shipped files are current")
    args = parser.parse_args(argv)
    if args.check:
        found = problems()
        for line in found:
            print(line, file=sys.stderr)
        return 1 if found else 0
    write()
    print(f"wrote {len(LUTS)} LUTs and {len(catalog.load().looks)} thumbnails")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
