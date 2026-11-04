"""MkDocs hook: make links that leave docs/ work on the built site.

Many pages link to files that live outside docs/ (GOVERNANCE.md, examples/,
the role's files). On GitHub those relative links work; on a built site they
would be dead. This hook rewrites them to the repository's file or folder at
the same revision the site is built from (`DOCS_REF`, default `main`; set it to
the release tag when building a release, so the site matches that version).

Links that stay inside docs/ are left to MkDocs, which checks them in strict
mode. The hook reads local files only.
"""

from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(\]\()([^)\s#]+)(#[^)\s]*)?(\))")


def rewrite(markdown: str, src_uri: str, repo_url: str, ref: str) -> str:
    page_dir = posixpath.dirname(src_uri)
    parts = re.split(r"(```.*?```)", markdown, flags=re.DOTALL)  # leave fenced code alone
    for index in range(0, len(parts), 2):
        def swap(match: re.Match) -> str:
            target = match.group(2)
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("/"):
                return match.group(0)
            resolved = posixpath.normpath(posixpath.join(page_dir, target))
            if not resolved.startswith(".."):
                return match.group(0)
            repo_path = posixpath.normpath(posixpath.join("docs", resolved))
            kind = "tree" if (ROOT / repo_path).is_dir() else "blob"
            return f"{match.group(1)}{repo_url.rstrip('/')}/{kind}/{ref}/{repo_path}{match.group(3) or ''}{match.group(4)}"
        parts[index] = LINK.sub(swap, parts[index])
    return "".join(parts)


def on_page_markdown(markdown, page, config, files):  # noqa: ANN001 - MkDocs hook signature
    return rewrite(markdown, page.file.src_uri, config["repo_url"], os.environ.get("DOCS_REF", "main"))
