"""One timeout for the tests that talk to a real Console server over a socket.

The CI runners share CPU: the Python 3.11 job runs the whole suite in a throttled pod and a first request can wait
longer than any fixed few seconds (pipeline 88138: three telemetry tests timed out at 10 s on their first action while
the same tests take milliseconds on a free core). A request that takes this long is a hang, not slowness, so the limit
is generous; it never makes a passing test slower. Override with STREAMSTACK_TEST_HTTP_TIMEOUT (seconds).
"""

import os

HTTP_TIMEOUT = float(os.environ.get("STREAMSTACK_TEST_HTTP_TIMEOUT", "90"))
