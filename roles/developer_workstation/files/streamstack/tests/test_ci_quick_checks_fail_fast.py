"""A seconds-long CI check that hangs must time out and retry, so it cannot hold the merge train for an hour (#146)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CI = (Path(__file__).resolve().parent.parent / ".gitlab-ci.yml").read_text()


def job(name: str) -> str:
    match = re.search(rf"^{re.escape(name)}:\n((?:[ #].*\n|\n)+)", CI, re.MULTILINE)
    assert match, f"{name} is not a job in .gitlab-ci.yml"
    return match[1]


@pytest.mark.parametrize("name", ["changelog-check"])
def test_a_quick_merge_request_check_has_a_short_timeout(name: str) -> None:
    match = re.search(r"^  timeout: (\d+)m$", job(name), re.MULTILINE)
    assert match, f"{name} has no timeout of its own, so a hung runner holds it for the project limit"
    assert int(match[1]) <= 15


@pytest.mark.parametrize("name", ["changelog-check"])
def test_a_quick_merge_request_check_retries_a_stuck_runner(name: str) -> None:
    body = job(name)
    assert re.search(r"^  retry:\n    max: [1-2]$", body, re.MULTILINE)
    assert "      - stuck_or_timeout_failure" in body
