"""The documentation builds without private services, and its snippets still parse.

`mkdocs build --strict` runs when mkdocs is installed (the `docs-site` CI job
installs it from docs/requirements.txt); the other checks need only PyYAML and
bash.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
FENCE = re.compile(r"^```([A-Za-z0-9_+-]*)\n(.*?)^```", re.DOTALL | re.MULTILINE)


class _Loader(yaml.SafeLoader):
    """Safe loader that accepts MkDocs' `!!python/name:` style tags as plain strings."""


_Loader.add_multi_constructor("tag:yaml.org,2002:python/", lambda loader, suffix, node: None)


def _nav_files(nav: list) -> list[str]:
    found: list[str] = []
    for item in nav:
        for value in item.values():
            found += _nav_files(value) if isinstance(value, list) else [value]
    return found


def _load_hooks():
    spec = importlib.util.spec_from_file_location("mkdocs_hooks", ROOT / "scripts/mkdocs_hooks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFIG = yaml.load((ROOT / "mkdocs.yml").read_text(), Loader=_Loader)


def test_nav_lists_every_page_exactly_once_and_only_real_pages() -> None:
    listed = _nav_files(CONFIG["nav"])
    assert len(listed) == len(set(listed)), "a page appears twice in the nav"
    on_disk = {p.relative_to(DOCS).as_posix() for p in DOCS.rglob("*.md")}
    assert set(listed) == on_disk, sorted(set(listed) ^ on_disk)


def test_configuration_is_strict_and_private_service_free() -> None:
    assert CONFIG["strict"] is True
    assert CONFIG["repo_url"].startswith("https://github.com/Curiosity-Corp/")
    text = (ROOT / "mkdocs.yml").read_text().lower()
    for marker in ("developerdojo", "focusapiary", "focuspass", "token", "password"):
        assert marker not in text
    assert (DOCS / "requirements.txt").read_text().strip() == "mkdocs==1.6.1", "the docs toolchain is pinned"
    assert not [p for p in CONFIG.get("plugins", []) if p != "search"], "no plugin that fetches from a private service"


def test_index_page_links_every_quickstart() -> None:
    index = (DOCS / "index.md").read_text()
    for guide in sorted((DOCS / "quickstarts").glob("*.md")):
        if guide.name != "README.md":
            assert f"quickstarts/{guide.name}" in index, guide.name


def test_links_that_leave_docs_are_rewritten_to_the_repository_at_a_ref() -> None:
    hooks = _load_hooks()
    repo = "https://github.com/Curiosity-Corp/catalog/"
    top = hooks.rewrite("[gov](../GOVERNANCE.md#who-approves-what) [dir](../examples)", "x.md", repo, "v0.2.0")
    assert "(https://github.com/Curiosity-Corp/catalog/blob/v0.2.0/GOVERNANCE.md#who-approves-what)" in top
    assert "(https://github.com/Curiosity-Corp/catalog/tree/v0.2.0/examples)" in top
    nested = hooks.rewrite("[gov](../../GOVERNANCE.md)", "community/x.md", repo, "main")
    assert "(https://github.com/Curiosity-Corp/catalog/blob/main/GOVERNANCE.md)" in nested
    untouched = "[page](other.md) [web](https://example.org/x) [anchor](#top) [sibling](../quickstarts/laptop.md)"
    assert hooks.rewrite(untouched, "community/x.md", repo, "main") == untouched
    fenced = "```\n[code](../../keep.md)\n```\n"
    assert hooks.rewrite(fenced, "community/x.md", repo, "main") == fenced, "links inside code fences stay as written"


def _snippets(language: tuple[str, ...]):
    for page in sorted(DOCS.rglob("*.md")):
        for match in FENCE.finditer(page.read_text()):
            if match.group(1) in language:
                yield pytest.param(page.relative_to(ROOT).as_posix(), match.group(2), id=f"{page.relative_to(DOCS)}:{match.start()}")


@pytest.mark.parametrize(("page", "code"), list(_snippets(("yaml", "yml"))))
def test_yaml_snippets_parse(page: str, code: str) -> None:
    list(yaml.safe_load_all(code))


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.parametrize(("page", "code"), list(_snippets(("bash", "sh"))))
def test_shell_snippets_have_valid_syntax(page: str, code: str) -> None:
    result = subprocess.run(["bash", "-n"], input=code, capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"{page}: {result.stderr}"


@pytest.mark.skipif(importlib.util.find_spec("mkdocs") is None, reason="mkdocs not installed")
def test_strict_build_succeeds(tmp_path: Path) -> None:
    result = subprocess.run([sys.executable, "-m", "mkdocs", "build", "--strict", "-f", str(ROOT / "mkdocs.yml"), "-d", str(tmp_path / "site")],
                            cwd=ROOT, capture_output=True, text=True, timeout=600, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "site/index.html").exists()
