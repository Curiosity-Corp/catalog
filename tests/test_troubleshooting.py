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
CI = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())

MESSAGES = re.findall(r"^```text\n(.+?)\n```", TEXT, flags=re.MULTILINE | re.DOTALL)


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
    for fragment in ("stale version", "SHA-256 asset digests", "ipykernel", "rolled back"):
        assert fragment in joined, fragment
    assert len(MESSAGES) >= 4


def test_every_quoted_message_is_still_printed_by_the_role() -> None:
    for message in MESSAGES:
        assert _occurs(message), f"no task prints: {message}"


def test_a_reworded_message_would_fail() -> None:
    assert not _occurs("Could not resolve an upstream release for <name>.")


def test_page_links_resolve() -> None:
    for target in re.findall(r"\]\(([^)\s]+)\)", TEXT):
        path_part, _, anchor = target.partition("#")
        dest = (PAGE.parent / path_part).resolve()
        assert dest.is_file(), target


def test_readme_links_the_page() -> None:
    assert "](docs/troubleshooting.md)" in (ROOT / "README.md").read_text()


def test_this_file_runs_in_the_policy_job() -> None:
    script = " ".join(CI["policy"]["script"])
    assert "tests/test_troubleshooting.py" in script
