"""Stand-in evidence for docs/decisions/0004-resumable-downloads.md (catalog#133).

The decision record recommends resuming large artifacts with `curl -C -` into
`<dest>.part`, then the same SHA-256 check the role uses today, then a rename.
These tests run that procedure (FETCH below, a prototype: the role does not
use it) against local stand-in servers only, never the real network:

- a range-capable origin reached through a stand-in forwarding proxy set with
  the lowercase `http_proxy` / `no_proxy` variables that
  examples/restricted-network-site.yml exports, which proves the proxy keeps
  the `Range` header and only the missing bytes cross the link;
- a corrupted partial, which resumes but must fail the digest check and leave
  nothing behind (resuming alone cannot detect corruption, so the check stays);
- an origin without range support (plain `python3 -m http.server`), where
  curl exits 33 and the procedure must start again from zero;
- a partial that is already complete, which must not be downloaded again.
"""

from __future__ import annotations

import hashlib
import http.client
import http.server
import os
import shutil
import socketserver
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

# The prototype procedure. Exit 33 is curl's "server does not support byte
# ranges"; everything else non-zero is a real failure.
FETCH = r"""
set -u
url=$1 dest=$2 sha=$3
part="$dest.part"
verify() { echo "$sha  $part" | sha256sum --check --status; }
if [ -s "$part" ] && verify; then mv "$part" "$dest"; exit 0; fi
rc=0
curl --fail --location --silent --show-error --continue-at - --output "$part" "$url" || rc=$?
if [ "$rc" -eq 33 ]; then
  rm -f "$part"
  rc=0
  curl --fail --location --silent --show-error --output "$part" "$url" || rc=$?
fi
[ "$rc" -eq 0 ] || exit "$rc"
if ! verify; then rm -f "$part"; echo "checksum mismatch: $part removed" >&2; exit 1; fi
mv "$part" "$dest"
"""

PAYLOAD = os.urandom(1_000_000)
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def test_curl_is_available() -> None:
    # The procedure needs curl, which every profile installs. Fail loudly
    # instead of skipping so CI cannot pass this file without running it.
    assert shutil.which("curl"), "curl is required for the resumable-download stand-in tests"


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, handler: type[http.server.BaseHTTPRequestHandler], root: Path | None = None) -> None:
        self.root = root
        self.ranges: list[str | None] = []
        self.body_bytes = 0
        self.request_lines: list[str] = []
        super().__init__(("127.0.0.1", 0), handler)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _RangeOrigin(http.server.BaseHTTPRequestHandler):
    """Serves PAYLOAD and honours a single open-ended `bytes=N-` range."""

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        server: _Server = self.server  # type: ignore[assignment]
        header = self.headers.get("Range")
        server.ranges.append(header)
        start = 0
        if header:
            assert header.startswith("bytes=") and header.endswith("-"), header
            start = int(header[len("bytes=") : -1])
            if start >= len(PAYLOAD):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(PAYLOAD)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
        else:
            self.send_response(200)
        body = PAYLOAD[start:]
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        server.body_bytes += len(body)


class _NoRangeOrigin(http.server.SimpleHTTPRequestHandler):
    """`python3 -m http.server` behaviour: the Range header is ignored."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        server = args[2]
        super().__init__(*args, directory=str(server.root), **kwargs)  # type: ignore[arg-type]

    def log_message(self, *args: object) -> None:
        pass

    def send_head(self):  # type: ignore[no-untyped-def]
        self.server.ranges.append(self.headers.get("Range"))  # type: ignore[attr-defined]
        del self.headers["Range"]
        return super().send_head()


class _ForwardingProxy(http.server.BaseHTTPRequestHandler):
    """A plain-HTTP forward proxy that records what it forwards."""

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        server: _Server = self.server  # type: ignore[assignment]
        server.request_lines.append(self.requestline)
        target = urlsplit(self.path)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in {"proxy-connection", "connection"}}
        conn = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
        conn.request("GET", target.path, headers=headers)
        upstream = conn.getresponse()
        body = upstream.read()
        self.send_response(upstream.status)
        for key, value in upstream.getheaders():
            if key.lower() not in {"connection", "transfer-encoding", "server", "date"}:
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)
        conn.close()


def _serve(server: _Server) -> Iterator[_Server]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def range_origin() -> Iterator[_Server]:
    yield from _serve(_Server(_RangeOrigin))


@pytest.fixture
def proxy() -> Iterator[_Server]:
    yield from _serve(_Server(_ForwardingProxy))


@pytest.fixture
def no_range_origin(tmp_path: Path) -> Iterator[_Server]:
    root = tmp_path / "www"
    root.mkdir()
    (root / "artifact.bin").write_bytes(PAYLOAD)
    yield from _serve(_Server(_NoRangeOrigin, root))


def _fetch(url: str, dest: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base_env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    return subprocess.run(
        ["bash", "-c", FETCH, "fetch", url, str(dest), DIGEST],
        env={**base_env, **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_resume_through_proxy_fetches_only_the_missing_bytes(
    tmp_path: Path, range_origin: _Server, proxy: _Server
) -> None:
    dest = tmp_path / "artifact.archive"
    Path(f"{dest}.part").write_bytes(PAYLOAD[:300_000])

    result = _fetch(
        f"{range_origin.base}/artifact.bin",
        dest,
        {"http_proxy": proxy.base, "https_proxy": proxy.base, "no_proxy": ""},
    )

    assert result.returncode == 0, result.stderr
    assert dest.read_bytes() == PAYLOAD
    assert not Path(f"{dest}.part").exists()
    assert proxy.request_lines == [f"GET {range_origin.base}/artifact.bin HTTP/1.1"]
    assert range_origin.ranges == ["bytes=300000-"]
    assert range_origin.body_bytes == len(PAYLOAD) - 300_000


def test_corrupted_partial_fails_the_digest_and_leaves_nothing(tmp_path: Path, range_origin: _Server) -> None:
    dest = tmp_path / "artifact.archive"
    Path(f"{dest}.part").write_bytes(b"\0" * 300_000)

    result = _fetch(f"{range_origin.base}/artifact.bin", dest)

    assert result.returncode == 1
    assert "checksum mismatch" in result.stderr
    assert not dest.exists()
    assert not Path(f"{dest}.part").exists()


def test_origin_without_range_support_starts_again_from_zero(tmp_path: Path, no_range_origin: _Server) -> None:
    dest = tmp_path / "artifact.archive"
    Path(f"{dest}.part").write_bytes(PAYLOAD[:300_000])

    result = _fetch(f"{no_range_origin.base}/artifact.bin", dest)

    assert result.returncode == 0, result.stderr
    assert dest.read_bytes() == PAYLOAD
    assert no_range_origin.ranges == ["bytes=300000-", None]


def test_complete_partial_is_not_downloaded_again(tmp_path: Path, range_origin: _Server) -> None:
    dest = tmp_path / "artifact.archive"
    Path(f"{dest}.part").write_bytes(PAYLOAD)

    result = _fetch(f"{range_origin.base}/artifact.bin", dest)

    assert result.returncode == 0, result.stderr
    assert dest.read_bytes() == PAYLOAD
    assert range_origin.ranges == []
