"""StreamStack: get the camera, lighting and voice right before a call or stream.

Settings come from /etc/streamstack/streamstack.env and
~/.config/streamstack/streamstack.env; everything here runs as the desktop
user and never needs root.

    streamstack doctor          check every device and say what to pick in Teams/Zoom
    streamstack studio          start the voice chain and the Canon bridge (the Console makes the program camera)
    streamstack stop            stop the camera bridge; calls use the raw camera
    streamstack status          one line (or a panel icon) for what calls receive
    streamstack auto on|off     the auto-cinematographer and auto-audio (Claude)
    streamstack cameras         every live camera and every mode it offers
    streamstack camlink         the Cam Link's HDMI mode, the best one on offer
    streamstack preview [cam]   watch a camera feed (canon, camlink, program, /dev/videoN)
    streamstack mic-check [s]   record a few seconds and play them back
    streamstack mic-revive      bring back a stalled or digitally silent microphone (capture-control resync,
                                suspend/resume, card profile cycle; --reprobe adds a WirePlumber restart)
    streamstack default-source  make call apps' default microphone the voice chain (or the resolved microphone)
    streamstack voice [preset]  list or load a voice preset (bass, treble, pitch)
    streamstack console         the visual control surface: faders, meters, scenes, live picture
    streamstack canon-bridge    run the Canon live view -> loopback camera bridge
    streamstack canon status    the bridge's GPU path, rates and the camera's settings (also: get, set, af, focus)
    streamstack lights          camera follow and daylight schedule for Litra lights (run|status|enable|disable)
    streamstack deck            Elgato Stream Deck as a control surface for the Console (run|status|enable|disable)
    streamstack setup           seed this user's voice presets and the lights and deck services
    streamstack stills          AVIF snapshots and the lossless JPEG XL archive (status|archive|restore|snapshot)
    streamstack framing         auto-framing: backends, face model (--fetch downloads and verifies it)
    streamstack call-settings   the Teams and Zoom settings that match this rig
    streamstack hardware        what is plugged in and the rig it resolves to (--json, --identity)
    streamstack verify-install  version, packaged files and commands of this install; how to verify the signature
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from streamstack import (
    __version__,
    camlink,
    config,
    crash,
    deckservice,
    lightservice,
    micwatch,
    stills_cli,
    store,
    system,
    verify_install,
    videochain,
    virtualdevices,
)
from streamstack.audiochain import cli as chain_cli
from streamstack.audiochain import control as voicechain
from streamstack.audiochain import graph as voice_graph
from streamstack.audiochain import links as voice_links
from streamstack.audiochain import migrate
from streamstack.audiochain import plugin as voice_plugin
from streamstack.canon import run as canon_run
from streamstack.config import camera_external, load_settings
from streamstack.consolelink import ConsoleLink, ConsoleRefused, ConsoleUnavailable
from streamstack.framing import cli as framing_cli
from streamstack.framing import health as framing_health
from streamstack.hardware import capturehold, duplex
from streamstack.hardware import voice as hw_voice
from streamstack.hardware.doctor import report as hardware_report
from streamstack.hardware.found import Found, probe_now
from streamstack.system import (
    CANON_USB_VENDOR,
    FFMPEG_FORMATS,
    VOICE_SOURCE,
    audio_sources,
    camera_modes,
    capture_devices,
    current_format,
    find_camlink,
    have,
    loopback_has_producer,
    mode_label,
    run,
    usb_products,
    user_unit_active,
    video_name,
)

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


def _console_video() -> dict | None:
    """The running Console's video block, or None when there is no Console or it does not answer (doctor stays read-only
    and never fails because the Console is down)."""
    link = ConsoleLink.find(timeout=2.0)
    if link is None:
        return None
    try:
        return link.state()
    except (ConsoleUnavailable, ConsoleRefused):
        return None


def camlink_checks(probe: camlink.Probe) -> list[dict[str, str]]:
    """The Cam Link's health: is it there, is the HDMI source sending, and the best mode it offers."""
    if not probe.present:
        return [
            check(
                "video",
                "camlink",
                "warn",
                "no Elgato Cam Link 4K found",
                hint="plug it into a USB 3 port (blue) directly, not through a USB 2 hub",
            )
        ]
    items = []
    if not probe.signal:
        items.append(
            check(
                "video",
                "camlink",
                "ok",
                f"Elgato Cam Link on {probe.device}: no HDMI signal",
                "the HDMI source is off, asleep or not connected",
                "turn on the HDMI camera and disable its on-screen display",
            )
        )
    else:
        best = probe.chosen
        detail = "offers " + ", ".join(probe.labels[:6])
        if probe.active and probe.active[0]:
            detail += f"; now {system.current_format(probe.device)}"
        hint = ""
        if best and best[3] < 50 and best[2] == 1080:
            hint = "the camera sends 30 fps; set it to 1080p60 or 1080p50 for smoother motion"
        items.append(
            check(
                "video",
                "camlink",
                "ok",
                f"Elgato Cam Link on {probe.device}: {camlink.short_label(best) if best else 'no usable mode'}",
                detail,
                hint,
            )
        )
    if probe.note:
        items.append(check("video", "camlink-config", "warn", probe.note, hint="use auto, off or WIDTHxHEIGHT@FPS"))
    if probe.audio:
        items.append(
            check(
                "audio",
                "camlink-audio",
                "ok",
                "Cam Link HDMI audio is the camera's sound, not a call microphone",
                probe.audio,
            )
        )
    return items


