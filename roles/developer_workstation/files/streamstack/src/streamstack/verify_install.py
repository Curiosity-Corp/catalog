"""`streamstack verify-install`: is this installation whole, and how do I prove it is the signed release?

The checks cover what a broken wheel or a half-copied tree loses: the version the code reports against the
version pip recorded, the files the package ships as data (the Console frontend, templates, icons, voice
presets), and the four commands the wheel installs. Signature verification needs the release files from the
project's package registry, so it is printed as instructions (docs/release.md has the long form).
"""

from __future__ import annotations

import importlib
import importlib.metadata as metadata
import importlib.util
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from streamstack import __version__, registry
from streamstack.config import data_dir

DIST_NAME = "streamstack"
GENERIC_PACKAGE = "streamstack-release"

#: console command -> "module:function" it must resolve to
EXPECTED_ENTRY_POINTS = {
    "streamstack": "streamstack.cli:main",
    "streamstack-auto": "streamstack.auto:main",
    "streamstack-console": "streamstack.console.launcher:main",
    "streamstack-tray": "streamstack.tray:main",
}

#: files under data_dir() that every install must carry
REQUIRED_DATA = (
    "console/index.html",
    "console/app.js",
    "console/theme.css",
    "console/shell.css",
    "console/pages/index.js",
    "console/components/index.js",
    "icons/streamstack-live.svg",
    "icons/streamstack-raw.svg",
    "icons/streamstack-warn.svg",
    "icons/streamstack-off.svg",
)

#: directories under data_dir() that must not be empty
REQUIRED_DIRS = ("audio/presets", "personas", "console/demo")

OK, WARN, FAIL = "ok", "warn", "FAIL"


@dataclass(frozen=True)
class Result:
    state: str
    message: str
    hint: str = ""


def installed_version() -> str | None:
    """The version pip recorded for the distribution, or None when it is not pip-installed."""
    try:
        return metadata.version(DIST_NAME)
    except metadata.PackageNotFoundError:
        return None


def install_kind() -> str:
    """How pip got it: 'wheel', 'editable', 'index' or 'unknown' (from PEP 610 direct_url.json)."""
    try:
        text = metadata.distribution(DIST_NAME).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return "unknown"
    if not text:
        return "index"
    try:
        info = json.loads(text)
    except json.JSONDecodeError:
        return "unknown"
    if info.get("dir_info", {}).get("editable"):
        return "editable"
    if str(info.get("url", "")).endswith(".whl"):
        return "wheel"
    return "unknown"


def check_version(expected: str | None, recorded: str | None = None) -> list[Result]:
    recorded = recorded if recorded is not None else installed_version()
    out: list[Result] = []
    if recorded is None:
        out.append(
            Result(
                WARN,
                f"version {__version__}: no pip metadata (a source tree or `make install`)",
                "released installs come from pip/pipx",
            )
        )
    elif recorded != __version__:
        out.append(
            Result(
                FAIL,
                f"the code reports {__version__} but pip recorded {recorded}",
                "a stale copy shadows the install; reinstall with --force",
            )
        )
    else:
        out.append(Result(OK, f"version {__version__} (pip metadata agrees, installed as {install_kind()})"))
    if expected is not None and expected.lstrip("v") != __version__:
        out.append(
            Result(FAIL, f"expected {expected.lstrip('v')} but this is {__version__}", "install the release you meant")
        )
    return out


def check_data(root: Path | None = None) -> list[Result]:
    base = root if root is not None else data_dir()
    missing = [rel for rel in REQUIRED_DATA if not (base / rel).is_file()]
    empty = [rel for rel in REQUIRED_DIRS if not (base / rel).is_dir() or not any((base / rel).iterdir())]
    if missing or empty:
        names = ", ".join(missing + [f"{d}/ (empty)" for d in empty])
        return [
            Result(
                FAIL, f"package data incomplete under {base}: {names}", "reinstall the wheel; do not copy partial trees"
            )
        ]
    return [Result(OK, f"package data complete under {base} ({len(REQUIRED_DATA)} files, Console frontend included)")]


def _resolves(spec: str, importer: Callable[[str], object] = importlib.import_module) -> str | None:
    """None when 'module:function' imports and the function exists, else the reason."""
    module_name, _, attr = spec.partition(":")
    try:
        module = importer(module_name)
    except ImportError as exc:
        return f"import of {module_name} failed: {exc}"
    if not callable(getattr(module, attr, None)):
        return f"{module_name} has no function {attr}"
    return None


def check_entry_points(importer: Callable[[str], object] = importlib.import_module) -> list[Result]:
    declared: dict[str, str] = {}
    recorded = installed_version() is not None
    if recorded:
        declared = {
            ep.name: ep.value for ep in metadata.distribution(DIST_NAME).entry_points if ep.group == "console_scripts"
        }
    out: list[Result] = []
    for name, spec in EXPECTED_ENTRY_POINTS.items():
        if recorded and declared.get(name) != spec:
            out.append(
                Result(FAIL, f"{name}: pip recorded {declared.get(name)!r}, expected {spec!r}", "reinstall the wheel")
            )
            continue
        problem = _resolves(spec, importer)
        on_path = shutil.which(name)
        if problem and name == "streamstack-tray":
            out.append(
                Result(
                    WARN,
                    f"{name}: {problem}",
                    "the tray needs the distribution's python3-gi (pipx --system-site-packages)",
                )
            )
        elif problem:
            out.append(Result(FAIL, f"{name}: {problem}", "reinstall the wheel"))
        elif not on_path and recorded:
            out.append(
                Result(
                    WARN, f"{name} resolves to {spec} but is not on PATH", "run `pipx ensurepath` and open a new shell"
                )
            )
        else:
            out.append(Result(OK, f"{name} -> {spec}" + (f" ({on_path})" if on_path else "")))
    return out


def signature_instructions(version: str = __version__) -> str:
    """How to prove these files are the signed release (the long form is docs/release.md)."""
    base = registry.package_base(GENERIC_PACKAGE, version)
    unconfigured = registry.gitlab_host() is None
    return "\n".join(
        [
            f"Verify that {version} is the signed release (details: docs/release.md, section Verify a release):",
            f"  1. fetch from {base}/ (GitLab token header PRIVATE-TOKEN):",
            "       RELEASE-SHA256SUMS, RELEASE-SHA256SUMS.cose.sig, python-sbom.cdx.json,",
            f"       streamstack-{version}-py3-none-any.whl, streamstack-{version}.tar.gz",
            "  2. notation blob verify --signature RELEASE-SHA256SUMS.cose.sig RELEASE-SHA256SUMS",
            "       (needs notation >= 2.0.0-alpha.1 and the CI signing certificate in a trust store)",
            "  3. sha256sum --check --ignore-missing RELEASE-SHA256SUMS",
            *(
                [f"  (the release host comes from configuration: export {registry.HOST_ENV}=<the GitLab host>)"]
                if unconfigured
                else []
            ),
            f"  or, from a checkout, one command: scripts/install-release.sh --verify-only {version}",
        ]
    )


def run_checks(expected: str | None = None) -> list[Result]:
    return [*check_version(expected), *check_data(), *check_entry_points()]
