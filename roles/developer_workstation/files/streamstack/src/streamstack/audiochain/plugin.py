"""The DeepFilterNet LADSPA plugin: where CI publishes it, how it is fetched and verified, where the chain finds it.

The plugin is built from a pinned upstream release by the `dfn-ladspa-*` jobs of `.gitlab-ci.yml` (recipe:
`images/dfn-ladspa/`), and published to this project's Generic Package Registry as package `streamstack-ladspa`,
version `<upstream version>-<build revision>`:

    libdeep_filter_ladspa.so            the plugin (label deep_filter_mono)
    SHA256SUMS                          its checksum
    libdeep_filter_ladspa.cdx.json      CycloneDX SBOM of the crates linked into it
    LICENSES.json                       the licence of every one of them (the build fails outside the policy)

`install_deepfilternet` downloads the plugin and its checksum list, refuses anything that does not match, and writes it
to `graph.user_ladspa_dir()` (default `~/.local/share/streamstack/ladspa/`), which the chain searches before the system
LADSPA directories. The fetch is injectable so the logic is unit tested without a network.
"""

from __future__ import annotations

import hashlib
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .. import registry, system
from . import graph

# The pin. `images/dfn-ladspa/Dockerfile` carries the same version and revision as build arguments and
# `tests/test_dfn_ladspa_ci.py` fails when they differ.
UPSTREAM_VERSION = "0.5.6"
BUILD_REVISION = 1
PACKAGE = "streamstack-ladspa"
PACKAGE_VERSION = f"{UPSTREAM_VERSION}-{BUILD_REVISION}"
PLUGIN_FILE = graph.PLUGINS["deepfilternet"]["file"]
SUMS_FILE = "SHA256SUMS"
# sha256 of the published plugin, once a CI build has been accepted and pinned here (None: the published SHA256SUMS
# is trusted alone). When set, a download that differs is refused even if the published sums agree with it.
PINNED_SHA256: str | None = "327bca072d05f79ee74a4b88c1df51169b1ce4c2b5f7f94684d6ddccf473276e"

Fetcher = Callable[[str, tuple[str, str] | None], bytes]


class PluginError(RuntimeError):
    """The plugin could not be fetched or did not verify; the message says what to do."""


def package_url(host: str, project_id: str, name: str) -> str:
    return f"https://{host}/api/v4/projects/{project_id}/packages/generic/{PACKAGE}/{PACKAGE_VERSION}/{name}"


def _token(host: str) -> tuple[str, str] | None:
    """(header, value) for the registry: a personal token from GITLAB_TOKEN or `glab`, or the CI job token in CI."""
    token = os.environ.get("GITLAB_TOKEN")
    if token:
        return "PRIVATE-TOKEN", token.strip()
    result = system.run(["glab", "config", "get", "token", "--host", host], timeout=10)
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return "PRIVATE-TOKEN", result.stdout.strip()
    job = os.environ.get("CI_JOB_TOKEN")
    return ("JOB-TOKEN", job.strip()) if job else None


def http_fetch(url: str, token: tuple[str, str] | None) -> bytes:
    """GET with the GitLab token in a header (never in the URL or the output)."""
    request = urllib.request.Request(url, headers={token[0]: token[1]} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data: bytes = response.read()
            return data
    except urllib.error.HTTPError as exc:
        raise PluginError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise PluginError(f"{url}: {exc}") from exc


def checksum_of(sums: str, name: str) -> str | None:
    """The sha256 listed for `name` in a `sha256sum` style list, or None."""
    for line in sums.splitlines():
        found = re.fullmatch(r"([0-9a-f]{64}) [ *]?(\S+)", line.strip())
        if found and found.group(2) == name:
            return found.group(1)
    return None


def verify_plugin(data: bytes, sums: str, pinned: str | None = None) -> str:
    """The plugin's sha256 when `data` is the file `sums` lists (and the module pin, or `pinned`), else PluginError."""
    listed = checksum_of(sums, PLUGIN_FILE)
    if listed is None:
        raise PluginError(f"{SUMS_FILE} does not list {PLUGIN_FILE}; refusing to install")
    pinned = pinned or PINNED_SHA256
    actual = hashlib.sha256(data).hexdigest()
    if actual != listed:
        raise PluginError(f"{PLUGIN_FILE} does not match {SUMS_FILE} (got {actual[:12]}, expected {listed[:12]})")
    if pinned is not None and actual != pinned:
        raise PluginError(f"{PLUGIN_FILE} is not the build StreamStack pins (got {actual[:12]}, pinned {pinned[:12]})")
    if not data.startswith(b"\x7fELF"):
        raise PluginError(f"{PLUGIN_FILE} is not a Linux shared library")
    if b"ladspa_descriptor" not in data or graph.PLUGINS["deepfilternet"]["label"].encode() not in data:
        raise PluginError(f"{PLUGIN_FILE} is not a DeepFilterNet LADSPA plugin (no ladspa_descriptor or label)")
    return actual


def install_deepfilternet(
    fetch: Fetcher = http_fetch,
    dest: Path | None = None,
    host: str | None = None,
    project_id: str | None = None,
) -> Path:
    """Download, verify and install the plugin. Returns the installed path; raises PluginError with the fix."""
    try:
        host = host or registry.require_gitlab_host()
    except registry.RegistryNotConfigured as exc:
        raise PluginError(str(exc)) from exc
    project_id = project_id or registry.project_id()
    token = _token(host)
    if token is None and fetch is http_fetch:
        raise PluginError(
            f"no GitLab token for {host}: set GITLAB_TOKEN or run `glab auth login --hostname {host}` "
            "(the package registry is private)"
        )
    try:
        sums = fetch(package_url(host, project_id, SUMS_FILE), token).decode()
        data = fetch(package_url(host, project_id, PLUGIN_FILE), token)
    except UnicodeDecodeError as exc:
        raise PluginError(f"{SUMS_FILE} is not text") from exc
    verify_plugin(data, sums)
    folder = dest or graph.user_ladspa_dir()
    target = folder / PLUGIN_FILE
    tmp = folder / f".{PLUGIN_FILE}.tmp"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        tmp.chmod(0o755)
        tmp.replace(target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise PluginError(f"cannot write {target}: {exc}") from exc
    return target


def missing_hint() -> str:
    """The fix `streamstack doctor` and the Console show when DeepFilterNet is not installed."""
    return (
        f"run `streamstack chain install-plugin` (fetches the CI-built, checksum-verified {PLUGIN_FILE} "
        f"{PACKAGE_VERSION} into {graph.user_ladspa_dir()}); see docs/audio-chain.md"
    )


def installed_path() -> Path | None:
    return graph.find_plugin("deepfilternet")


__all__ = [
    "PACKAGE",
    "PACKAGE_VERSION",
    "PluginError",
    "install_deepfilternet",
    "installed_path",
    "missing_hint",
    "verify_plugin",
]