def call_microphone(cfg: dict[str, str], sources: dict[str, str], found: Found | None = None) -> str:
    """The raw call microphone: the hardware Plan's, chosen by its interface's USB id. Only when the probe cannot run
    does the name rule in `system.pick_microphone` stand in, so a command line still works."""
    found = found or probe_now(cfg)
    if found is not None:
        return found.plan.audio.microphone or ""
    return system.pick_microphone(sources, cfg["STREAMSTACK_MICROPHONE"], system.default_device("source"))


def _loopback_guard_checks(cfg: dict[str, str], program: str) -> list[dict[str, str]]:
    """The v4l2loopback contract (#86): the module for this and the next kernel, the call-app caps and format, and
    who else holds the camera nodes. Each part is read-only and a probe that cannot run says nothing."""
    from streamstack.hardware import contention, contract, kmod

    rows = kmod.guard_checks()
    rows += contract.report_check(virtualdevices.camera_report(program, cfg["STREAMSTACK_PROGRAM_LABEL"]))
    rows += contention.guard_checks(cfg, find_camlink() or None)
    return rows


def run_checks(
    cfg: dict[str, str] | None = None, *, stall_probe: bool = True, found: Found | None = None, hardware: bool = True
) -> list[dict[str, str]]:
    """Every doctor check as structured data: id, group (hardware|video|audio|apps), state (ok|warn|fail), title,
    detail, hint. `found` is a probe the caller already has; without it the hardware is probed here. The Console passes
    `hardware=False`: its hardware controller owns the probe and supplies the hardware rows itself.

    `streamstack doctor` prints these and the Console shows them as cards. `stall_probe` adds the microphone
    stall check, which samples the microphone's ALSA PCM for about 5 s; the Console turns it off because its audio
    controller watches the microphone continuously.
    """
    cfg = cfg or load_settings()
    found = found or (probe_now(cfg) if hardware else None)
    checks: list[dict[str, str]] = list(hardware_report(found)["checks"]) if hardware else []
    camera = cfg["STREAMSTACK_CAMERA_DEVICE"]
    program = cfg["STREAMSTACK_PROGRAM_DEVICE"]

    for device, label, role, cid in (
        (camera, cfg["STREAMSTACK_CAMERA_LABEL"], "camera bridge", "camera-node"),
        (program, cfg["STREAMSTACK_PROGRAM_LABEL"], "program camera", "program-node"),
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
        if not virtualdevices.can_open(device):
            # "no picture yet" would be a guess: this user cannot ask. The virtual-camera-access check says why.
            continue
        if loopback_has_producer(device):
            checks.append(
                check("video", cid, "ok", f"{label} ({device}) is live", current_format(device) or "format unknown")
            )
        else:
            if role == "program camera":
                hint = "start the Console (streamstack console); the video engine feeds it"
            elif camera_external(cfg):
                hint = "this camera is fed by another tool on this host; start its service"
            else:
                hint = f"start it: systemctl --user start {CANON_UNIT}"
            checks.append(
                check(
                    "video", cid, "warn", f"{label} ({device}) has no picture yet; browsers will not list it", hint=hint
                )
            )

    checks.extend(virtualdevices.camera_checks(virtualdevices.camera_report(program, cfg["STREAMSTACK_PROGRAM_LABEL"])))
    checks.extend(videochain.chain_checks(cfg))
    checks.extend(videochain.idle_checks(_console_video()))

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

    sources = audio_sources()
    checks.extend(camlink_checks(camlink.probe(cfg, sources)))

    mic = call_microphone(cfg, sources, found)
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
    raw_silent = False
    if mic and stall_probe:
        probe = _watch_microphone(mic)
        if stall_item := micwatch.check_item(probe.stalled, probe.detail, probe.kind):
            checks.append(stall_item)
            raw_silent = stall_item["id"] == micwatch.SILENCE_CHECK_ID and stall_item["state"] == "fail"
    dump = system.run(["pw-dump"], timeout=10) if have("pw-dump") else None
    dump_text = dump.stdout if dump is not None and dump.returncode == 0 else None
    if capturehold.is_wave_xlr(mic) and have("pw-dump"):
        effective = capturehold.always_process(dump_text, mic) if dump_text is not None else None
        if hold_row := capturehold.check_row(mic, effective):
            checks.append(hold_row)
    if have("amixer"):
        checks.append(check("audio", "alsa-utils", "ok", "amixer is installed (alsa-utils)"))
    else:
        checks.append(
            check(
                "audio",
                "alsa-utils",
                "warn",
                "amixer is missing: the digital-silence fix and the interface controls cannot run",
                hint="sudo apt install alsa-utils",
            )
        )
    if default_row := hw_voice.default_source_row(
        system.default_device("source"), mic or None, VOICE_SOURCE in sources, lambda node: sources.get(node) or node
    ):
        checks.append(default_row)
    if VOICE_SOURCE in sources:
        checks.append(check("audio", "voice-chain", "ok", "StreamStack Mic (processed voice) is available"))
        # green is not enough: the chain must read the Plan's microphone, not whatever WirePlumber picked
        links = voice_links.parse_text(dump_text) if dump_text is not None else None
        if source_row := voice_links.check_row(mic or None, links, True):
            checks.append(source_row)
        if mic and stall_probe and not raw_silent and (silence_row := _voice_chain_silence_row()):
            checks.append(silence_row)
    elif missing := voice_graph.missing_required():
        checks.append(
            check(
                "audio",
                "voice-chain",
                "fail",
                "the voice chain's plugins are missing, so there is no processed voice",
                hint="install " + ", ".join(missing) + " (LADSPA); see docs/audio-chain.md",
            )
        )
    else:
        checks.append(
            check(
                "audio",
                "voice-chain",
                "fail",
                "the voice chain is not running, so there is no processed voice",
                hint="streamstack chain start (or open the Console, which starts it)",
            )
        )
    checks.extend(virtualdevices.mic_checks(virtualdevices.mic_report()))
    engines = voice_graph.available_engines()
    if not engines:
        checks.append(
            check(
                "audio",
                "voice-denoise",
                "warn",
                "no neural noise removal installed (DeepFilterNet or RNNoise)",
                hint=voice_plugin.missing_hint(),
            )
        )
    elif "deepfilternet" not in engines:
        checks.append(
            check(
                "audio",
                "voice-denoise",
                "warn",
                "DeepFilterNet (the default noise removal) is not installed; the chain uses RNNoise",
                hint=voice_plugin.missing_hint(),
            )
        )

    checks.append(framing_health.health_check(cfg))

    checks.extend(app_checks())
    checks.append(virtualdevices.seen_check(virtualdevices.seen_by_apps(cfg)))
    checks.extend(_loopback_guard_checks(cfg, program))
    checks.append(
        check("apps", "auto-mode", "ok", "auto mode", f"{'on' if auto_running() else 'off'} (streamstack auto on|off)")
    )
    return checks


def app_checks() -> list[dict[str, str]]:
    """The apps the rig and the calls need. Optional extras never warn."""
    checks: list[dict[str, str]] = []
    for tool, why in (
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
            # the zoom.us web client is a full replacement, so a missing desktop client is not a problem
            "ok",
            "Zoom desktop client",
            "installed" if have("zoom") else "not installed, calls use the zoom.us web client",
        )
    )
    return checks


