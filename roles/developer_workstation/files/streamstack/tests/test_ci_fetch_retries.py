"""CI retries a failed source fetch, so a Gitaly hiccup cannot turn main red on its own."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CI = (Path(__file__).resolve().parent.parent / ".gitlab-ci.yml").read_text()


@pytest.mark.parametrize("name", ["GET_SOURCES_ATTEMPTS", "ARTIFACT_DOWNLOAD_ATTEMPTS", "RESTORE_CACHE_ATTEMPTS"])
def test_the_pipeline_retries_what_the_network_can_drop(name: str) -> None:
    match = re.search(rf'^  {name}: "(\d+)"$', CI, re.MULTILINE)
    assert match, f"{name} is not set in the global variables"
    assert 2 <= int(match[1]) <= 10
