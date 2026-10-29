"""The Console's HTTP server: token-guarded JSON API, SSE, MJPEG previews and the static frontend."""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from streamstack import config, crash
from streamstack.console.diagnostics import METRICS_PATH
from streamstack.console.errors import ActionError
from streamstack.console.hub import Hub
from streamstack.console.rig import Rig
from streamstack.console.whep import MAX_SDP, WhepError, WhepService
from streamstack.presets import catalog as presets_catalog

MAX_BODY = 64 * 1024
TOKEN_HEADER = "X-StreamStack-Token"  # noqa: S105 - a header name
ALLOW_HOSTS_ENV = "STREAMSTACK_CONSOLE_ALLOW_HOSTS"
PING_SECONDS = 15
ACTION_LOG_ENV = (
    "STREAMSTACK_CONSOLE_ACTION_LOG"  # any non-empty value: one stderr line per action (the e2e rig sets it)
)
SLOW_ACTION_S = 1.0  # an action that takes longer is logged even without the switch
CSP = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
)
CONNECTION_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)
CLIP_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".wav": "audio/wav",
    ".txt": "text/plain; charset=utf-8",
}

# Shown when the package carries no frontend (a broken install or a bare checkout). It has its own CSP
# because the page-wide one forbids the inline style a readable page wants.
FALLBACK_CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
FALLBACK_PAGE = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>StreamStack Console</title>
<style>body{font:16px/1.5 system-ui,sans-serif;background:#14161a;color:#e8eaed;margin:0;display:grid;
place-items:center;min-height:100vh}main{max-width:32rem;padding:1.5rem}code{background:#262a31;padding:.1rem .4rem;
border-radius:4px}</style></head>
<body><main><h1>StreamStack Console</h1>
<p>The Console server is running, but its frontend is not installed (no <code>index.html</code> under
<code>data/console/</code>).</p>
<p>Reinstall StreamStack, or run <code>streamstack doctor</code> in a terminal to see what the Console would show.</p>
</main></body></html>
"""


def allowed_hosts_from_env() -> set[str]:
    return {h.strip().lower() for h in os.environ.get(ALLOW_HOSTS_ENV, "").split(",") if h.strip()}


def safe_join(root: Path, relative: str) -> Path | None:
    """`relative` under `root`, or None when it would leave it (dot segments, backslashes, links)."""
    if not relative or "\x00" in relative or "\\" in relative or any(p == ".." for p in relative.split("/")):
        return None
    try:
        root = root.resolve()
        target = (root / relative.lstrip("/")).resolve()
    except (OSError, RuntimeError):
        return None
    return target if target.is_relative_to(root) and target.is_file() else None


def clamp_int(query: dict[str, list[str]], name: str, default: int, low: int, high: int) -> int:
    raw = query.get(name, [str(default)])[0]
    try:
        return max(low, min(high, int(raw)))
    except ValueError:
        raise ActionError(f"{name} must be a whole number.", 400) from None


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(
        self,
        rig: Rig,
        port: int = 0,
        token: str | None = None,
        host: str = "127.0.0.1",
        static_dir: Path | None = None,
        allow_hosts: set[str] | None = None,
    ) -> None:
        super().__init__((host, port), Handler)
        self.rig = rig
        self.hub = Hub(rig)
        self.whep = WhepService(rig.frame)
        self.token = token or secrets.token_urlsafe(24)
        self.static_dir = static_dir or config.data_dir() / "console"
        self.allow_hosts = allow_hosts if allow_hosts is not None else allowed_hosts_from_env()
        self.stopping = threading.Event()
        self._thread: threading.Thread | None = None
        if rig.diagnostics is not None:
            rig.diagnostics.attach(self)
        if rig.call is not None:
            self.hub.meter_listeners.append(rig.call.on_meters)  # the talking-while-muted warning hears every frame
        on_meters = getattr(rig.framing, "on_meters", None)
        if on_meters is not None:
            self.hub.meter_listeners.append(on_meters)  # speaker hand-off hears the raw microphone too
        if rig.preflight is not None:
            self.hub.meter_listeners.append(rig.preflight.on_meters)  # the spoken prompt hears the raw microphone
        if rig.safejoin is not None:
            self.hub.meter_listeners.append(rig.safejoin.on_meters)  # a hold notices a manual release; the timer

    @property
    def port(self) -> int:
        return self.server_address[1]

    def url(self, with_token: bool = True) -> str:
        return f"http://127.0.0.1:{self.port}/" + (f"?token={self.token}" if with_token else "")

    def host_allowed(self, header: str | None) -> bool:
        """DNS-rebinding guard: only the literal loopback names on our own port (plus the configured extras)."""
        if not header:
            return False
        header = header.strip().lower()
        if header in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            return True
        return "*" in self.allow_hosts or header in self.allow_hosts or header.rpartition(":")[0] in self.allow_hosts

    def token_ok(self, supplied: str | None) -> bool:
        return supplied is not None and hmac.compare_digest(supplied.encode(), self.token.encode())

    def start(self) -> None:
        """Start the rig, the hub and the accept loop (all in background threads)."""
        self.rig.start()
        self.hub.start()
        # A short poll interval keeps shutdown (and the tests) quick.
        self._thread = threading.Thread(
            target=lambda: self.serve_forever(poll_interval=0.1), name="console-http", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        """Stop everything; open SSE and MJPEG handlers notice `stopping` and return."""
        self.stopping.set()
        self.hub.stop()
        self.whep.close()
        if self._thread:
            self.shutdown()
            self._thread.join(timeout=3)
        self.server_close()
        self.rig.stop()

    def handle_error(self, request: object, client_address: object) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, CONNECTION_ERRORS):
            return  # a window went away mid-response
        print("streamstack console: request failed", file=sys.stderr)
        traceback.print_exc()
        if isinstance(exc, Exception):
            crash.record_exception(exc, "console request handler", "console")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60  # an idle keep-alive connection, or a client that stopped reading a stream, is dropped
    server: ConsoleServer  # type: ignore[assignment]
    server_version = "StreamStack"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        return None

    # -- plumbing -------------------------------------------------------------

    _started = False  # headers of the current request are on the wire
    _params_seen: tuple[str, ...] = ()  # the parameter names of the action being run (for the action log)

    def _begin(self, status: int, content_type: str, length: int | None = None, csp: str = CSP, **extra: str) -> None:
        self._started = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if length is None:
            self.send_header("Connection", "close")
            self.close_connection = True
        else:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp)
        for name, value in extra.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()

    def _send(self, status: int, body: bytes, content_type: str, csp: str = CSP, **extra: str) -> None:
        self._begin(status, content_type, len(body), csp, **extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: object) -> None:
        self._send(status, json.dumps(payload, separators=(",", ":")).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    def _dispatch(self) -> None:
        self._started = False
        try:
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if not self.server.host_allowed(self.headers.get("Host")):
                self.close_connection = True
                return self._error(403, "This host name is not allowed.")
            if url.path == METRICS_PATH:
                return self._metrics(query)
            if url.path.startswith("/api/"):
                supplied = self.headers.get(TOKEN_HEADER) or (query.get("token") or [None])[0]
                if not self.server.token_ok(supplied):
                    self.close_connection = True
                    return self._error(401, "Missing or wrong token; open the Console with `streamstack console`.")
                return self._api(url.path, query)
            if self.command in ("GET", "HEAD"):
                return self._static(url.path)
            self.close_connection = True
            return self._error(405, "Method not allowed.")
        except CONNECTION_ERRORS:
            self.close_connection = True
        except ActionError as exc:
            if self._started:
                self.close_connection = True
            else:
                self._error(exc.status, exc.message)
        except Exception as exc:  # never take the server down for one bad request
            print(f"streamstack console: {self.command} {self.path.split('?')[0]} failed", file=sys.stderr)
            traceback.print_exc()
            crash.record_exception(exc, f"console {self.command} handler", "console")
            if self.server.rig.diagnostics is not None:
                self.server.rig.diagnostics.observe_http_error(500)
            self.close_connection = True
            if not self._started:
                try:
                    self._error(500, f"Internal error: {type(exc).__name__}: {str(exc)[:120]}")
                except CONNECTION_ERRORS:
                    pass

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _dispatch

    # -- static ---------------------------------------------------------------

    def _static(self, path: str) -> None:
        if path == "/":
            relative = "index.html"
        elif path.startswith("/static/"):
            relative = unquote(path[len("/static/") :])
        else:
            return self._error(404, "Not found.")
        target = safe_join(self.server.static_dir, relative)
        if target is None:
            if relative == "index.html":
                return self._send(200, FALLBACK_PAGE, MIME[".html"], csp=FALLBACK_CSP)
            return self._error(404, "Not found.")
        self._send(200, target.read_bytes(), MIME.get(target.suffix.lower(), "application/octet-stream"))

    # -- API ------------------------------------------------------------------

    def _api(self, path: str, query: dict[str, list[str]]) -> None:
        method = self.command
        if path == "/api/whep" and method == "POST":
            return self._whep_offer(query)
        if path.startswith("/api/whep/") and method == "DELETE":
            return self._whep_delete(path[len("/api/whep/") :])
        if path == "/api/action":
            if method != "POST":
                return self._error(405, "Use POST.")
            return self._action()
        if method not in ("GET", "HEAD"):
            self.close_connection = True
            return self._error(405, "Use GET.")
        if path == "/api/state":
            # Fresh, not the hub's copy: a client that just acted must see its own change.
            return self._send(200, self.server.hub.fresh_json().encode(), "application/json")
        if path == "/api/presets/catalog":
            return self._json(200, presets_catalog.load().as_json())
        if path == "/api/events":
            return self._events()
        if path == "/api/preview.jpg":
            return self._preview_still(query)
        if path == "/api/preview.mjpg":
            return self._preview_stream(query)
        if path == "/api/whep":
            return self._json(200, self.server.whep.snapshot())
        if path == "/api/director/sheet.jpg":
            return self._director_sheet()
        if path.startswith("/api/deck/"):
            return self._deck(path[len("/api/deck/") :], query)
        if path == "/api/slots/profiles.json":
            slots = self.server.rig.slots
            if slots is None:
                return self._error(404, "This rig has no time-of-day profiles.")
            return self._json(200, slots.export())
        if path == "/api/hotkeys/bindings.json":
            hotkeys = self.server.rig.hotkeys
            if hotkeys is None:
                return self._error(404, "This rig has no hotkeys.")
            return self._json(200, hotkeys.export())
        if path.startswith("/api/crashes/") and path.endswith(".json"):
            return self._crash_report(unquote(path[len("/api/crashes/") : -len(".json")]))
        if path.startswith("/api/clips/") and path.endswith(".wav"):
            return self._clip(path[len("/api/clips/") : -len(".wav")])
        return self._error(404, "Unknown endpoint.")

    def _deck(self, name: str, query: dict[str, list[str]]) -> None:
        deck = self.server.rig.deck
        if deck is None:
            return self._error(404, "This rig has no Stream Deck.")
        if name == "palette":
            return self._json(200, {"palette": deck.palette()})
        if name == "profile.json":
            return self._json(200, deck.export())
        if name == "key.png":
            page = query.get("page", [""])[0]
            if len(page) > 40:
                return self._error(400, "page is not a valid id.")
            png = deck.key_png(page or None, clamp_int(query, "key", 0, 0, 255), clamp_int(query, "size", 96, 16, 256))
            return self._send(200, png, MIME[".png"])
        return self._error(404, "Unknown endpoint.")

    def _action(self) -> None:
        self.close_connection = True  # until the body is read, the connection cannot be reused
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return self._error(415, "Send Content-Type: application/json.")
        if self.headers.get("Transfer-Encoding"):
            return self._error(411, "Send a Content-Length.")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._error(411, "Send a Content-Length.")
        if length < 0 or length > MAX_BODY:
            return self._error(413, f"The body may be at most {MAX_BODY // 1024} KiB.")
        raw = self.rfile.read(length)
        self.close_connection = False
        try:
            body = json.loads(raw)
        except ValueError:
            return self._error(400, "The body is not valid JSON.")
        if not isinstance(body, dict) or not isinstance(body.get("action"), str):
            return self._error(400, 'Send an object like {"action": "audio.mute", "on": true}.')
        params = {k: v for k, v in body.items() if k != "action"}
        self._params_seen = tuple(params)
        began = time.monotonic()
        try:
            message = self.server.rig.act(body["action"], params)
        except ActionError as exc:
            self._observe(body["action"], began, exc.status)
            raise
        except Exception:
            self._observe(body["action"], began, 500)
            raise
        self._observe(body["action"], began, None)
        seq = self.server.hub.mark()  # before the kick: the snapshot the kick makes is the first one numbered `seq`
        self.server.hub.kick()
        self._json(200, {"ok": True, "seq": seq, **({"message": message} if message else {})})

    def _observe(self, action: str, began: float, status: int | None) -> None:
        seconds = time.monotonic() - began
        if os.environ.get(ACTION_LOG_ENV) or seconds >= SLOW_ACTION_S:
            # keys only: a value may be a stream key or a token
            keys = ",".join(sorted(self._params_seen))
            print(
                f"streamstack console: action {action[:40]} -> {status or 200} in {seconds * 1000:.0f} ms"
                f" [{keys}]{' SLOW' if seconds >= SLOW_ACTION_S else ''}",
                file=sys.stderr,
                flush=True,
            )
        diagnostics = self.server.rig.diagnostics
        if diagnostics is not None:
            diagnostics.observe_action(action, seconds, status)

    def _supplied_token(self, query: dict[str, list[str]]) -> str | None:
        header = self.headers.get("Authorization") or ""
        bearer = header[7:].strip() if header[:7].lower() == "bearer " else None
        return self.headers.get(TOKEN_HEADER) or bearer or (query.get("token") or [None])[0]

    def _metrics(self, query: dict[str, list[str]]) -> None:
        """Prometheus text, for a caller with the Console token or the scrape token; absent while telemetry is off."""
        if self.command not in ("GET", "HEAD"):
            self.close_connection = True
            return self._error(405, "Use GET.")
        supplied = self._supplied_token(query)
        diagnostics = self.server.rig.diagnostics
        allowed = self.server.token_ok(supplied) or (diagnostics is not None and diagnostics.token_ok(supplied))
        if not allowed:
            self.close_connection = True
            return self._error(401, "Missing or wrong token.")
        if diagnostics is None or not diagnostics.enabled:
            return self._error(404, "Telemetry is off. Turn it on under System, or set STREAMSTACK_TELEMETRY=true.")
        body = diagnostics.render(json.loads(self.server.hub.state_json())).encode()
        self._send(200, body, "text/plain; version=0.0.4; charset=utf-8")

    def _crash_report(self, report_id: str) -> None:
        report = crash.read_report(report_id)
        if report is None:
            return self._error(404, "No such crash report.")
        self._json(200, report)

    def _events(self) -> None:
        hub = self.server.hub
        client = hub.subscribe()
        try:
            self._begin(200, "text/event-stream", None, X_Accel_Buffering="no")
            self.wfile.write(b"retry: 2000\n\n")
            while not self.server.stopping.is_set():
                frame = client.get(PING_SECONDS)
                if frame is None:
                    break
                self.wfile.write(frame or b": ping\n\n")
        finally:
            hub.unsubscribe(client)

    # -- previews and clips -----------------------------------------------------

    @staticmethod
    def _preview_args(query: dict[str, list[str]]) -> tuple[str, int, int]:
        source = query.get("source", ["program"])[0]
        if not source or len(source) > 120 or any(ord(c) < 32 for c in source):
            raise ActionError("source is not a valid name.", 400)
        return source, clamp_int(query, "width", 640, 160, 1280), clamp_int(query, "fps", 5, 1, 15)

    def _preview_still(self, query: dict[str, list[str]]) -> None:
        source, width, _ = self._preview_args(query)
        frame = self.server.rig.frame(source, width)
        if not frame:
            return self._error(503, "No picture from this source right now.")
        self._send(200, frame, "image/jpeg")

    def _whep_offer(self, query: dict[str, list[str]]) -> None:
        """WHEP: POST an SDP offer, get 201, the answer and the session's URL in Location (docs/whep-preview.md)."""
        self.close_connection = True  # until the body is read, the connection cannot be reused
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/sdp":
            return self._error(415, "Send Content-Type: application/sdp.")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._error(411, "Send a Content-Length.")
        if length < 0 or length > MAX_SDP:
            return self._error(413, f"The offer may be at most {MAX_SDP // 1024} KiB.")
        raw = self.rfile.read(length)
        source, width, fps = self._preview_args(query)
        try:
            offer = raw.decode("utf-8")
            session_id, answer = self.server.whep.offer(offer, source, width, fps)
        except UnicodeDecodeError:
            return self._error(400, "The offer is not UTF-8 text.")
        except WhepError as exc:
            return self._error(exc.status, exc.message)
        self.close_connection = False
        self._send(201, answer.encode(), "application/sdp", Location=f"/api/whep/{session_id}")

    def _whep_delete(self, session_id: str) -> None:
        if not self.server.whep.delete(session_id) and not self.server.whep.valid_id(session_id):
            return self._error(404, "No such preview session.")
        # Ending a session that is already gone (the server dropped it when the connection failed, and the page
        # reports the same failure) is a success: a 404 here is logged by the browser as a console error on any page.
        self._send(200, b"", "application/sdp")

    def _director_sheet(self) -> None:
        sheet = self.server.rig.director_sheet()
        if not sheet:
            return self._error(404, "Claude has not been shown a contact sheet yet.")
        self._send(200, sheet, "image/jpeg")

    def _preview_stream(self, query: dict[str, list[str]]) -> None:
        source, width, fps = self._preview_args(query)
        rig, period = self.server.rig, 1.0 / fps
        frame = rig.frame(source, width)
        if not frame:
            return self._error(503, "No picture from this source right now.")
        self._begin(200, "multipart/x-mixed-replace; boundary=frame", None)
        while not self.server.stopping.is_set():
            began = time.monotonic()
            if frame:
                head = f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n".encode()
                self.wfile.write(head + frame + b"\r\n")  # a vanished client raises here and ends the loop
            time.sleep(max(0.0, period - (time.monotonic() - began)))
            frame = rig.frame(source, width)

    def _clip(self, clip_id: str) -> None:
        path = self.server.rig.clip_path(clip_id) if CLIP_ID.fullmatch(clip_id) else None
        if path is None or not path.is_file():
            return self._error(404, "That recording is gone.")
        data = path.read_bytes()
        status, start, end = 200, 0, len(data) - 1
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if match and (match.group(1) or match.group(2)):
            if match.group(1):
                start = int(match.group(1))
                end = min(int(match.group(2)), end) if match.group(2) else end
            else:
                start = max(0, len(data) - int(match.group(2)))
            if start > end or start >= len(data):
                return self._error(416, "Range not satisfiable.")
            status = 206
        extra = {"Accept_Ranges": "bytes"}
        if status == 206:
            extra["Content_Range"] = f"bytes {start}-{end}/{len(data)}"
        self._send(status, data[start : end + 1], MIME[".wav"], **extra)