def summarize(checks: list[dict[str, str]]) -> str:
    """ok, warn or fail: the worst state among the checks."""
    states = {c["state"] for c in checks}
    return "fail" if "fail" in states else "warn" if "warn" in states else "ok"


GROUP_TITLES = {"hardware": "Hardware", "video": "Video", "audio": "Audio", "apps": "Apps"}


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_settings()
    found = probe_now(cfg)
    rig = hardware_report(found)
    checks = rig["checks"] if args.hardware else run_checks(cfg, found=found)
    failed = any(c["state"] == "fail" for c in checks)
    if args.json:
        print(json.dumps({"rig": rig["rig"], "summary": summarize(checks), "checks": checks}, indent=2))
        return 1 if failed else 0
    if rig["rig"]:
        print(rig["rig"]["text"] + "\n")
    shown = [(group, title) for group, title in GROUP_TITLES.items() if any(c["group"] == group for c in checks)]
    for number, (group, title) in enumerate(shown):
        print(("\n" if number else "") + title)
        for item in (c for c in checks if c["group"] == group):
            message = f"{item['title']}: {item['detail']}" if item["detail"] else item["title"]
            report("FAIL" if item["state"] == "fail" else item["state"], message, item["hint"])
    if args.hardware:
        return 1 if any(c["state"] == "fail" for c in checks) else 0
    print("\nPick these in Teams / Zoom")
    print(
        f"  Camera:     {cfg['STREAMSTACK_PROGRAM_LABEL']}   "
        f"(graded by StreamStack; or {cfg['STREAMSTACK_CAMERA_LABEL']} raw)"
    )
    print("  Microphone: StreamStack Mic")
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
    if args.action in ("persona", "property"):
        from streamstack.director import commands

        return commands.persona(args.values) if args.action == "persona" else commands.prop(args.values)
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
    if event == "change":
        return f"{entry.get('key')}: {entry.get('before')} -> {entry.get('after')} ({entry.get('reason', '')})"
    if event == "override":
        return f"you changed {', '.join(entry.get('keys', []))}; left alone for {entry.get('hold_minutes', 30)} min"
    return str(entry.get("error") or entry.get("reason") or "")


def studio_running(cfg: dict[str, str]) -> bool:
    """The program camera exists: the video engine is feeding its node."""
    return bool(loopback_has_producer(cfg["STREAMSTACK_PROGRAM_DEVICE"]))


def call_camera(cfg: dict[str, str], studio: bool) -> str:
    """What calls get for a camera: graded (the program camera), idle, raw or off."""
    if studio and loopback_has_producer(cfg["STREAMSTACK_PROGRAM_DEVICE"]):
        return "graded"
    if studio:
        return "idle"
    camera = cfg["STREAMSTACK_CAMERA_DEVICE"]
    return "raw" if Path(camera).exists() and loopback_has_producer(camera) else "off"


def cmd_status(args: argparse.Namespace) -> int:
    """One line for a panel button: what calls get for camera and microphone."""
    cfg = load_settings()
    camera = call_camera(cfg, studio_running(cfg))
    mic = "fx" if VOICE_SOURCE in audio_sources() else "raw"
    detail = f"camera for calls: {camera}; microphone: {'StreamStack Mic' if mic == 'fx' else 'raw mic'}"
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
    Microphone ......... StreamStack Mic
    Speaker ............ headphones
    Noise suppression .. Low (the voice chain already removes noise; High/Auto fights it)
  In the meeting: no background effects or blur - StreamStack does framing and grading.

Zoom (desktop client or web)
  Settings > Video
    Camera ............. {cfg["STREAMSTACK_PROGRAM_LABEL"]}
    HD ................. on
    Mirror my video .... off when text or signs are in shot
    Adjust for low light, Touch up appearance ... off (StreamStack handles it)
  Settings > Audio
    Microphone ......... StreamStack Mic
    Automatically adjust microphone volume ... off (StreamStack auto mode levels the mic)
    Suppress background noise ... Low
    Original sound for musicians ... on, with echo cancellation on unless you wear headphones

