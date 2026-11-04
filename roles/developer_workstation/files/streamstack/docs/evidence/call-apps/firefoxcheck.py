"""Same check as devicecheck.py, in the system Firefox (the one a user runs), headless, with its own throw-away profile.

Usage: python -I firefoxcheck.py <out-dir> <firefox-binary> [--hide /dev/video0,/dev/video1]
--hide masks those device nodes from this one Firefox's mount namespace (bubblewrap, nothing on the host changes). It is
for a Cam Link with no HDMI signal: Firefox's V4L2 capability probe of it never returns, which stalls every camera
request in the browser, StreamStack Camera included (see docs/call-apps.md).
Without --hide the script reports that stall as a timeout.
Serves devicecheck.html on 127.0.0.1 (a secure context, one request, closed after the report), runs Firefox on it with
the permission prompt switched off in the profile, and writes firefox-streamstack-devices.json and .png (the preview
frame as the page read it back from the StreamStack Camera track). The profile is deleted at the end. Firefox is
stopped through the process group it was started in.
"""

import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

out = Path(sys.argv[1])
binary = sys.argv[2]
hide = sys.argv[4].split(",") if len(sys.argv) > 4 and sys.argv[3] == "--hide" else []
out.mkdir(parents=True, exist_ok=True)
page = (Path(__file__).parent / "devicecheck.html").read_bytes()
report: dict = {}
done = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        report.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(204)
        self.end_headers()
        done.set()

    def log_message(self, *args):
        return


server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
profile = Path(tempfile.mkdtemp(prefix="ss-ff-"))
(profile / "user.js").write_text(
    "\n".join(
        [
            'user_pref("media.navigator.permission.disabled", true);',
            'user_pref("permissions.default.camera", 1);',
            'user_pref("permissions.default.microphone", 1);',
            'user_pref("media.autoplay.default", 0);',
            'user_pref("browser.shell.checkDefaultBrowser", false);',
            'user_pref("datareporting.policy.dataSubmissionEnabled", false);',
        ]
    )
    + "\n"
)
mask = (
    ["bwrap", "--dev-bind", "/", "/"] + [a for node in hide for a in ("--ro-bind", "/dev/null", node)] if hide else []
)
proc = subprocess.Popen(
    [
        *mask,
        binary,
        "--headless",
        "--no-remote",
        "--profile",
        str(profile),
        f"http://127.0.0.1:{server.server_address[1]}/",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
try:
    if not done.wait(60):
        raise SystemExit("no report within 60 s: Firefox is stuck probing a camera (see --hide in the header)")
finally:
    # Firefox starts its own children; stop the whole group this script started (its own PID group, nothing else).
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        try:
            proc.wait(10)
            break
        except subprocess.TimeoutExpired:
            continue
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # anything the leader left behind
    except ProcessLookupError:
        pass
    server.shutdown()
    shutil.rmtree(profile, ignore_errors=True)
png = report.pop("png", "")
if png.startswith("data:image/png;base64,"):
    (out / "firefox-streamstack-devices.png").write_bytes(base64.b64decode(png.split(",", 1)[1]))
report["hiddenFromFirefox"] = hide
(out / "firefox-streamstack-devices.json").write_text(json.dumps(report, indent=1) + "\n")
print(json.dumps(report, indent=1))
