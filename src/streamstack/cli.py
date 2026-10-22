"""StreamStack: get the camera, lighting and voice right before a call or stream.

Settings come from /etc/streamstack/streamstack.env and
~/.config/streamstack/streamstack.env; everything here runs as the desktop
user and never needs root.

    streamstack doctor          check every device and say what to pick in Teams/Zoom
    streamstack studio          start the voice chain and OBS with the virtual camera
    streamstack stop            stop OBS and the camera bridge; calls use the raw camera
    streamstack status          one line (or a panel icon) for what calls receive
    streamstack auto on|off     the auto-cinematographer and auto-audio (Claude)
    streamstack cameras         every live camera and every mode it offers
    streamstack preview [cam]   watch a camera feed (canon, camlink, program, /dev/videoN)
    streamstack mic-check [s]   record a few seconds and play them back
    streamstack mic-revive      bring back a stalled microphone (suspend/resume, then a card profile cycle)
    streamstack voice [preset]  list or load a voice preset (bass, treble, pitch)
    streamstack console         the visual control surface: faders, meters, scenes, live picture
    streamstack canon-bridge    run the Canon live view -> loopback camera bridge
    streamstack setup           seed this user's OBS profile and voice presets
    streamstack call-settings   the Teams and Zoom settings that match this rig
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from streamstack import __version__, config, micwatch, system
from streamstack.config import camera_external, load_settings
from streamstack.system import (
    CAMLINK_LINK,
    CANON_USB_VENDOR,
    EE_SOURCE,
    FFMPEG_FORMATS,
    audio_sources,
    camera_modes,
    capture_devices,
    current_format,
    detach,
    easyeffects_running,
    ee_presets,
    find_camlink,
    have,
    loopback_devices,
    loopback_has_producer,
    mode_label,
    opened_by,
    run,
    usb_products,
    user_unit_active,
    video_name,
)

OBS_NAME = "StreamStack"
AUTO_UNIT = "streamstack-auto.service"
CANON_UNIT = "streamstack-canon.service"

OK, WARN, FAIL = "ok", "warn", "FAIL"
_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
_COLORS = {OK: "32", WARN: "33", FAIL: "31"}


def report(state: str, message: str, hint: str = "") -> None:
    tag = f"[{state:>4}]"
    if _USE_COLOR:
        tag = f"\033[{_COLORS[state]}m{tag}\033[0m"
    print(f"{tag} {message}")
    if hint:
        print(f"       -> {hint}")


def auto_running() -> bool:
    return user_unit_active(AUTO_UNIT)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def check(group: str, cid: str, state: str, title: str, detail: str = "", hint: str = "") -> dict[str, str]:
    return {"id": cid, "group": group, "state": state, "title": title, "detail": detail, "hint": hint}


def run_checks(cfg: dict[str, str] | None = None, *, stall_probe: bool = True) -> list[dict[str, str]]:
    """Every doctor check as structured data: id, group (video|audio|apps), state (ok|warn|fail), title, detail, hint.

    `streamstack doctor` prints these and the Console shows them as cards. `stall_probe` adds the microphone
    stall check, which samples the microphone's ALSA PCM for about 5 s; the Console turns it off because its audio
    controller watches the microphone continuously.
    """
    cfg = cfg or load_settings()
    checks: list[dict[str, str]] = []
    camera = cfg["STREAMSTACK_CAMERA_DEVICE"]
    program = cfg["STREAMSTACK_PROGRAM_DEVICE"]

    for device, label, role, cid in (
        (camera, cfg["STREAMSTACK_CAMERA_LABEL"], "camera bridge", "camera-node"),
        (program, cfg["STREAMSTACK_PROGRAM_LABEL"], "OBS virtual camera", "program-node"),
    ):
        name = video_name(device)
        if not name:
            checks.append(
                check(
                    "video",
                    cid,
                    "fail",
                    f"{device} ({role}) is missing",
                    hint="load v4l2loopback with the StreamStack nodes (see the README), or reboot after installing",
                )
            )
            continue
        if name != label:
            checks.append(
                check(
                    "video",
                    f"{cid}-name",
                    "warn",
                    f"{device} is named {name!r}, expected {label!r}",
                    hint="another v4l2loopback configuration is loaded; see Troubleshooting in the README",
                )
            )
        if loopback_has_producer(device):
            checks.append(
                check("video", cid, "ok", f"{label} ({device}) is live", current_format(device) or "format unknown")
            )
        else:
            if role == "OBS virtual camera":
                hint = "start it: streamstack studio (OBS Start Virtual Camera)"
            elif camera_external(cfg):
                hint = "this camera is fed by another tool on this host; start its service"
            else:
                hint = f"start it: systemctl --user start {CANON_UNIT}"
            checks.append(
                check(
                    "video", cid, "warn", f"{label} ({device}) has no picture yet; browsers will not list it", hint=hint
                )
            )

    canon = usb_products(CANON_USB_VENDOR)
    if canon:
        checks.append(check("video", "canon-usb", "ok", "Canon on USB", ", ".join(canon)))
        if (
            cfg["STREAMSTACK_CANON_BRIDGE"] != "none"
            and not camera_external(cfg)
            and not user_unit_active(CANON_UNIT)
            and not loopback_has_producer(camera)
        ):
            checks.append(
                check(
                    "video",
                    "canon-bridge",
                    "warn",
                    "the Canon bridge is not running",
                    hint=f"systemctl --user start {CANON_UNIT}",
                )
            )
    elif not camera_external(cfg) and cfg["STREAMSTACK_CANON_BRIDGE"] != "none":
        checks.append(
            check(
                "video",
                "canon-usb",
                "warn",
                "no Canon camera on USB",
                hint="check the cable, power, and that the dial is not on a movie-only mode",
            )
        )

    # OBS's virtual camera takes whichever loopback node is free first, which
    # can be the camera node when its bridge is not running yet.
    strays = [
        d
        for d in opened_by("obs", loopback_devices())
        if d not in {str(Path(camera).resolve()), str(Path(program).resolve())}
    ]
    if strays:
        checks.append(
            check(
                "video",
                "obs-loopback",
                "warn",
                f"OBS holds loopback camera {', '.join(strays)}, not {program}",
                hint="stop the virtual camera, close other camera tools, then streamstack studio",
            )
        )
    elif not opened_by("obs", [program]) and opened_by("obs", [camera]) and not loopback_has_producer(program):
        checks.append(
            check(
                "video",
                "obs-loopback",
                "warn",
                f"OBS may be feeding {camera} instead of {program}",
                hint="stop the virtual camera, start the camera bridge first, then Start Virtual Camera",
            )
        )

    camlink = find_camlink()
    if camlink:
        fmt = current_format(camlink)
        checks.append(
            check(
                "video",
                "camlink",
                "ok",
                f"Elgato Cam Link on {camlink}",
                fmt or "no HDMI signal format reported",
                "" if fmt else "turn on the HDMI camera and disable its on-screen display",
            )
        )
    else:
        checks.append(
            check(
                "video",
                "camlink",
                "warn",
                "no Elgato Cam Link 4K found",
                hint="plug it into a USB 3 port (blue) directly, not through a USB 2 hub",
            )
        )

    sources = audio_sources()
    mic = system.pick_microphone(sources, cfg["STREAMSTACK_MICROPHONE"], system.default_device("source"))
    if mic:
        checks.append(check("audio", "microphone", "ok", "call microphone", sources.get(mic) or mic))
    elif cfg["STREAMSTACK_MICROPHONE"]:
        checks.append(
            check(
                "audio",
                "microphone",
                "warn",
                f"microphone {cfg['STREAMSTACK_MICROPHONE']!r} is not connected",
                hint="check USB and power",
            )
        )
    else:
        checks.append(
            check(
                "audio",
                "microphone",
                "warn",
                "no microphone found",
                hint="plug in a USB microphone; the Wave XLR also needs +48V for condenser mics",
            )
        )
    if mic and stall_probe:
        probe = _watch_microphone(mic)
        if stall_item := micwatch.check_item(probe.stalled, probe.detail):
            checks.append(stall_item)
    if EE_SOURCE in sources:
        checks.append(check("audio", "voice-chain", "ok", "Easy Effects Source (processed voice) is available"))
    elif easyeffects_running():
        checks.append(
            check(
                "audio",
                "voice-chain",
                "warn",
                "Easy Effects is running but its virtual source is missing",
                hint="restart it: easyeffects -q; streamstack studio",
            )
        )
    else:
        checks.append(
            check(
                "audio",
                "voice-chain",
                "fail",
                "Easy Effects is not running, so there is no processed voice",
                hint="streamstack studio",
            )
        )
    if any("cam_link" in name.lower() for name in sources):
        checks.append(
            check("audio", "camlink-audio", "ok", "Cam Link HDMI audio exists; never pick it as a call microphone")
        )

    for tool, why in (
        ("obs", "OBS Studio"),
        ("easyeffects", "Easy Effects"),
        ("qpwgraph", "PipeWire patchbay"),
        ("v4l2-ctl", "camera controls"),
        ("gphoto2", "Canon tether"),
        ("ffplay", "previews"),
    ):
        checks.append(
            check("apps", f"app-{tool}", "ok" if have(tool) else "fail", why, "installed" if have(tool) else "missing")
        )
    browsers = [
        b for b in ("microsoft-edge", "microsoft-edge-stable", "google-chrome", "chromium", "firefox") if have(b)
    ]
    checks.append(
        check(
            "apps",
            "browser",
            "ok" if browsers else "warn",
            "browser for Teams web",
            ", ".join(browsers) or "none found",
        )
    )
    checks.append(
        check(
            "apps",
            "zoom",
            "ok" if have("zoom") else "warn",
            "Zoom desktop client",
            "installed" if have("zoom") else "not installed (zoom.us web client works too)",
        )
    )
    checks.append(
        check("apps", "auto-mode", "ok", "auto mode", f"{'on' if auto_running() else 'off'} (streamstack auto on|off)")
    )
    return checks


def summarize(checks: list[dict[str, str]]) -> str:
    """ok, warn or fail: the worst state among the checks."""
    states = {c["state"] for c in checks}
    return "fail" if "fail" in states else "warn" if "warn" in states else "ok"


GROUP_TITLES = {"video": "Video", "audio": "Audio", "apps": "Apps"}


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_settings()
    checks = run_checks(cfg)
    for number, (group, title) in enumerate(GROUP_TITLES.items()):
        print(("\n" if number else "") + title)
        for item in (c for c in checks if c["group"] == group):
            message = f"{item['title']}: {item['detail']}" if item["detail"] else item["title"]
            report("FAIL" if item["state"] == "fail" else item["state"], message, item["hint"])
    print("\nPick these in Teams / Zoom")
    print(
        f"  Camera:     {cfg['STREAMSTACK_PROGRAM_LABEL']}   "
        f"(graded OBS output; or {cfg['STREAMSTACK_CAMERA_LABEL']} raw)"
    )
    print("  Microphone: Easy Effects Source")
    print("  Speaker:    headphones - not the laptop speakers")
    print("  Run `streamstack call-settings` for the in-app toggles to turn off.")
    return 1 if any(c["state"] == "fail" for c in checks) else 0


def cmd_auto(args: argparse.Namespace) -> int:
    """Turn the auto-cinematographer/auto-audio mode on or off."""
    from streamstack import auto

    if args.action == "on":
        auto.cmd_check(args)
        result = run(["systemctl", "--user", "start", AUTO_UNIT], timeout=30)
        if not result or result.returncode != 0:
            print("streamstack: could not start auto mode", file=sys.stderr)
            return 1
        print("Auto mode on: exposure and mic gain every few seconds, a Claude pass every few minutes.")
        return 0
    if args.action == "off":
        run(["systemctl", "--user", "stop", AUTO_UNIT], timeout=30)
        print("Auto mode off; the current look and levels stay as they are (streamstack auto reset undoes them).")
        return 0
    if args.action == "once":
        return auto.cmd_once(args)
    if args.action == "check":
        return auto.cmd_check(args)
    if args.action == "reset":
        return auto.cmd_reset(args)
    print(f"Auto mode: {'on' if auto_running() else 'off'}")
    if auto.load_baseline():
        print("  changes since the last reset can be undone: streamstack auto reset")
    journal = config.state_dir() / "auto.jsonl"
    try:
        lines = journal.read_text().splitlines()[-args.lines :]
    except OSError:
        lines = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        print(f"  {entry.get('time', '')}  {entry.get('event', ''):<16} {describe(entry)}")
    return 0


def describe(entry: dict) -> str:
    """One readable line for an auto.jsonl entry."""
    event = entry.get("event")
    if event == "mic-gain":
        return f"{entry['before_db']:+.1f} dB -> {entry['after_db']:+.1f} dB"
    if event == "exposure":
        return f"{entry['source']}: {entry.get('metering', 'frame')} luma {entry['stats'].get('YAVG', 0):.0f}"
    if event == "ai":
        return f"({entry.get('confidence', 0):.1f}) {entry.get('proposal', {}).get('notes', '')}"
    return str(entry.get("error") or entry.get("reason") or "")


def call_camera(cfg: dict[str, str], studio: bool) -> str:
    """What calls get for a camera: graded (OBS virtual camera), obs-idle, raw or off."""
    if studio and loopback_has_producer(cfg["STREAMSTACK_PROGRAM_DEVICE"]):
        return "graded"
    if studio:
        return "obs-idle"
    camera = cfg["STREAMSTACK_CAMERA_DEVICE"]
    return "raw" if Path(camera).exists() and loopback_has_producer(camera) else "off"


def cmd_status(args: argparse.Namespace) -> int:
    """One line for a panel button: what calls get for camera and microphone."""
    cfg = load_settings()
    camera = call_camera(cfg, system.running("obs")).replace("obs-idle", "OBS idle")
    mic = "fx" if EE_SOURCE in audio_sources() else "raw"
    detail = f"camera for calls: {camera}; microphone: {'Easy Effects Source' if mic == 'fx' else 'raw mic'}"
    if auto_running():
        detail += "; auto-cinematographer on"
    if args.icon:
        # tint2 execp_has_icon: the icon path, then the (empty) text; the
        # details go to stderr, which tint2 shows in the tooltip.
        state = {"graded": "live", "raw": "raw", "off": "off"}.get(camera, "warn")
        if mic != "fx" and state in ("live", "raw"):
            state = "warn"
        icon = config.data_dir() / f"icons/streamstack-{state}.png"
        print(icon if icon.exists() else icon.with_suffix(".svg"))
        print()
        print(f"StreamStack - {detail}", file=sys.stderr)
        return 0
    print(f"STUDIO {camera} / mic {mic}" if args.short else detail)
    return 0


def cmd_call_settings(args: argparse.Namespace) -> int:
    cfg = load_settings()
    print(f"""Microsoft Teams (web in Edge/Chrome, or the Teams PWA)
  Start `streamstack studio` FIRST: browsers list a loopback camera only while it has a picture.
  Settings > Devices
    Camera ............. {cfg["STREAMSTACK_PROGRAM_LABEL"]}
    Microphone ......... Easy Effects Source
    Speaker ............ headphones
    Noise suppression .. Low (Easy Effects already removes noise; High/Auto fights it)
  In the meeting: no background effects or blur - do framing and grading in OBS.