Both
  Test call first: Teams "Make a test call", Zoom zoom.us/test.
  Headphones stop echo. Speak at arm's length with the mic just off-axis.
  `streamstack mic-check` lets you hear the processed voice before anyone else does.
""")
    return 0


def cmd_camlink(args: argparse.Namespace) -> int:
    """Show the Cam Link's HDMI modes and the one StreamStack picks (the video engine follows it by itself)."""
    cfg = load_settings()
    probe = camlink.probe(cfg)
    if not probe.present:
        print("No Elgato Cam Link found. Plug it into a USB 3 port; see `streamstack doctor`.")
        return 1
    print(f"Cam Link on {probe.device}")
    if not probe.signal or probe.chosen is None:
        print("    no HDMI signal: turn on the camera and disable its on-screen display")
        return 1
    print("    offers: " + ", ".join(probe.labels))
    print(f"    best:   {camlink.short_label(probe.chosen)} ({mode_label(probe.chosen)})")
    if probe.active:
        print(f"    now:    {current_format(probe.device)}")
    if probe.note:
        print(f"    note:   {probe.note}")
    if probe.audio:
        print(f"    audio:  {probe.audio} (HDMI sound, never the call microphone)")
    return 0


def cmd_cameras(args: argparse.Namespace) -> int:
    cfg = load_settings()
    roles = {
        str(Path(cfg["STREAMSTACK_CAMERA_DEVICE"]).resolve()): "camera bridge",
        str(Path(cfg["STREAMSTACK_PROGRAM_DEVICE"]).resolve()): "StreamStack Camera",
    }
    card = find_camlink()
    if card:
        roles[str(Path(card).resolve())] = "capture card"
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
    processed = VOICE_SOURCE if VOICE_SOURCE in sources else ""
    raw = call_microphone(cfg, sources)
    takes = []
    if args.raw and raw:
        takes.append(("raw microphone", raw))
    if processed:
        takes.append(("processed (StreamStack Mic)", processed))
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
    """Judge the microphone for a full window (blocks ~5 s): its ALSA PCM once a second for a stall, and, from a
    parec capture running alongside, whether the samples are all exact zeros (digital silence)."""
    monitor = micwatch.StallMonitor()
    capture = micwatch.SignalCapture(mic) if have("parec") else None
    listening = capture is not None and capture.start()
    if listening:
        time.sleep(0.5)  # let our own capture wake the source, so the PCM is judged while it runs
    deadline = time.monotonic() + micwatch.WINDOW_S + 1.5
    try:
        while True:
            listing = system.run(["pactl", "-f", "json", "list", "sources"], timeout=micwatch.PACTL_TIMEOUT)
            probe = monitor.probe(mic, listing.stdout if listing and listing.returncode == 0 else "")
            if probe.state != "watching" or time.monotonic() >= deadline:
                break
            time.sleep(micwatch.SAMPLE_S)
    finally:
        sample = capture.stop() if capture is not None and listening else None
    if sample is not None and sample.digital_silence() is True:
        probe = micwatch.silence_verdict(probe, sample.seconds)
    return probe


def _voice_chain_silence_row() -> dict[str, str] | None:
    """A fail row when StreamStack Mic itself is digital silence (a short capture of exact zeros), None when it carries
    signal or cannot be judged. The caller skips this while the raw microphone is silent: that is its own check."""
    sample = micwatch.capture_signal(VOICE_SOURCE, micwatch.VERIFY_SIGNAL_S) if have("parec") else None
    if sample is None or sample.digital_silence(micwatch.VERIFY_MIN_S) is not True:
        return None
    return check(
        "audio",
        "voice-chain-silence",
        "fail",
        "StreamStack Mic delivers digital silence - nobody can hear you",
        "exact zeros while the microphone has signal: the chain is not reading it",
        "streamstack chain stop && streamstack chain start",
    )


def _still_silent(mic: str) -> bool | None:
    """After a revive step: True when a short capture is still all zeros, False when audio arrives, None unknown."""
    sample = micwatch.capture_signal(mic, micwatch.VERIFY_SIGNAL_S) if have("parec") else None
    return None if sample is None else sample.digital_silence(micwatch.VERIFY_MIN_S)


def _alsa_card_for(found: Found | None, mic: str) -> int | None:
    """The ALSA card to re-write the capture controls of: the Plan's interface, else the microphone's own card."""
    if found is not None:
        interface = found.plan.audio_interface()
        if interface is not None and interface.alsa_card is not None:
            return interface.alsa_card
    listing = system.run(["pactl", "-f", "json", "list", "sources"], timeout=micwatch.PACTL_TIMEOUT)
    info = micwatch.parse_source(listing.stdout, mic) if listing and listing.returncode == 0 else None
    return info.pcm.card if info is not None and info.pcm is not None else None


def _reprobe(found: Found | None) -> micwatch.ReviveResult:
    """The explicit last resort: restart WirePlumber and wait for the interface's card to show an input profile."""
    interface = found.plan.audio_interface() if found is not None else None
    card = interface.pipewire_card if interface is not None else None

    def ready() -> bool:
        cards = system.run(["pactl", "-f", "json", "list", "cards"], timeout=micwatch.PACTL_TIMEOUT)
        if not (cards and cards.returncode == 0):
            return False
        if card:
            return duplex.cards_have_input(cards.stdout, card)
        return bool(audio_sources())

    return micwatch.reprobe_wireplumber(ready=ready, say=lambda text: print(text, flush=True))


