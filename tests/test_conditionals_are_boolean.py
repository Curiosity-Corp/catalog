"""ansible-core 2.19 fails a task whose conditional renders to a non-boolean.

`regex_search` returns the match string (or None), so a bare
`x | regex_search(...)` in `assert.that`, `when`, `failed_when` or
`changed_when` passes lint and the unit tests but fails on the host
(deskmeet1 pull, 2026-10-29 23:17, minimal-desktop-integrity.yml). Use
`is match(...)`/`is search(...)`, or compare the result with `is none`.
"""

import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CONDITION_KEYS = ("when", "failed_when", "changed_when")
BARE_REGEX_SEARCH = re.compile(r"regex_search\(.*\)\s*$", re.S)


def _conditions(node):
    if isinstance(node, list):
        for item in node:
            yield from _conditions(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in CONDITION_KEYS or (key == "that" and "ansible.builtin.assert" not in node):
                values = value if isinstance(value, list) else [value]
                yield from (v for v in values if isinstance(v, str))
            elif key in ("ansible.builtin.assert", "assert") and isinstance(value, dict):
                that = value.get("that", [])
                yield from (v for v in (that if isinstance(that, list) else [that]) if isinstance(v, str))
            else:
                yield from _conditions(value)


class _Loader(yaml.SafeLoader):
    """SafeLoader that reads Ansible's `!unsafe` scalars as plain strings."""


_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


TASK_FILES = sorted(ROOT.glob("roles/*/tasks/*.yml")) + sorted(ROOT.glob("roles/*/handlers/*.yml"))


@pytest.mark.parametrize("path", TASK_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_conditional_ends_in_a_bare_regex_search(path):
    data = yaml.load(path.read_text(), Loader=_Loader) or []
    offenders = [c for c in _conditions(data) if BARE_REGEX_SEARCH.search(c.strip())]
    assert not offenders, f"non-boolean conditionals: {offenders}"
