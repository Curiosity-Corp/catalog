"""Where StreamStack's private release registry is: configuration only, never a hostname in the code.

The GitLab host that serves StreamStack's package registry is private. It must not be written into the vendored
sources (the public catalog contract gate refuses it), so it comes from the environment:

1. ``STREAMSTACK_GITLAB_HOST``: the explicit setting (a bare host name, e.g. ``gitlab.example.org``);
2. ``CI_SERVER_HOST``: set by GitLab CI, so a pipeline of the project needs no setting at all.

The project id defaults to StreamStack's own (a number, not a secret) and ``STREAMSTACK_PROJECT_ID`` overrides it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

HOST_ENV = "STREAMSTACK_GITLAB_HOST"
CI_HOST_ENV = "CI_SERVER_HOST"
PROJECT_ENV = "STREAMSTACK_PROJECT_ID"
DEFAULT_PROJECT_ID = "2122"
HOST_PLACEHOLDER = f"${HOST_ENV}"


class RegistryNotConfigured(RuntimeError):
    """No release host is configured; the message says which variable to set."""


def _clean_host(value: str) -> str:
    """A bare host name from a setting that may carry a scheme, a path or whitespace."""
    host = value.strip().removeprefix("https://").removeprefix("http://")
    return host.split("/", 1)[0].strip()


def gitlab_host(environ: Mapping[str, str] | None = None) -> str | None:
    """The configured release host, or None when nothing configures one."""
    env = os.environ if environ is None else environ
    for name in (HOST_ENV, CI_HOST_ENV):
        host = _clean_host(env.get(name, ""))
        if host:
            return host
    return None


def require_gitlab_host(environ: Mapping[str, str] | None = None) -> str:
    host = gitlab_host(environ)
    if host is None:
        raise RegistryNotConfigured(
            f"no release host configured: set {HOST_ENV} to the GitLab host that serves the StreamStack package "
            f"registry (a pipeline of the project gets it from {CI_HOST_ENV})"
        )
    return host


def project_id(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return env.get(PROJECT_ENV, "").strip() or DEFAULT_PROJECT_ID


def package_base(package: str, version: str, host: str | None = None, environ: Mapping[str, str] | None = None) -> str:
    """The Generic Package Registry URL of ``package``/``version``; ``host`` None reads the configuration.

    When no host is configured a ``$STREAMSTACK_GITLAB_HOST`` placeholder stands in, so instructions that only
    print the URL still read correctly.
    """
    resolved = host or gitlab_host(environ) or HOST_PLACEHOLDER
    return f"https://{resolved}/api/v4/projects/{project_id(environ)}/packages/generic/{package}/{version}"
