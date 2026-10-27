"""streamstack.registry and the repository as a whole: the private release host is configuration, never code."""

from __future__ import annotations

from pathlib import Path

import pytest

from streamstack import registry

ROOT = Path(__file__).resolve().parents[1]
# built in two pieces so this file does not itself contain the name it forbids
PRIVATE_HOST = "git." + "developerdojo" + ".org"


def test_the_host_is_the_explicit_setting_then_the_ci_server() -> None:
    assert registry.gitlab_host({}) is None
    assert registry.gitlab_host({"CI_SERVER_HOST": "ci.example"}) == "ci.example"
    both = {"STREAMSTACK_GITLAB_HOST": "set.example", "CI_SERVER_HOST": "ci.example"}
    assert registry.gitlab_host(both) == "set.example"
    assert registry.gitlab_host({"STREAMSTACK_GITLAB_HOST": " https://set.example/some/path "}) == "set.example"
    assert registry.gitlab_host({"STREAMSTACK_GITLAB_HOST": "  "}) is None


def test_an_unconfigured_host_is_an_error_that_names_the_variable() -> None:
    with pytest.raises(registry.RegistryNotConfigured, match="STREAMSTACK_GITLAB_HOST"):
        registry.require_gitlab_host({})


def test_package_base_and_project_id() -> None:
    env = {"STREAMSTACK_GITLAB_HOST": "h.example"}
    assert registry.package_base("p", "1.2.3", environ=env) == (
        "https://h.example/api/v4/projects/2122/packages/generic/p/1.2.3"
    )
    env["STREAMSTACK_PROJECT_ID"] = "9"
    assert "/projects/9/" in registry.package_base("p", "1", environ=env)
    assert registry.package_base("p", "1", host="x.example", environ={}).startswith("https://x.example/")


GUARDED_DIRS = ("src", "scripts", "docs", "packaging", "tools")
SKIPPED_PARTS = {"__pycache__", ".venv", ".ci-venv", "node_modules", ".git"}


def _shipped_files() -> list[Path]:
    """Every file under the guarded directories, found by walking the tree (no git binary needed in CI)."""
    return [
        path
        for top in GUARDED_DIRS
        for path in (ROOT / top).rglob("*")
        if path.is_file() and not SKIPPED_PARTS & set(path.relative_to(ROOT).parts)
    ]


def test_no_shipped_file_names_the_private_release_host() -> None:
    """The public catalog contract gate forbids the host in vendored code: the script, the docs and the package."""
    skip = (".png", ".jpg", ".ico", ".gz", ".whl", ".onnx", ".wav", ".ttf", ".woff2", ".pyc")
    shipped = [path for path in _shipped_files() if path.suffix not in skip]
    assert shipped
    offenders = [str(p.relative_to(ROOT)) for p in shipped if PRIVATE_HOST.encode() in p.read_bytes()]
    assert not offenders, f"the release host is configuration only; found it in {offenders}"
