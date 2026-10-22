#!/usr/bin/env python3
"""A stand-in for the `gphoto2` command, for tests. Never touches USB.

State (FAKE_GPHOTO2_STATE, a JSON file) holds the settings. Like the real CLI it can run live view
(`--stdout --capture-movie`) or answer configuration calls, but not both at once: while a live view process runs
(its pid is in FAKE_GPHOTO2_STATE + '.live'), config calls fail with the "claim the USB device" error.
"""

import json
import os
import signal
import sys
import time
from pathlib import Path

STATE = Path(os.environ["FAKE_GPHOTO2_STATE"])
LIVE = Path(str(STATE) + ".live")
LOG = Path(str(STATE) + ".log")


def log(line: str) -> None:
    with LOG.open("a") as fh:
        fh.write(line + "\n")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def live_running() -> bool:
    try:
        return alive(int(LIVE.read_text()))
    except (OSError, ValueError):
        return False


def main(argv: list[str]) -> int:
    state = json.loads(STATE.read_text())
    if "--stdout" in argv and "--capture-movie" in argv:
        if state.get("busy"):
            sys.stderr.write("*** Error: Could not claim the USB device ***\n")
            return 1
        LIVE.write_text(str(os.getpid()))
        log("live-start")
        running = True

        def stop(signum: int, frame: object) -> None:
            nonlocal running
            running = False

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        frame = Path(os.environ["FAKE_GPHOTO2_FRAME"]).read_bytes()
        period = 1.0 / float(os.environ.get("FAKE_GPHOTO2_FPS", "30"))
        out = sys.stdout.buffer
        try:
            while running:
                out.write(frame)
                out.flush()
                time.sleep(period)
        except BrokenPipeError:
            return 0
        finally:
            LIVE.unlink(missing_ok=True)
            log("live-stop")
        return 0
    if live_running():
        sys.stderr.write("*** Error: Could not claim the USB device ***\n")
        return 1
    settings = state["settings"]
    i = 1
    while i < len(argv):
        flag, arg = argv[i], argv[i + 1]
        i += 2
        if flag == "--get-config":
            if arg not in settings:
                sys.stderr.write(f"Error: Unknown configuration entry {arg}\n")
                return 1
            s = settings[arg]
            print(f"Label: {s['label']}\nReadonly: {1 if s.get('readonly') else 0}\nType: {s['type']}")
            print(f"Current: {s['value']}")
            for n, c in enumerate(s.get("choices", [])):
                print(f"Choice: {n} {c}")
            print("END")
        elif flag == "--set-config-value":
            key, _, value = arg.partition("=")
            if key not in settings:
                sys.stderr.write(f"Error: Unknown configuration entry {key}\n")
                return 1
            s = settings[key]
            if s.get("readonly"):
                sys.stderr.write("Error: Read-only widget\n")
                return 1
            if s.get("choices") and value not in s["choices"]:
                sys.stderr.write(f"Error: Choice {value} not found within list of choices\n")
                return 1
            s["value"] = value
            STATE.write_text(json.dumps(state))
            log(f"set {key}={value}")
        else:
            sys.stderr.write(f"Error: unsupported {flag}\n")
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
