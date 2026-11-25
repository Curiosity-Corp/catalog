"""catalog#128: the troubleshooting page quotes messages the role still prints.

Every text code block in docs/troubleshooting.md is a message. Its fixed parts
(between <placeholders>) must occur, in order, in one string of the role's
task files, with Jinja expressions standing in for the placeholders. A reworded
message fails here until the page is updated.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "roles/developer_workstation/tasks"
PAGE = ROOT / "docs/troubleshooting.md"
TEXT = PAGE.read_text()
QUICKSTARTS = ROOT / "docs/quickstarts"
CI = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())

# Quickstarts quote some of the same messages (docs/quickstarts/restricted-networks.md);
# they are held to the same rule.
QUOTING_PAGES = [PAGE, *sorted(QUICKSTARTS.glob("*.md"))]


def _messages(text: str) -> list[str]:
    return re.findall(r"^```text\n(.+?)\n```", text, flags=re.MULTILINE | re.DOTALL)


MESSAGES = _messages(TEXT)


def _strings(node: object):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


class _TaskLoader(yaml.SafeLoader):
    """SafeLoader that reads Ansible's tagged scalars (!unsafe, !vault) as plain values."""


_TaskLoader.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_scalar(node))

HOLE = "\x00"


def _normalise(text: str) -> str:
    return re.sub(r"\s*\x00\s*", HOLE, " ".join(text.split()))


def _role_strings() -> list[str]:
    found = []
    for path in sorted(TASKS.glob("*.yml")):
        for value in _strings(yaml.load(path.read_text(), Loader=_TaskLoader)):
            found.append(_normalise(re.sub(r"\{\{.*?\}\}", HOLE, value, flags=re.DOTALL)))
    return found


ROLE = _role_strings()


def _occurs(message: str) -> bool:
    parts = [_normalise(p) for p in re.split(r"<[a-z]+>", message)]
    pattern = HOLE.join(re.escape(p) for p in parts)
    return any(re.search(pattern, value) for value in ROLE)


def test_page_quotes_the_issue_s_failures() -> None:
    joined = "\n".join(MESSAGES)
    for fragment in (
        "stale version",
        "SHA-256 asset digests",
        "ipykernel",
        "is not an IANA zone",
        "rolled back",
    ):
        assert fragment in joined, fragment
    assert len(MESSAGES) >= 5


def test_every_quoted_message_is_still_printed_by_the_role() -> None:
    for page in QUOTING_PAGES:
        for message in _messages(page.read_text()):
            assert _occurs(message), f"{page.relative_to(ROOT)}: no task prints: {message}"


def test_a_reworded_message_would_fail() -> None:
    assert not _occurs("Could not resolve an upstream release for <name>.")
    assert not _occurs("workstation_timezone=<value> is not a valid zone.")


def test_page_links_resolve() -> None:
    for target in re.findall(r"\]\(([^)\s]+)\)", TEXT):
        path_part, _, anchor = target.partition("#")
        dest = (PAGE.parent / path_part).resolve()
        assert dest.is_file(), target


def test_readme_links_the_page() -> None:
    assert "](docs/troubleshooting.md)" in (ROOT / "README.md").read_text()


def test_quickstarts_index_links_the_page() -> None:
    index = QUICKSTARTS / "README.md"
    assert "](../troubleshooting.md)" in index.read_text()


def test_quickstarts_index_links_resolve() -> None:
    index = QUICKSTARTS / "README.md"
    for target in re.findall(r"\]\(([^)\s]+)\)", index.read_text()):
        assert (index.parent / target.partition("#")[0]).resolve().exists(), target


def test_this_file_runs_in_the_policy_job() -> None:
    script = " ".join(CI["policy"]["script"])
    assert "tests/test_troubleshooting.py" in script