Zoom (desktop client or web)
  Settings > Video
    Camera ............. {cfg["STREAMSTACK_PROGRAM_LABEL"]}
    HD ................. on
    Mirror my video .... off when text or signs are in shot
    Adjust for low light, Touch up appearance ... off (OBS handles it)
  Settings > Audio
    Microphone ......... Easy Effects Source
    Automatically adjust microphone volume ... off (StreamStack auto mode levels the mic)
    Suppress background noise ... Low
    Original sound for musicians ... on, with echo cancellation on unless you wear headphones

Both
  Test call first: Teams "Make a test call", Zoom zoom.us/test.
  Headphones stop echo. Speak at arm's length with the mic just off-axis.
  `streamstack mic-check` lets you hear the processed voice before anyone else does.
""")
    return 0


def cmd_cameras(args: argparse.Namespace) -> int:
    cfg = load_settings()
    roles = {
        str(Path(cfg["STREAMSTACK_CAMERA_DEVICE"]).resolve()): "camera bridge",
        str(Path(cfg["STREAMSTACK_PROGRAM_DEVICE"]).resolve()): "StreamStack Camera (OBS)",
    }
    camlink = find_camlink()
    if camlink:
        roles[str(Path(camlink).resolve())] = "capture card"
    devices = capture_devices()
    if not devices:
        print("No camera is delivering video. Check `streamstack doctor`.")
        return 1
    for device, name in devices:
        role = roles.get(str(Path(device).resolve()), "")
        print(f"{device}  {name}" + (f"  [{role}]" if role else ""))
        modes = camera_modes(device)
        current = current_format(device)
        if current:
            print(f"    now: {current}")
        for mode in modes:
            print(f"    {mode_label(mode)}")
        if modes:
            print(f"    preview: streamstack preview {device} --mode '{mode_label(modes[0])}'")
    return 0


def preview_argv(label: str, device: str, mode: str = "", grid: bool = False) -> list[str]:
    argv = [
        "ffplay",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "nobuffer",
        "-window_title",
        f"StreamStack preview - {label} ({device})",
        "-f",
        "v4l2",
    ]
    if mode:
        match = re.fullmatch(r"(\d+)x(\d+)(?:@([\d.]+))?(?:\s+(\w+))?", mode.strip())
        if not match:
            raise ValueError("--mode is WIDTHxHEIGHT[@FPS] [FOURCC], e.g. '1920x1080@30 MJPG'")
        width, height, fps, fourcc = match.groups()
        if fourcc:
            argv += ["-input_format", FFMPEG_FORMATS.get(fourcc.upper(), fourcc.lower())]
        argv += ["-video_size", f"{width}x{height}"]
        if fps:
            argv += ["-framerate", fps]
    argv += ["-i", device]
    if grid:
        argv += ["-vf", "drawgrid=w=iw/3:h=ih/3:t=1:c=white@0.5"]
    return argv


def cmd_preview(args: argparse.Namespace) -> int:
    cfg = load_settings()
    devices = {
        "canon": cfg["STREAMSTACK_CAMERA_DEVICE"],
        "program": cfg["STREAMSTACK_PROGRAM_DEVICE"],
        "camlink": find_camlink(),
    }
    device = devices.get(args.camera, args.camera)
    if not device or not Path(device).exists():
        print(f"streamstack: no device for {args.camera!r}", file=sys.stderr)
        return 1
    if not have("ffplay"):
        print("streamstack: ffplay is missing (install ffmpeg)", file=sys.stderr)
        return 1
    try:
        argv = preview_argv(args.camera, device, args.mode or "", args.grid)
    except ValueError as exc:
        print(f"streamstack: {exc}", file=sys.stderr)
        return 1
    os.execvp(argv[0], argv)
    return 0


def cmd_mic_check(args: argparse.Namespace) -> int:
    for tool in ("parecord", "paplay"):
        if not have(tool):
            print(f"streamstack: {tool} is missing (install pulseaudio-utils)", file=sys.stderr)
            return 1
    cfg = load_settings()
    sources = audio_sources()
    processed = EE_SOURCE if EE_SOURCE in sources else ""
    raw = system.pick_microphone(sources, cfg["STREAMSTACK_MICROPHONE"], system.default_device("source"))
    takes = []
    if args.raw and raw:
        takes.append(("raw microphone", raw))
    if processed:
        takes.append(("processed (Easy Effects Source)", processed))
    elif not takes:
        takes.append(("default input", "@DEFAULT_SOURCE@"))
    with tempfile.TemporaryDirectory(prefix="streamstack-") as tmp:
        recordings = []
        for label, source in takes:
            path = os.path.join(tmp, f"{len(recordings)}.wav")
            print(f"Recording {args.seconds:g}s from {label}. Talk now...")
            if system.record(source, args.seconds, path):
                recordings.append((label, path))
            else:
                print(f"  nothing captured from {source}")
        for label, path in recordings:
            print(f"Playing back: {label}")
            system.run(["paplay", path], timeout=args.seconds + 20)  # a wedged server must not hold the terminal
    return 0 if takes else 1


def _watch_microphone(mic: str) -> micwatch.StallProbe:
    """Sample the microphone's ALSA PCM once a second for a full window and judge it (blocks ~5 s)."""
    monitor = micwatch.StallMonitor()
    deadline = time.monotonic() + micwatch.WINDOW_S + 1.5
    while True:
        listing = system.run(["pactl", "-f", "json", "list", "sources"], timeout=micwatch.PACTL_TIMEOUT)
        probe = monitor.probe(mic, listing.stdout if listing and listing.returncode == 0 else "")
        if probe.state != "watching" or time.monotonic() >= deadline:
            return probe
        time.sleep(micwatch.SAMPLE_S)


