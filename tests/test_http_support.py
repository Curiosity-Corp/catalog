"""Guard: socket tests against a real Console use the shared generous timeout (tests/http_support.py)."""

import re
from pathlib import Path

from http_support import HTTP_TIMEOUT

TESTS = Path(__file__).parent


def test_no_test_hard_codes_a_short_http_timeout() -> None:
    offenders = [
        f"{path.name}:{number}"
        for path in TESTS.glob("test_*.py")
        if path.name != Path(__file__).name
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"HTTPConnection\(.*timeout=\d+(\.\d+)?\)", line)
    ]
    assert not offenders, f"use http_support.HTTP_TIMEOUT: {offenders}"


def test_the_shared_timeout_is_longer_than_a_throttled_runner_stall() -> None:
    assert HTTP_TIMEOUT >= 30