def cmd_mic_revive(args: argparse.Namespace) -> int:
    """Revive a stalled or digitally silent microphone: re-write the capture controls (digital silence only), suspend
    and resume the source, cycle its card profile; with --reprobe restart WirePlumber as the last resort."""
    if not have("pactl"):
        print("streamstack: pactl is missing (install pulseaudio-utils)", file=sys.stderr)
        return 1
    cfg = load_settings()
    found = probe_now(cfg)
    if found is not None and (lost := duplex.lost_input(found.plan, found.snapshot)) is not None:
        label = found.plan.audio_interface()
        print(
            f"{label.label if label else 'The audio interface'}: its sound card ({lost.profile}) has no input profile."
        )
        if not args.reprobe:
            print(
                "WirePlumber has to probe the card again: run `streamstack mic-revive --reprobe`. That restarts "
                "WirePlumber and briefly drops every audio stream, so not during a call.",
                file=sys.stderr,
            )
            return 1
        result = _reprobe(found)
        print(result.message, file=sys.stdout if result.ok else sys.stderr)
        return 0 if result.ok else 1
    sources = audio_sources()
    mic = call_microphone(cfg, sources, found)
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

    card = _alsa_card_for(found, mic)
    monitor = micwatch.StallMonitor()
    result = micwatch.revive_mic(
        mic,
        say=lambda text: print(text, flush=True),
        stalled=lambda: monitor.verify(mic),
        kind=probe.kind,
        resync=micwatch.feature_unit_resync(card) if card is not None else None,
        silent=lambda: _still_silent(mic),
    )
    print(f"Did: {', '.join(result.steps)}")
    print(result.message, file=sys.stdout if result.ok else sys.stderr)
    if not result.ok and args.reprobe:
        result = _reprobe(found)
        print(result.message, file=sys.stdout if result.ok else sys.stderr)
    return 0 if result.ok else 1


def cmd_default_source(args: argparse.Namespace) -> int:
    """Make PipeWire's default source what call apps should get: StreamStack Mic while the voice chain runs, else the
    resolved microphone."""
    if not have("pactl"):
        print("streamstack: pactl is missing (install pulseaudio-utils)", file=sys.stderr)
        return 1
    cfg = load_settings()
    sources = audio_sources()
    wanted = hw_voice.wanted_default_source(call_microphone(cfg, sources), VOICE_SOURCE in sources)
    if wanted is None:
        print(
            "streamstack: no microphone and no voice chain to make the default (see `streamstack doctor`)",
            file=sys.stderr,
        )
        return 1
    current = system.default_device("source")
    label = sources.get(wanted) or wanted
    if current == wanted:
        print(f"The default microphone is already {label}.")
        return 0
    if not hw_voice.set_default_source(wanted):
        print(f"streamstack: pactl did not accept {wanted} as the default source", file=sys.stderr)
        return 1
    print(f"The default microphone is now {label} (it was {sources.get(current) or current or 'unset'}).")
    return 0


def cmd_voice(args: argparse.Namespace) -> int:
    presets = voicechain.preset_names()
    if not args.preset:
        print("Voice presets (fine-tune every stage in the Console):")
        current = voicechain.current_preset()
        for name in presets:
            print(f"{'*' if name == current else ' '} {name}")
        return 0
    if args.preset not in presets:
        print(f"streamstack: unknown preset {args.preset!r}; run `streamstack voice` for the list", file=sys.stderr)
        return 1
    if not voicechain.load_preset(args.preset):
        print("streamstack: the voice chain did not accept the preset", file=sys.stderr)
        return 1
    print(f"Loaded voice preset: {args.preset}")
    return 0


def cmd_chain(args: argparse.Namespace) -> int:
    """Start, stop or inspect the voice chain (the PipeWire filter-chain behind "StreamStack Mic")."""
    cfg = load_settings()
    actions = {
        "start": chain_cli.start,
        "run": chain_cli.run_foreground,
        "stop": chain_cli.stop,
        "status": chain_cli.status,
        "print-config": chain_cli.print_config,
        "claim-default": chain_cli.claim_default,
        "release-default": chain_cli.release_default,
        "install-plugin": chain_cli.install_plugin,
    }
    return actions[args.action](cfg)


