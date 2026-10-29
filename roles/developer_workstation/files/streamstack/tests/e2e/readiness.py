"""Readiness gate and first-load measurement for the e2e demo server (issue #124).

Firefox ``Page.goto`` timeouts recurred on the shared CI runner: the console is an unbundled ES-module graph read from
disk (~150 files), so the first browser to open it pays for every module's first read, on a runner whose fsyncs stall
for tens of seconds (infra#639), and the first navigation of a fresh browser engine pays for its own start on top.
The ``rig`` fixture therefore does not yield until this module has fetched the page and its whole module graph from
the server (every file 200, timed), so the first test's navigation hits a warm server; the numbers are printed in the
terminal summary, so the next stall is measured, not guessed. Standard library only: the unit suite proves the gate
(tests/test_e2e_readiness.py) without Playwright.
"""

from __future__ import annotations

import posixpath
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

Fetch = Callable[[str], tuple[int, bytes]]  # path relative to the server root -> (HTTP status, body)

# `static/app.js` in the page, `import x from "./core/y.js"`, `export * from '../z.js'`, `import("./pages/p.js")`.
HTML_ASSET = re.compile(r"""(?:src|href)=["'](static/[\w./-]+\.(?:js|css))["']""")
# whole-line `//` comments and `/* */` blocks: doc comments quote imports (`from '../app.js'`) that nothing loads
JS_COMMENT = re.compile(r"(?m)^[ \t]*//[^\n]*$|/\*.*?\*/", re.S)
JS_IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*)["'](\.{1,2}/[\w./-]+\.js)["']""")


class NotReady(RuntimeError):
    """The server did not become ready in time, or answered a file of the page with an error."""


@dataclass
class FirstLoad:
    """What fetching the page and its module graph from a cold server cost."""

    api_wait_s: float = 0.0
    files: dict[str, tuple[int, float]] = field(default_factory=dict)  # path -> (bytes, milliseconds)

    @property
    def total_bytes(self) -> int:
        return sum(size for size, _ in self.files.values())

    @property
    def total_ms(self) -> float:
        return sum(ms for _, ms in self.files.values())

    def summary(self) -> str:
        slowest = max(self.files.items(), key=lambda item: item[1][1], default=("-", (0, 0.0)))
        return (
            f"console first load: {len(self.files)} files, {self.total_bytes / 1024:.0f} KiB, "
            f"{self.total_ms:.0f} ms to read them all (slowest {slowest[0]} {slowest[1][1]:.0f} ms); "
            f"API answered after {self.api_wait_s:.1f} s"
        )


def references(path: str, body: bytes) -> list[str]:
    """The files ``body`` (served at ``path``) loads: stylesheets and scripts of the page, imports of a module."""
    text = body.decode("utf-8", errors="replace")
    if path.endswith(".js"):
        text = JS_COMMENT.sub("", text)
        return [posixpath.normpath(posixpath.join(posixpath.dirname(path), ref)) for ref in JS_IMPORT.findall(text)]
    if path.endswith(".css"):
        return []
    return HTML_ASSET.findall(text)


def load_graph(fetch: Fetch, root: str = "", *, clock: Callable[[], float] = time.monotonic) -> FirstLoad:
    """Fetch ``root`` and everything it references; every file must answer 200."""
    result = FirstLoad()
    queue = [root]
    while queue:
        path = queue.pop(0)
        if path in result.files:
            continue
        began = clock()
        status, body = fetch(path)
        result.files[path] = (len(body), (clock() - began) * 1000)
        if status != 200:
            raise NotReady(f"the console served {path or '/'} with HTTP {status} while the e2e server was warming up")
        queue.extend(references(path or "index.html", body))
    return result


def gate(
    fetch: Fetch,
    probe: Callable[[], bool],
    *,
    timeout: float = 60.0,
    interval: float = 0.25,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> FirstLoad:
    """Block until the server is ready, then read the page and its module graph once.

    ``probe`` is the server's own readiness answer (the authenticated state endpoint, which the demo server serves only
    once its rig is built); it may raise OSError while the socket is still coming up. Raises ``NotReady`` on timeout.
    """
    began = clock()
    last: str = "no answer"
    while True:
        try:
            if probe():
                break
            last = "the readiness probe said not ready"
        except OSError as exc:
            last = f"{type(exc).__name__}: {exc}"
        if clock() - began >= timeout:
            raise NotReady(f"the e2e server was not ready after {timeout:.0f} s ({last})")
        sleep(interval)
    waited = clock() - began
    loaded = load_graph(fetch, clock=clock)
    loaded.api_wait_s = waited
    return loaded