def cmd_mic_revive(args: argparse.Namespace) -> int:
    """Revive a stalled microphone: suspend/resume the source, then cycle its card profile if that was not enough."""
    if not have("pactl"):
        print("streamstack: pactl is missing (install pulseaudio-utils)", file=sys.stderr)
        return 1
    cfg = load_settings()
    sources = audio_sources()
    mic = system.pick_microphone(sources, cfg["STREAMSTACK_MICROPHONE"], system.default_device("source"))
    if not mic:
        print("streamstack: no microphone found (plug it in, or set STREAMSTACK_MICROPHONE)", file=sys.stderr)
        return 1
    print(f"Microphone: {sources.get(mic) or mic}")
    print(f"Watching the microphone for {micwatch.WINDOW_S:g} seconds...", flush=True)
    probe = _watch_microphone(mic)
    print(f"  now: {probe.detail or 'could not tell whether it is stalled'}")
    if probe.stalled is False and not args.force:
        print("The microphone is delivering audio, nothing to revive (--force cycles it anyway).")
        return 0
    if probe.stalled is None and not args.force:
        print("Cannot tell whether the microphone is stalled, so it is left alone (--force cycles it anyway).")
        return 0

    monitor = micwatch.StallMonitor()
    result = micwatch.revive_mic(mic, say=lambda text: print(text, flush=True), stalled=lambda: monitor.verify(mic))
    print(f"Did: {', '.join(result.steps)}")
    print(result.message, file=sys.stdout if result.ok else sys.stderr)
    return 0 if result.ok else 1


