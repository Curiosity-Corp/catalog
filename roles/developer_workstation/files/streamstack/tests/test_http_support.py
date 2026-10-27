"""Guard: socket tests against a real Console use the shared generous timeout (tests/http_support.py)."""

import re
from pathlib import Path

from http_support import HTTP_TIMEOUT

TESTS = Path(__file__).parent
SHORT = re.compile(r"(?:HTTPConnection|urlopen)\(.*timeout=(\d+(?:\.\d+)?)\)")


def test_no_test_hard_codes_a_short_http_timeout() -> None:
    offenders = [
        f"{path.name}:{number}"
        for path in TESTS.rglob("test_*.py")
        if path.name != Path(__file__).name
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if (found := SHORT.search(line)) and float(found.group(1)) < 30
    ]
    assert not offenders, f"use http_support.HTTP_TIMEOUT (or tests/e2e/support.py): {offenders}"


def test_the_shared_timeout_is_longer_than_a_throttled_runner_stall() -> None:
    assert HTTP_TIMEOUT >= 30