def cmd_studio(args: argparse.Namespace) -> int:
    cfg = load_settings()
    print("Starting the voice chain...")
    if chain_cli.start(cfg, say=print) == 0:
        preset = cfg["STREAMSTACK_VOICE_PRESET"]
        if preset in voicechain.preset_names() and voicechain.current_preset() is None:
            voicechain.load_preset(preset)
    bridge = cfg["STREAMSTACK_CANON_BRIDGE"] != "none" and not camera_external(cfg)
    if bridge and usb_products(CANON_USB_VENDOR) and not user_unit_active(CANON_UNIT):
        print("Starting the Canon bridge...")
        run(["systemctl", "--user", "start", CANON_UNIT], timeout=40)
    print("The program camera is made by StreamStack itself: open the Console, `streamstack console`.")
    print(f"In Teams/Zoom pick camera '{cfg['STREAMSTACK_PROGRAM_LABEL']}' and microphone 'StreamStack Mic'.")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop the Canon bridge; calls fall back to the raw camera."""
    cfg = load_settings()
    if not camera_external(cfg) and user_unit_active(CANON_UNIT):
        print("Stopping the Canon bridge...")
        run(["systemctl", "--user", "stop", CANON_UNIT], timeout=20)
    if args.voice:
        chain_cli.stop(cfg)
    print(f"In Teams/Zoom pick camera '{cfg['STREAMSTACK_CAMERA_LABEL']}' (raw) until the studio runs again.")
    return 0


def cmd_console(args: argparse.Namespace) -> int:
    from streamstack.console.launcher import run_console

    return run_console(
        demo=args.demo, port=args.port, window=not args.no_window, mini=args.mini, idle_exit=args.idle_exit
    )


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
    return canon_run.run_bridge(cfg)


def _seed(src: Path, dest: Path, force: bool) -> bool:
    if dest.exists() and not force:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    store.atomic_write_text(dest, src.read_text())
    return True


def _setup_lights_service() -> list[str]:
    """Install and start the lights user service (camera follow). A machine without systemd just skips it."""
    try:
        outcome = lightservice.install_unit()
        if outcome in ("installed", "updated") or not lightservice.is_active():
            lightservice.enable()
    except lightservice.ServiceError as exc:
        print(
            f"streamstack: lights service not started ({exc}); retry with `streamstack lights enable`", file=sys.stderr
        )
        return []
    return [f"lights service ({outcome})"] if outcome in ("installed", "updated") else []


def _setup_deck_service() -> list[str]:
    """Install and start the deck user service, but only where the deck extra is installed (it would only fail)."""
    import importlib.util

    if importlib.util.find_spec("StreamDeck") is None or importlib.util.find_spec("PIL") is None:
        return []
    try:
        outcome = deckservice.install_unit()
        if outcome in ("installed", "updated") or not deckservice.is_active():
            deckservice.enable()
    except deckservice.ServiceError as exc:
        print(f"streamstack: deck service not started ({exc}); retry with `streamstack deck enable`", file=sys.stderr)
        return []
    return [f"deck service ({outcome})"] if outcome in ("installed", "updated") else []


def cmd_setup(args: argparse.Namespace) -> int:
    home = Path.home()
    seeded = []
    seeded.extend(f"voice preset {name} (from Easy Effects)" for name in migrate.migrate())
    (home / "Videos/StreamStack").mkdir(parents=True, exist_ok=True)
    if not args.no_service and os.environ.get("STREAMSTACK_SETUP_SERVICE", "").lower() not in ("off", "0", "no"):
        seeded.extend(_setup_lights_service())
        seeded.extend(_setup_deck_service())
    if not args.quiet:
        for item in seeded:
            print(f"seeded {item}")
        if not seeded:
            print("already set up (use --force to restore the StreamStack defaults)")
    return 0


def cmd_lights(args: argparse.Namespace) -> int:
    """Camera follow and the daylight schedule: the service, its status, and the switch."""
    from streamstack import lightauto

    if args.action == "run":
        return lightauto.run_service(load_settings())
    if args.action in ("enable", "disable"):
        try:
            outcome = lightservice.enable() if args.action == "enable" else lightservice.disable()
        except lightservice.ServiceError as exc:
            print(f"streamstack: {exc}", file=sys.stderr)
            return 1
        print(
            f"Lights service {outcome}."
            if args.action == "disable"
            else "Lights service running: the lights follow the camera."
        )
        return 0
    status = lightauto.read_status()
    active = lightservice.is_active()
    if status is None or not status.get("running"):
        state = "running but silent" if active else "not running"
        print(f"Lights service {state}. Start it with `streamstack lights enable`.")
        return 0 if active else 1
    print(f"Lights service running ({status.get('where', 'service')}). {status.get('summary', '')}")
    camera = status.get("camera", {})
    print(f"Camera: {camera.get('reason') or 'not in use'}")
    for light_id, light in status.get("lights", {}).items():
        print(f"  {light_id}: {light.get('state')}{' (held by hand)' if light.get('held') else ''}")
    return 0


def cmd_deck(args: argparse.Namespace) -> int:
    """The Stream Deck: the service that owns the device, its status, and the switch."""
    if args.action == "run":
        from streamstack.deck import service

        return service.run(autostart=not args.no_autostart)
    if args.action in ("enable", "disable"):
        try:
            outcome = deckservice.enable() if args.action == "enable" else deckservice.disable()
        except deckservice.ServiceError as exc:
            print(f"streamstack: {exc}", file=sys.stderr)
            return 1
        print(
            f"Deck service {outcome}."
            if args.action == "disable"
            else "Deck service running: plug in a Stream Deck and its keys follow the Console."
        )
        return 0
    from streamstack.deck import status

    return status.report(deckservice.is_active())


def cmd_verify_install(args: argparse.Namespace) -> int:
    results = verify_install.run_checks(args.expect_version)
    for result in results:
        report(result.state, result.message, result.hint)
    failed = sum(1 for result in results if result.state == FAIL)
    print()
    print(verify_install.signature_instructions())
    print()
    verdict = f"install is damaged, {failed} check(s) failed" if failed else "install verified"
    print(f"streamstack {__version__}: {verdict}")
    return 1 if failed else 0


def cmd_engine(args: argparse.Namespace) -> int:
    """The native video engine: probe the GPU path, list encoders, or measure latency and CPU."""
    from streamstack.engine import bench, encode, probe
    from streamstack.hardware import roles

    cfg = load_settings()
    choice = roles.choose("engine", roles.LivePlan().get(), cfg.get("STREAMSTACK_ENGINE_RENDER_NODE", ""))
    if args.action == "probe":
        node = choice.path
        backend, why = probe.probe_backend(node)
        gpu = f" ({choice.name or choice.pci_id or 'unknown GPU'}, by {choice.via})"
        print(f"render node: {node or 'none'}{gpu}")
        print(f"backend: {backend}" + (f" ({why})" if why else ""))
        for candidate in encode.ENCODERS:
            print(f"encoder {candidate.name}: {'ok' if encode.probe_encoder(candidate, node=node) else 'unavailable'}")
        return 0
    try:
        width, _, height = args.size.partition("x")
        result = bench.run(
            int(width),
            int(height),
            args.fps,
            args.seconds,
            label=f"{args.size}@{args.fps:g}",
            out_device=args.output,
            render_node=choice.path,
        )
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"streamstack: bench failed: {exc}", file=sys.stderr)
        return 1
    for key, value in result.summary().items():
        print(f"{key}: {value}")
    return 0


def cmd_hardware(args: argparse.Namespace) -> int:
    """Probe the hardware once and print the Plan it resolves to (or this machine's rig identity hash)."""
    from streamstack.console.hardware import overrides_from, plan_state
    from streamstack.hardware import probe, schema
    from streamstack.hardware.plan import resolve

    snapshot = probe.snapshot()
    if args.identity:
        if not snapshot.machine:
            status = snapshot.status("machine")
            print(f"streamstack: no machine identity: {status.error if status else 'unknown'}", file=sys.stderr)
            return 1
        print(snapshot.machine)
        return 0
    try:
        catalogue = schema.packaged()
    except schema.ProfileError as exc:
        print(f"streamstack: the hardware profiles are not valid: {exc}", file=sys.stderr)
        return 1
    plan = resolve(snapshot, catalogue, overrides_from(load_settings(), os.environ))
    state = plan_state(plan, snapshot)
    if args.json:
        print(json.dumps(state, indent=2))
        return 0
    rig = state["rig"]
    print(f"rig: {rig['label']} ({rig['id']}, chosen by {rig['via']})")
    for device in state["devices"]:
        print(f"  {device['state']:<8} {device['label']}" + (f"  [{device['detail']}]" if device["detail"] else ""))
    for gpu in state["gpus"]:
        roles = ", ".join(gpu["roles"]) or "unused"
        print(f"gpu {gpu['node']}: {gpu['name']} ({gpu['pci_id']}): {roles}")
    for job in state["video"]["jobs"]:
        print(f"video {job['consumer']} ({job['role']}): {job['node'] or 'any'} [{job['pci'] or '-'}, {job['via']}]")
    for cam in state["video"]["cameras"]:
        print(f"camera {cam['label']}: {cam['state']}, usb {cam['usb'] or '-'}, node {cam['node'] or '-'}")
    loop = state["video"]["loopback"]
    print(
        f"virtual cameras: camera {loop['camera']['node'] or 'missing'}, program {loop['program']['node'] or 'missing'}"
    )
    encoder = state["encoder"]
    print(f"encoder: {encoder['ffmpeg']} ({'hardware' if encoder['hardware'] else 'CPU'})")
    audio = state["audio"]
    interface = audio["interface"]["label"] if audio["interface"] else "none"
    print(f"audio: {interface}, {audio['rate']} Hz {audio['format'] or ''}".rstrip())
    print(f"  microphone: {audio['microphone'] or 'none'}")
    print(f"  monitor: {audio['monitor'] or 'none'}")
    for notice in state["notices"]:
        print(f"{notice['level']}: {notice['text']}")
    for source in state["sources"]:
        if not source["ok"]:
            print(f"source {source['source']}: {source['state']}: {source['error']}")
    return 0


def _loopback_guard(cfg: dict[str, str]) -> int:
    """`streamstack loopback guard`: the module for this and the next kernel, the call-app contract, the holders.
    Exit 1 when a row fails (a warning is printed but passes)."""
    rows = _loopback_guard_checks(cfg, cfg["STREAMSTACK_PROGRAM_DEVICE"])
    for row in rows:
        print(f"{row['state']:<4} {row['title']}")
        if row["state"] != "ok" and row["hint"]:
            print(f"     fix: {row['hint']}")
    return 1 if any(r["state"] == "fail" for r in rows) else 0


def cmd_loopback(args: argparse.Namespace) -> int:
    """The virtual cameras the rig profile asks for: `status` compares, `apply` writes the module files and creates
    a missing node (never touching one that exists)."""
    from streamstack.console.hardware import overrides_from
    from streamstack.hardware import loopback, probe, schema
    from streamstack.hardware.plan import resolve

    try:
        catalogue = schema.packaged()
    except schema.ProfileError as exc:
        print(f"streamstack: the hardware profiles are not valid: {exc}", file=sys.stderr)
        return 1
    plan = resolve(probe.snapshot(), catalogue, overrides_from(load_settings(), os.environ))
    rig = catalogue.rig(plan.rig.id) or catalogue.fallback_rig()
    try:
        if args.action == "guard":
            return _loopback_guard(cfg=load_settings())
        if args.action == "status":
            current = loopback.status(plan, rig)
            print(f"rig: {plan.rig.label} ({plan.rig.id})")
            for line in current.lines():
                print(line)
            return 0 if current.ok else 1
        for line in loopback.apply(plan, rig, root=Path(args.root), dry_run=args.dry_run):
            print(line)
    except loopback.LoopbackError as exc:
        print(f"streamstack: {exc}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="streamstack", description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--version", action="version", version=f"streamstack {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="check the hardware, cameras, microphone and apps")
    doctor.add_argument(
        "--hardware", action="store_true", help="only the hardware group: devices against the rig profile"
    )
    doctor.add_argument("--json", action="store_true", help="print the rig and every check as JSON")
    doctor.set_defaults(func=cmd_doctor)
    auto = sub.add_parser(
        "auto", help="auto-cinematographer and auto-audio: on, off, status, once, reset, check, persona, property"
    )
    auto.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["on", "off", "status", "once", "reset", "check", "persona", "property"],
    )
    auto.add_argument("values", nargs="*", help="persona: an id; property: NAME LATITUDE LONGITUDE, or clear")
    auto.add_argument("--dry-run", action="store_true", help="with once: show Claude's proposal, change nothing")
    auto.add_argument("--lines", type=int, default=8, help="with status: how many recent actions to show")
    auto.set_defaults(func=cmd_auto)
    status = sub.add_parser("status", help="one-line camera/microphone state (for panel buttons)")
    status.add_argument("--short", action="store_true")
    status.add_argument("--icon", action="store_true", help="icon path for a panel button, details on stderr")
    status.set_defaults(func=cmd_status)
    sub.add_parser("call-settings", help="Teams and Zoom settings for this rig").set_defaults(func=cmd_call_settings)
    verify = sub.add_parser("verify-install", help="check this install is whole; print how to verify its signature")
    verify.add_argument("--expect-version", metavar="X.Y.Z", help="fail unless this is exactly that release")
    verify.set_defaults(func=cmd_verify_install)
    studio = sub.add_parser("studio", help="start the voice chain and the Canon bridge (the Console runs video)")
    studio.set_defaults(func=cmd_studio)
    stop = sub.add_parser("stop", help="stop the Canon bridge (calls use the raw camera)")
    stop.add_argument("--voice", action="store_true", help="also stop the voice chain")
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
    link = sub.add_parser("camlink", help="the Cam Link's HDMI modes and the best one")
    link.set_defaults(func=cmd_camlink)
    sub.add_parser("cameras", help="every live video device and each mode it offers").set_defaults(func=cmd_cameras)
    mic = sub.add_parser("mic-check", help="record and play back your voice")
    mic.add_argument("seconds", nargs="?", type=float, default=6.0)
    mic.add_argument("--raw", action="store_true", help="also record the unprocessed mic for an A/B comparison")
    mic.set_defaults(func=cmd_mic_check)
    revive = sub.add_parser("mic-revive", help="bring back a stalled microphone")
    revive.add_argument("--force", action="store_true", help="cycle the microphone even when it looks healthy")
    revive.add_argument(
        "--reprobe",
        action="store_true",
        help="also restart WirePlumber when nothing else helps or the card has no input profile "
        "(drops every audio stream for a moment)",
    )
    revive.set_defaults(func=cmd_mic_revive)
    sub.add_parser("default-source", help="make the voice chain (or the microphone) the default source").set_defaults(
        func=cmd_default_source
    )
    voice = sub.add_parser("voice", help="list or load a voice preset")
    voice.add_argument("preset", nargs="?")
    voice.set_defaults(func=cmd_voice)
    chain_parser = sub.add_parser("chain", help="start, stop or inspect the voice chain behind StreamStack Mic")
    chain_parser.add_argument(
        "action",
        choices=(
            "start",
            "stop",
            "status",
            "print-config",
            "run",
            "claim-default",
            "release-default",
            "install-plugin",
        ),
        help="run is for service managers (stays in the foreground as the PipeWire host); claim-default and "
        "release-default are its ExecStartPost and ExecStopPost; install-plugin fetches the DeepFilterNet plugin",
    )
    chain_parser.set_defaults(func=cmd_chain)
    canon_run.add_parser(sub)
    sub.add_parser("canon-bridge", help="feed Canon live view into the loopback camera").set_defaults(
        func=cmd_canon_bridge
    )
    eng = sub.add_parser("engine", help="the native video engine: probe the GPU path, or bench latency and CPU")
    eng.add_argument("action", choices=("probe", "bench"))
    eng.add_argument("--size", default="1280x720")
    eng.add_argument("--fps", type=float, default=30.0)
    eng.add_argument("--seconds", type=float, default=10.0)
    eng.add_argument(
        "--output", default="/dev/video43", help="bench: the loopback device to feed (nothing may read it)"
    )
    eng.set_defaults(func=cmd_engine)
    framing_cli.add_parser(sub)
    stills_cli.add_parser(sub)
    lights = sub.add_parser("lights", help="camera follow and daylight schedule for Litra lights")
    lights.add_argument("action", nargs="?", default="status", choices=("run", "status", "enable", "disable"))
    lights.set_defaults(func=cmd_lights)
    hardware = sub.add_parser("hardware", help="what is plugged in and the rig profile it resolves to")
    hardware.add_argument("--json", action="store_true", help="print the full resolved plan as JSON")
    hardware.add_argument("--identity", action="store_true", help="print this machine's rig identity hash")
    hardware.set_defaults(func=cmd_hardware)
    loop = sub.add_parser(
        "loopback",
        help="the virtual cameras the rig profile asks for: status, apply, or guard (module per kernel, contract)",
    )
    loop.add_argument("action", nargs="?", default="status", choices=("status", "apply", "guard"))
    loop.add_argument("--root", default="/", help="with apply: write the module files under this directory")
    loop.add_argument("--dry-run", action="store_true", help="with apply: print what would change, change nothing")
    loop.set_defaults(func=cmd_loopback)
    deck = sub.add_parser("deck", help="Elgato Stream Deck as a control surface for the Console")
    deck.add_argument("action", nargs="?", default="status", choices=("run", "status", "enable", "disable"))
    deck.add_argument(
        "--no-autostart", action="store_true", help="with run: do not start a headless Console when none is running"
    )
    deck.set_defaults(func=cmd_deck)
    setup = sub.add_parser(
        "setup", help="seed the lights and deck services for this user (and import old Easy Effects voice presets once)"
    )
    setup.add_argument("--force", action="store_true", help="overwrite with the StreamStack defaults")
    setup.add_argument("--quiet", action="store_true")
    setup.add_argument("--no-service", action="store_true", help="do not install the lights and deck services")
    setup.set_defaults(func=cmd_setup)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    crash.install(str(args.command))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