def cmd_voice(args: argparse.Namespace) -> int:
    presets = ee_presets()
    if not args.preset:
        print("Voice presets (open Easy Effects to fine-tune bass, treble and pitch):")
        for name in presets:
            print(f"  {name}")
        if not presets:
            print("  none yet - run `streamstack setup`")
        return 0
    if args.preset not in presets:
        print(f"streamstack: unknown preset {args.preset!r}; run `streamstack voice` for the list", file=sys.stderr)
        return 1
    if not easyeffects_running():
        _start_easyeffects()
    result = run(["easyeffects", "-l", args.preset], timeout=15)
    if result is None or result.returncode != 0:
        print("streamstack: Easy Effects did not accept the preset", file=sys.stderr)
        return 1
    print(f"Loaded voice preset: {args.preset}")
    return 0


def _start_easyeffects() -> None:
    detach(["easyeffects", "--service-mode", "-w"])
    for _ in range(40):
        if EE_SOURCE in audio_sources():
            return
        time.sleep(0.25)


def cmd_studio(args: argparse.Namespace) -> int:
    cfg = load_settings()
    if not easyeffects_running():
        print("Starting Easy Effects (voice chain)...")
        _start_easyeffects()
    preset = cfg["STREAMSTACK_VOICE_PRESET"]
    if preset in ee_presets():
        run(["easyeffects", "-l", preset], timeout=15)
    camera = cfg["STREAMSTACK_CAMERA_DEVICE"]
    bridge = cfg["STREAMSTACK_CANON_BRIDGE"] != "none" and not camera_external(cfg)
    if bridge and usb_products(CANON_USB_VENDOR) and not user_unit_active(CANON_UNIT):
        print("Starting the Canon bridge...")
        run(["systemctl", "--user", "start", CANON_UNIT], timeout=40)
    if args.no_obs:
        return 0
    if system.running("obs"):
        print("OBS is already running; use Start Virtual Camera in its Controls dock.")
        return 0
    if auto_running():
        # Auto mode drives OBS through its local websocket; enable it while OBS is closed.
        from streamstack.obs import ensure_websocket

        ensure_websocket()
    # The OBS virtual camera claims the first free loopback node. Start it only
    # once the camera node has its producer, or OBS may take the camera's node.
    expect_camera = camera_external(cfg) or (bridge and usb_products(CANON_USB_VENDOR))
    live = loopback_has_producer(camera) if Path(camera).exists() else None
    for _ in range(80 if expect_camera else 0):
        if live:
            break
        time.sleep(0.25)
        live = loopback_has_producer(camera)
    argv = ["obs"]
    if live or not Path(camera).exists():
        argv.append("--startvirtualcam")
    else:
        print(
            f"{camera} has no picture yet, so OBS starts without the virtual camera (it would take {camera}).\n"
            "Once the camera is live, press Start Virtual Camera in OBS."
        )
    if (Path.home() / ".config/obs-studio/basic/profiles" / OBS_NAME).exists():
        argv += ["--profile", OBS_NAME, "--collection", OBS_NAME]
    print("Starting OBS" + (" with the virtual camera..." if "--startvirtualcam" in argv else "..."))
    detach(argv)
    print(f"In Teams/Zoom pick camera '{cfg['STREAMSTACK_PROGRAM_LABEL']}' and microphone 'Easy Effects Source'.")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop OBS and the Canon bridge; calls fall back to the raw camera."""
    cfg = load_settings()
    if system.running("obs"):
        print("Stopping OBS (it saves its scenes on the way out)...")
        run(["pkill", "-TERM", "-u", str(os.getuid()), "-x", "obs"])
        for _ in range(40):
            if not system.running("obs"):
                break
            time.sleep(0.25)
    if not camera_external(cfg) and user_unit_active(CANON_UNIT):
        print("Stopping the Canon bridge...")
        run(["systemctl", "--user", "stop", CANON_UNIT], timeout=20)
    if args.voice and easyeffects_running():
        print("Stopping Easy Effects...")
        run(["easyeffects", "-q"], timeout=15)
    print(f"In Teams/Zoom pick camera '{cfg['STREAMSTACK_CAMERA_LABEL']}' (raw) until the studio runs again.")
    return 0


def cmd_console(args: argparse.Namespace) -> int:
    from streamstack.console.launcher import run_console

    return run_console(
        demo=args.demo, port=args.port, window=not args.no_window, mini=args.mini, idle_exit=args.idle_exit
    )


def _canon_release_gvfs() -> None:
    # GVFS mounts PTP cameras on plug-in and then holds the USB interface.
    run(["gio", "mount", "-s", "gphoto2"], timeout=10)
    run(["pkill", "-u", str(os.getuid()), "-f", "gvfsd-gphoto2"], timeout=5)


def bridge_filter(width: str, height: str, fps: str) -> str:
    # Live view is 3:2 MJPEG; fill the 16:9 frame (crop, never letterbox) at a
    # constant rate so Teams/Zoom see a steady camera.
    return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps},format=yuyv422"


def cmd_canon_bridge(args: argparse.Namespace) -> int:
    cfg = load_settings()
    device = cfg["STREAMSTACK_CAMERA_DEVICE"]
    mode = cfg["STREAMSTACK_CANON_BRIDGE"]
    env = dict(os.environ, STREAMSTACK_CAMERA_DEVICE=device)
    if camera_external(cfg):
        print(
            f"streamstack: {device} is fed by another tool on this host (STREAMSTACK_CAMERA_EXTERNAL)", file=sys.stderr
        )
        return 1
    if not Path(device).exists():
        print(f"streamstack: {device} is missing; is v4l2loopback loaded?", file=sys.stderr)
        return 1
    if mode == "command":
        command = cfg["STREAMSTACK_CANON_COMMAND"]
        if not command:
            print("streamstack: STREAMSTACK_CANON_BRIDGE=command needs STREAMSTACK_CANON_COMMAND", file=sys.stderr)
            return 1
        os.execvpe("/bin/sh", ["/bin/sh", "-c", command], env)
    if mode != "gphoto2":
        print(f"streamstack: canon bridge mode {mode!r} is disabled", file=sys.stderr)
        return 1
    _canon_release_gvfs()
    vf = bridge_filter(cfg["STREAMSTACK_WIDTH"], cfg["STREAMSTACK_HEIGHT"], cfg["STREAMSTACK_FPS"])
    camera = subprocess.Popen(["gphoto2", "--stdout", "--capture-movie"], stdout=subprocess.PIPE)
    encoder = subprocess.Popen(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "mjpeg",
            "-i",
            "pipe:0",
            "-vf",
            vf,
            "-f",
            "v4l2",
            device,
        ],
        stdin=camera.stdout,
        env=env,
    )
    if camera.stdout:
        camera.stdout.close()

    def stop(signum: int, frame: object) -> None:
        for proc in (camera, encoder):
            if proc.poll() is None:
                proc.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    code = encoder.wait()
    stop(0, None)
    camera.wait(timeout=10)
    return code or camera.returncode or 0


def _seed(src: Path, dest: Path, force: bool, replacements: dict[str, str] | None = None) -> bool:
    if dest.exists() and not force:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = src.read_text()
    for old, new in (replacements or {}).items():
        text = text.replace(old, new)
    tmp = dest.with_name(dest.name + ".streamstack-tmp")
    tmp.write_text(text)
    tmp.replace(dest)
    return True


def cmd_setup(args: argparse.Namespace) -> int:
    cfg = load_settings()
    home = Path.home()
    share = config.data_dir()
    replacements = {
        "@HOME@": str(home),
        "@CAMERA_DEVICE@": cfg["STREAMSTACK_CAMERA_DEVICE"],
        "@CAMERA_LABEL@": cfg["STREAMSTACK_CAMERA_LABEL"],
        "@CAMLINK_DEVICE@": str(CAMLINK_LINK),
        "@WIDTH@": cfg["STREAMSTACK_WIDTH"],
        "@HEIGHT@": cfg["STREAMSTACK_HEIGHT"],
        "@FPS@": cfg["STREAMSTACK_FPS"],
        "@HALF_WIDTH@": str(int(cfg["STREAMSTACK_WIDTH"]) // 2),
        "@HALF_HEIGHT@": str(int(cfg["STREAMSTACK_HEIGHT"]) // 2),
        "@QUARTER_HEIGHT@": str(int(cfg["STREAMSTACK_HEIGHT"]) // 4),
    }
    seeded = []
    for preset in sorted((share / "easyeffects/input").glob("*.json")):
        if _seed(preset, home / ".local/share/easyeffects/input" / preset.name, args.force):
            seeded.append(f"voice preset {preset.stem}")
    obs = home / ".config/obs-studio/basic"
    if _seed(share / "obs/basic.ini", obs / "profiles" / OBS_NAME / "basic.ini", args.force, replacements):
        seeded.append("OBS profile StreamStack")
    if _seed(share / "obs/scenes.json", obs / "scenes" / f"{OBS_NAME}.json", args.force, replacements):
        seeded.append("OBS scene collection StreamStack")
    (home / "Videos/StreamStack").mkdir(parents=True, exist_ok=True)
    if not args.quiet:
        for item in seeded:
            print(f"seeded {item}")
        if not seeded:
            print("already set up (use --force to restore the StreamStack defaults)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="streamstack", description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--version", action="version", version=f"streamstack {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check cameras, microphone and apps").set_defaults(func=cmd_doctor)
    auto = sub.add_parser("auto", help="auto-cinematographer and auto-audio: on, off, status, once, reset, check")
    auto.add_argument("action", nargs="?", default="status", choices=["on", "off", "status", "once", "reset", "check"])
    auto.add_argument("--dry-run", action="store_true", help="with once: show Claude's proposal, change nothing")
    auto.add_argument("--lines", type=int, default=8, help="with status: how many recent actions to show")
    auto.set_defaults(func=cmd_auto)
    status = sub.add_parser("status", help="one-line camera/microphone state (for panel buttons)")
    status.add_argument("--short", action="store_true")
    status.add_argument("--icon", action="store_true", help="icon path for a panel button, details on stderr")
    status.set_defaults(func=cmd_status)
    sub.add_parser("call-settings", help="Teams and Zoom settings for this rig").set_defaults(func=cmd_call_settings)
    studio = sub.add_parser("studio", help="start Easy Effects, the Canon bridge and OBS")
    studio.add_argument("--no-obs", action="store_true", help="only start the voice chain and camera bridge")
    studio.set_defaults(func=cmd_studio)
    stop = sub.add_parser("stop", help="stop OBS and the Canon bridge (calls use the raw camera)")
    stop.add_argument("--voice", action="store_true", help="also stop Easy Effects")
    stop.set_defaults(func=cmd_stop)
    preview = sub.add_parser("preview", help="watch a camera feed")
    preview.add_argument("camera", nargs="?", default="program", help="canon, camlink, program, or a /dev path")
    preview.add_argument("--grid", action="store_true", help="overlay a rule-of-thirds grid for framing")
    preview.add_argument("--mode", help="capture mode from `streamstack cameras`, e.g. '1920x1080@30 MJPG'")
    preview.set_defaults(func=cmd_preview)
    console = sub.add_parser("console", help="open the Console: faders, meters, scenes and the live picture")
    console.add_argument("--demo", action="store_true", help="a simulated rig; nothing is touched")
    console.add_argument("--no-window", action="store_true", help="only serve; print the address")
    console.add_argument("--mini", action="store_true", help="the compact window to keep beside a call")
    console.add_argument("--port", type=int, default=0, help="listen port (default: any free port)")
    console.add_argument(
        "--idle-exit",
        type=float,
        metavar="SECONDS",
        help="quit after this long with no window (default: 600 with a window, never without)",
    )
    console.set_defaults(func=cmd_console)
    sub.add_parser("cameras", help="every live video device and each mode it offers").set_defaults(func=cmd_cameras)
    mic = sub.add_parser("mic-check", help="record and play back your voice")
    mic.add_argument("seconds", nargs="?", type=float, default=6.0)
    mic.add_argument("--raw", action="store_true", help="also record the unprocessed mic for an A/B comparison")
    mic.set_defaults(func=cmd_mic_check)
    revive = sub.add_parser("mic-revive", help="bring back a stalled microphone")
    revive.add_argument("--force", action="store_true", help="cycle the microphone even when it looks healthy")
    revive.set_defaults(func=cmd_mic_revive)
    voice = sub.add_parser("voice", help="list or load a voice preset")
    voice.add_argument("preset", nargs="?")
    voice.set_defaults(func=cmd_voice)
    sub.add_parser("canon-bridge", help="feed Canon live view into the loopback camera").set_defaults(
        func=cmd_canon_bridge
    )
    setup = sub.add_parser("setup", help="seed OBS and Easy Effects defaults for this user")
    setup.add_argument("--force", action="store_true", help="overwrite with the StreamStack defaults")
    setup.add_argument("--quiet", action="store_true")
    setup.set_defaults(func=cmd_setup)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
