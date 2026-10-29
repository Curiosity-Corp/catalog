"""The `crash` module is process-global; the autouse fixture in conftest.py must hand every test a clean one (#159).

The two tests run in file order: the first dirties everything `crash` keeps, the second proves nothing survived it.
"""

from __future__ import annotations

import sys
import threading

from streamstack import crash

PRISTINE_COMPONENT = "streamstack"
# Collection imports this module before any test runs, so these are the process's own hooks.
HOOKS = (sys.excepthook, threading.excepthook, sys.unraisablehook)


def test_a_test_that_installs_the_hooks_and_rate_limits_dirties_the_module() -> None:
    crash.install("leaky-component", lambda: {"leak": True})
    assert crash.record_exception(OSError("x"), "leak-place") is not None
    assert crash._state["installed"] is True


def test_the_next_test_starts_with_the_module_as_the_process_began() -> None:
    assert crash._state["installed"] is False
    assert crash._state["component"] == PRISTINE_COMPONENT
    assert crash._state["provider"] is None
    assert crash._handled_at == {}
    assert (sys.excepthook, threading.excepthook, sys.unraisablehook) == HOOKS
    assert crash.record_exception(OSError("x"), "leak-place") is not None  # not rate limited by the previous test
