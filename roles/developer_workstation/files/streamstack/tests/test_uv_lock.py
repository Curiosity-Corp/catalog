"""uv.lock answers for what pyproject.toml asks (issue #165).

python-sbom runs `uv export --frozen --extra <x>` for every PYTHON_SBOM_EXTRAS entry, and --frozen trusts the lock as
it is. Four extras (stills, shortcuts, whep, calendar) reached pyproject.toml without a relock, so main's python-sbom
failed only after merge. uv records what pyproject asked for in the lock's metadata, so the comparison is offline.
Relock with `uv lock` when this fails.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
LOCKED = next(
    p for p in tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"] if p["name"] == PROJECT["name"]
)["metadata"]


def wanted() -> set[tuple[str, str, str | None]]:
    """(name, specifier, extra) for every dependency pyproject.toml declares."""
    out = set()
    groups = {None: PROJECT.get("dependencies", []), **PROJECT.get("optional-dependencies", {})}
    for extra, deps in groups.items():
        for dep in deps:
            req = Requirement(dep)
            out.add((canonicalize_name(req.name), str(req.specifier), extra))
    return out


def locked() -> set[tuple[str, str, str | None]]:
    """The same triples as the lock recorded them."""
    out = set()
    for dep in LOCKED.get("requires-dist", []):
        extra = re.search(r"extra == '([^']+)'", dep.get("marker", ""))
        out.add((canonicalize_name(dep["name"]), dep.get("specifier", ""), extra[1] if extra else None))
    return out


def test_the_lock_knows_every_extra() -> None:
    missing = set(PROJECT.get("optional-dependencies", {})) - set(LOCKED.get("provides-extras", []))
    assert not missing, f"uv.lock predates these extras, run `uv lock`: {sorted(missing)}"


def test_the_lock_was_resolved_from_the_current_dependencies() -> None:
    assert wanted() == locked(), (
        f"run `uv lock`; only in pyproject: {sorted(wanted() - locked(), key=str)}, "
        f"only in uv.lock: {sorted(locked() - wanted(), key=str)}"
    )
