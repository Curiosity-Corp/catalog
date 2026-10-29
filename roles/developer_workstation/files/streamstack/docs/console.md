# StreamStack Console — architecture and contract

StreamStack Console is the visual control surface for the rig: faders, meters,
scene pads, a live picture and one-press safety buttons, built for the moment
someone is *already live* and needs one setting fast. It replaces the nested
panel menu as the main way to drive StreamStack; the CLI and the tray stay.

This document is the contract the backend and frontend are built against.
Change it first, then the code.

## Goals

- **Two seconds to any important control during a stream.** Mute, privacy
  (camera off), scene, mic level, voice preset and brightness are always one
  press or one drag away on the Live page, and on the keyboard.
- **Visual, not textual.** State is shown as meters, lit pads, colour and
  pictures; text explains, it never is the interface.
- **Safe live.** Slider drags ramp smoothly, every action answers within
  100 ms (optimistic UI, then the real state), nothing destructive without an
  undo, and a failing helper (camera unplugged, mic unplugged) degrades one card,
  never the page.
- **No new runtime dependencies.** The backend is standard-library Python like
  the rest of StreamStack; the frontend is plain ES modules, Web Components and
  CSS shipped as package data. No build step.

## Shape

```text
 Chrome/Edge --app window (or any browser)          streamstack console
 ┌──────────────────────────────────────┐   HTTP    ┌───────────────────────────────┐
 │ data/console/  index.html, app.js,   │ ◀──────▶ │ console/server.py  routes,auth │
 │ components/*.js, pages/*.js, *.css   │  SSE      │ console/hub.py     state, push │
 └──────────────────────────────────────┘  MJPEG    │ console/rig.py     Rig facade  │
                                                    │   ├ console/audio.py  meters,  │
                                                    │   │   gain, mute, voice    │
                                                    │   │   chain (audiochain/),     │
                                                    │   │   outputs (outputs.py)     │
                                                    │   ├ console/native.py engine:  │
                                                    │   │   look, crop, privacy      │
                                                    │   └ console/studio.py studio,  │
                                                    │       auto, health, apps       │
                                                    └───────────────────────────────┘
```

- `streamstack console` starts the server on `127.0.0.1` (port 0 = any free
  port unless `--port`), writes `$XDG_RUNTIME_DIR/streamstack-console.json`
  (`{"pid", "port", "token"}`, mode 600) and opens the window. A second
  `streamstack console` finds the running server through that file and only
  opens another window.
- The access token is never logged. Under the headless user service stdout is the systemd journal, readable by
  other processes of the user and collected by log shippers, so the startup line is
  `StreamStack Console: http://127.0.0.1:<port>/ (access token not logged: ...)`. `streamstack console --url`
  prints the full tokenised address of the running Console (and exits 1 when none runs); the runtime file above
  (mode 600) holds the same.
- `--no-window` serves only (tests, a second screen); `--demo` uses the
  simulated rig; `--mini` opens the compact window (`#/mini`).
- The window is `google-chrome`/`microsoft-edge`/`chromium` with
  `--app=URL --class=StreamStack --user-data-dir=$XDG_CACHE_HOME/streamstack/console-browser`,
  falling back to `xdg-open`. `STREAMSTACK_CONSOLE_BROWSER` overrides.
- **Installable web app (#143, ADR 0006).** `static/manifest.webmanifest` (name, `display: standalone`, theme and
  background colour, `start_url` and `scope` `/`) and the icons `icon-192.png`, `icon-512.png`, `icon-maskable-512.png`
  and `icon.svg` make the Console installable from Chrome, Edge or Chromium. They are served without the access token
  (a browser fetches a manifest without credentials). Regenerate the PNGs with `python3 tools/make_console_icons.py`.
  The launcher already opens an app-mode window (`--app=`, no tabs or address bar) whenever Chrome, Edge or Chromium
  is on the PATH and falls back to `xdg-open`. **An installed app window needs the launcher:** the access token is new on
  every launch and lives only in the page address, so an icon opened without the launcher shows "No access token" and
  says to open the Console with `streamstack console` or the tray. The origin includes the port, so a different port is
  a different installed app.
- The tray and the desktop entry open the Console; the old menu stays as
  `streamstack-tray --menu`.

## Security

- Binds `127.0.0.1` only. Every `/api/*` request needs the per-launch token
  (`secrets.token_urlsafe(24)`): header `X-StreamStack-Token`, or `?token=`
  for `EventSource`, `<img>` and `<audio>` URLs, which cannot set headers.
  Compared with `hmac.compare_digest`. Static files need no token.
- The `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`
  (DNS rebinding), unless `STREAMSTACK_CONSOLE_ALLOW_HOSTS` lists more
  (comma separated, `*` for any; for reverse-proxied previews only).
- `POST` bodies are JSON, at most 64 KiB, `Content-Type: application/json`
  (a cross-site form cannot send that without a CORS preflight, which the
  server never grants). No CORS headers are ever sent.
- Responses carry `Cache-Control: no-store`, `X-Content-Type-Options:
  nosniff`, `Referrer-Policy: no-referrer` and a CSP of
  `default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:;
  style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'`.
- Action parameters are validated (types, ranges, names from the known lists);
  nothing is ever passed to a shell.

## URLs

The frontend uses **relative** URLs only (`api/state`, never `/api/state`), so
the Console also works behind a path-prefix proxy. The token arrives in the
page URL as `?token=…`; `app.js` keeps it in memory.

| Method | Path | Answer |
| --- | --- | --- |
| GET | `/` | `index.html` |
| GET | `/static/<path>` | files under `data/console/` |
| GET | `/api/state` | the full state snapshot (below) |
| GET | `/api/events` | Server-Sent Events: `state` (full snapshot, on every change, at most 4/s), `meters` (~30/s), `toast` (`{level, text}`), and a `: ping` comment every 15 s |
| POST | `/api/action` | `{"action": "<name>", ...params}` → `{"ok": true, "message"?: str}` or HTTP 400/409/500 `{"ok": false, "error": str}` |
| GET | `/api/preview.mjpg?source=program\|<camera>\|camera&fps=1..15&width=160..1280` | `multipart/x-mixed-replace` JPEG stream |
| GET | `/api/preview.jpg?source=…&width=…` | one JPEG (look thumbnails) |
| POST | `/api/whep?source=program&width=…&fps=…` | WHEP: `Content-Type: application/sdp` offer, `201` with the SDP answer and `Location: /api/whep/<id>`; see [whep-preview.md](whep-preview.md) |
| DELETE | `/api/whep/<id>` | end a WHEP session |
| GET | `/api/whep` | `{available, reason, max_sessions, sessions[]}` |
| GET | `/api/deck/palette` | the options a key can be given (`{"palette": [...]}`), built from the live rig |
| GET | `/api/slots/profiles.json` | the time-of-day profiles, for export (`slots.import` takes it back) |
| GET | `/api/deck/profile.json` | the deck profile, for export |
| GET | `/api/deck/key.png?page=&key=&size=` | the picture of one key (empty `page`: the page the deck is on); needs Pillow |
| GET | `/api/hotkeys/bindings.json` | the hotkey and MIDI bindings, for export (`docs/hotkeys.md`) |
| GET | `/api/clips/<id>.wav` | a mic-check recording (kept for 10 minutes, at most 4) |

## State snapshot

Every key is always present (use `null` for unknown) so the frontend never
guards against missing keys. Times are ISO-8601 local; levels in dBFS; look
values in the engine's units.

```jsonc
{
  "version": "0.2.0",
  "demo": false,
  "studio": {
    "running": true,                 // the program camera is on
    "busy": false,                   // a start/stop is in progress
    "camera_for_calls": "graded",    // graded | raw | idle | off
    "program_label": "StreamStack Camera",
    "camera_label": "Canon EOS 50D",
    "bridge": true,                  // Canon bridge unit active (or external producer live)
    "voice_chain": true              // StreamStack Mic exists (the voice chain is running)
  },
  "video": {
    "scenes": [{"name": "Program", "active": true}],   // the native engine has the one scene
    "program_scene": "Program",
    "privacy": false,                // the picture is blacked out because of the privacy button
    "privacy_scene": "Blackout",
    "sources": ["Canon EOS 50D", "Cam Link 4K"],   // the cameras found; the engine reads one at a time
    "selected_source": "Canon EOS 50D",
    "look": {"brightness": 0.0, "contrast": 0.0, "gamma": 0.0, "saturation": 0.0,
             "warmth": 0.0, "sharpen_enabled": false, "sharpen_amount": 0.08,
             "lut_enabled": false, "lut_amount": 0.35},
    "limits": {"brightness": [-0.5, 0.5], "contrast": [-1, 1], "gamma": [-1, 1],
               "saturation": [-1, 1], "warmth": [-1, 1], "sharpen_amount": [0, 1], "lut_amount": [0, 1]},
    "looks": [{"name": "Natural", "builtin": true, "values": {…look…}}],
    "blur": {                         // background blur; the native engine adds `native: true` and `arc`, see below
      "available": true,              // false while the Arc blur is unavailable
      "plugin": {"installed": true, "version": "1.4.1", "models_ok": true, "loaded": true,
                 "devices": ["cpu"], "device_labels": ["CPU"], "device": "cpu", "hint": ""},
      "sources": {"Canon EOS 50D": {"present": true, "enabled": false, "strength": 8, "rate": 2}},
      "on": false, "strength": 8, "rate": 2,                 // on = any camera has it enabled
      "limits": {"strength": [1, 20], "rate": [1, 6]},
      "presets": {"light": 3, "balanced": 2, "smooth": 1},   // smoothness name -> rate
      "cost": {"state": "off|measuring|ok|over", "detail": "+1.9 ms per frame, +0.4% CPU, 0 dropped",
               "reasons": [], "limits": {"p95_ms": 8, "cpu_percent": 1}}
    },
    "virtualcam": true,
    "recording": {"active": false, "paused": false, "timecode": null},
    "streaming": {"active": false, "timecode": null},
    "canon": {                       // the Canon bridge and body; every key is always present
      "available": true,             // the bridge answered; false = not running (state says why)
      "state": "running",            // disabled | offline | starting | running | paused | stalled | stopped | idle (live view rested with the engine)
      "message": null,
      "session": "shared",           // shared (python-gphoto2, instant) | pause (gphoto2 command, ~1 s freeze per change)
      "usb": ["04a9:3215 Canon EOS 50D"],
      "decode": {"path": "qsv", "label": "GPU (Intel Quick Sync)",   // qsv | vaapi | cpu | legacy
                 "tried": [{"name": "qsv", "ok": true, "reason": null}],
                 "render_node": "/dev/dri/renderD128", "ffmpeg": "8.0"},
      "input": {"width": 1056, "height": 704, "subsampling": "4:2:0"},
      "output": {"width": 1056, "height": 704, "mode": "native"},
      "fps_in": 29.9, "fps_out": 29.9, "grab_ms": 31.0, "pipe_ms": 0.4,
      "cpu": {"bridge": 6.1, "ffmpeg": 3.2, "gphoto2": null, "total": 9.3},   // percent of one core
      "frames": 1234, "restarts": {"source": 0, "encoder": 0}, "stalls": 0, "last_error": null, "uptime_s": 41,
      "controls": {                  // only what this body exposes; choices, read-only and range come from the camera
        "iso": {"id": "iso", "key": "iso", "label": "ISO", "group": "exposure", "value": "400",
                "choices": ["100", "200", "400"], "range": null, "readonly": false, "reason": null, "hint": null}
        // ids: iso shutter aperture exposure_comp whitebalance kelvin picturestyle focusmode liveviewsize
        //      and, information only: model lens battery mode
      }
    }
  },
  "audio": {
    "mic": {"name": "alsa_input.usb-Elgato…", "description": "Wave:3", "present": true,
            "gain_db": 0.0, "muted": false,
            "stalled": false,        // the mic node is "running" but the graph is not scheduling it (mic watchdog)
            "stalled_since": null,   // ISO time the stall was confirmed
            "revive": {"state": "idle", "message": null}},
                                     // state: idle | reviving | ok (shown ~8 s) | failed
    "gain_range": [-30.0, 12.0],
    "chain": {                         // the voice chain, docs/audio-chain.md
      "running": true, "available": true, "missing": [], "error": null,
      "source": "streamstack_mic", "description": "StreamStack Mic", "rate": 48000,
      "contract": {"name": "streamstack_mic", "media_class": "Audio/Source", "rate": 48000, "channels": 1,
                   "state": "running", "description": "StreamStack Mic"},   // what pw-dump says; null while not running
      "preset": "StreamStack Voice", "custom": false, "bypass": false,
      "presets": [{"name": "StreamStack Voice", "label": "Voice", "blurb": "default broadcast voice",
                   "engine": "deepfilternet", "user": false, "unavailable": null}],
      "engine": "deepfilternet", "engine_active": "deepfilternet",
      "engines": [{"id": "deepfilternet", "label": "DeepFilterNet", "delay_ms": 30, "available": true, "reason": null},
                  {"id": "rnnoise", "label": "RNNoise", "delay_ms": 12, "available": false, "reason": "RNNoise needs …"}],
      "stages": [{"id": "comp", "label": "Compressor", "blurb": "…", "on": true,
                  "params": [{"key": "comp_threshold_db", "label": "Threshold", "unit": "dB", "low": -50, "high": 0,
                              "default": -18, "step": 0.5, "hint": "…", "value": -18}]}],
                                       // stages in signal order: hp, gate, denoise, deess, eq, comp, gain, limiter
      "loudness": {"on": false, "target_lufs": -16.0, "targets": [-14, -16, -23], "auto_db": 0.0,
                   "level_lufs": null, "window_db": 12, "true_peak_db": -1.0,
                   "state": "off"}     // off | riding | holding | silence | muted | no-signal | at-limit | capped
    },
    "tone": {"bass_db": 2.0, "presence_db": 2.5, "treble_db": 1.5, "supported": true},   // shortcuts onto the EQ stage
    "tone_ranges": {"bass_db": [-12, 12], "presence_db": [-12, 12], "treble_db": [-12, 12]},
    "denoise": {"engine": "deepfilternet", "atten_limit_db": 100.0, "atten_range": [0, 100], "supported": true,
                "custom": false, "engines": []},      // the same engines as chain.engines
    "mic_check": {"state": "idle", "seconds": 6, "clips": [{"id": "…", "label": "raw microphone", "url": "api/clips/….wav"}]}
                                       // state: idle | recording-raw | recording-processed | done | error
    "outputs": {                       // every PipeWire output, console/outputs.py
      "available": true, "problem": null,          // problem: why there is no list (pw-dump missing, no sinks)
      "default": "alsa_output.usb-Elgato…", "default_label": "Elgato Wave XLR", "volume_range": [0, 100],
      "devices": [{"name": "alsa_output.usb-Elgato…", "label": "Elgato Wave XLR", "detail": "Analog Stereo",
                   "kind": "headphones",       // hdmi | headphones | headset | speaker | interface | other
                   "bus": "usb", "state": "running", "volume": 55, "muted": false, "default": true,
                   "playing": false,           // the sound test is on this output right now
                   "result": {"at": "2026-10-23T09:12:40", "result": "heard", "error": null}}],
                                       // result: null (never tested) | pending (played, waiting for the person)
                                       //         | heard | not-heard | failed (error says why); kept across restarts
      "routes": [{"name": "streamstack_virtual", "label": "A virtual sink", "detail": "…", "default": false}],
                                       // virtual sinks: shown as routes, never as devices, never tested
      "warning": null,                 // {"kind": "default-lost", "name", "label", "message"} until dismissed
      "test": {"state": "idle", "name": null, "index": 0, "total": 0, "all": false,
               "seconds": 2.8, "elapsed": null,
               "phases": [{"id": "left", "start": 0.05, "end": 0.45}, {"id": "right", "start": 0.65, "end": 1.15},
                          {"id": "chime", "start": 1.5, "end": 2.7}]}   // state: idle | playing
    }
  },
  "auto": {
    "running": false, "backend": "claude-cli", "can_undo": true, "busy": false,
    "last_note": "Face slightly under-exposed; lifted gamma.", "last_time": "2026-10-22T14:03:11",
    "journal": [{"time": "…", "event": "ai", "text": "…", "kind": "video|audio|ai|error"}]  // newest first, ≤ 30
  },
  "slots": {                           // time-of-day profiles, docs/slots.md
    "available": true, "mode": "off",  // off | suggest | auto
    "fade_seconds": 4.0,
    "slots": [{"id": "dawn", "label": "Dawn", "profile": {"look": "Natural", "lighting": "Soft"}, "empty": false}],
    "current": {"slot": "morning", "label": "Morning", "minute": 562, "clock": "09:22"},
    "strip": [{"slot": "night", "start": 0, "end": 300}],   // the local day in minutes
    "options": {"looks": [], "scenes": [], "voices": [], "lighting": [], "canon": {"present": true}, "blur": true},
    "status": {"state": "idle", "summary": "…", "applied": null, "held": null, "suggestion": null, "can_undo": false},
    "director": {"on": false}, "events": [{"time": "…", "level": "ok", "text": "…"}]
  },
  "eyeline": {                         // eye-line coach, docs/auto-framing.md
    "available": true, "unavailable_hint": null, "enabled": false,
    "state": "off",                    // off | starting | running | no-face | unavailable
    "reason": null, "device": null, "in_call": false,
    "gaze": null,                      // lens | screen | down | away (null until a face is seen)
    "pitch": null, "yaw": null,        // degrees from the calibrated lens pose; pitch is positive when the head points down
    "calibrated": false, "baseline": null, "calibrating": false, "calibration_progress": 0,
    "shares": {"lens": 0, "screen": 0, "down": 0, "away": 0},   // share of the last 30 s
    "advice": {"kind": "wait", "text": "", "degrees": null, "approximate": false},  // wait | ok | lower | raise | centre
    "eye_line": null, "eye_line_target": 0.333, "eye_line_hint": null,
    "reminder": {"on": false, "minutes": 3, "range": [1, 30], "away_s": 0},
    "wizard": {"viewing_cm": 60, "window_offset_cm": 6.3}
  },
  "framing": {                         // auto-framing, docs/auto-framing.md (also shot, shots, subjects, handoff, speaking, people, speaker)
    "mode": "off",                     // off | gentle | tight | follow
    "state": "off",                    // off | starting | tracking | held | no-face | unavailable
    "held": false, "available": true,  // available false: the extra or the model is missing
    "unavailable_hint": null, "reason": null,   // why, and the fix; reason is set when state is unavailable
    "backend": null, "device": null,   // "openvino", "Intel Arc A380 (GPU)"
    "detect_ms": null, "detect_p95_ms": null, "fps": null, "model": "YuNet 2023mar",
    "zoom": 1.0, "window": null,       // {"cx", "cy", "zoom"} in frame units while on
    "sources": [{"name": "Canon EOS 50D", "on_air": true, "state": "tracking", "zoom": 1.2, "cx": 0.5, "cy": 0.46, "max_zoom": 1.3}],
    "skipped": [],                     // scene items that cannot be zoomed, with the reason
    "modes": [{"name": "gentle", "label": "Gentle", "blurb": "…"}]
  },
  "health": {
    "updated": "2026-10-22T14:03:00",
    "summary": "ok",                  // ok | warn | fail
    "checks": [{"id": "camera-node", "group": "video|audio|apps", "state": "ok|warn|fail",
                "title": "Canon EOS 50D is live", "detail": "1920x1080 YUYV @ 30 fps", "hint": ""}]
  },
  "system": {"cpu_percent": 23.0, "load": [0.8, 0.7, 0.6], "cores": 8},
  "lighting": {
    "scanned": true,                  // false until the first USB scan finished
    "lights": [{"id": "c901-<uniq>", "name": "Litra Beam 1", "model": "Litra Beam", "pid": 51457,
                "connected": true, "error": "", "error_kind": "",   // error_kind: permission | gone | io
                "power": true, "brightness": 70, "lumen": 284, "lumen_range": [30, 400],
                "kelvin": 5000, "identifying": false}],
    "master": {"on": true, "all_on": true, "brightness": 63, "kelvin": 4600},
    "presets": [{"name": "Warm evening", "builtin": true, "on": true, "brightness": 35, "kelvin": 2700, "blurb": "…"}],
    "preset": "Warm evening",         // the preset the lights currently match, or null
    "problem": null,                  // {"kind": "scan|permission|io", "message": "…"} when the bus cannot be used
    "kelvin_range": [2700, 6500], "kelvin_step": 100
  },
  "interface": {                     // the audio interface on the desk; see docs/interfaces.md
    "present": true, "checked": true, "setting": "auto",
    "profile": "scarlett-2i2-gen4",  // scarlett-2i2-gen4 | scarlett-2i2-gen3 | wave-xlr | null
    "label": "Focusrite Scarlett 2i2 (4th Gen)", "usb": "1235:8219", "card": 1,
    "mode": null,                    // Wave XLR only: off | read | write
    "notice": null, "error": null,   // a sentence to show, never a stack trace
    "controls": [{"id": "gain.1", "kind": "slider", "label": "Preamp gain", "group": "Input 1", "hint": "",
                  "readonly": "",    // a reason when the control is locked right now
                  "danger": false,   // true: switching it on needs a hold and confirm (48V)
                  "value": 38.0, "min": 0, "max": 70, "step": 1, "unit": "dB"}],
                                     // kind: switch | choice (value = option index) | slider | action | readout
    "info": [{"id": "rate", "kind": "readout", "label": "Capture", "group": "Device", "value": "48 kHz, …"}],
    "capabilities": {"input_gain": true, "autogain": true, "air": true, "phantom": true, "clipguard": false},
    "unsupported": [{"label": "Clipguard", "reason": "…"}],
    "devices": [{"usb": "1235:8219", "name": "Scarlett 2i2 4th Gen", "profile": "scarlett-2i2-gen4"}],
    "software_gain": true,           // the Audio page's PipeWire gain is always the fallback
    "demo_profiles": []              // demo only: [{"id", "label"}] the page offers to simulate
  },
  "storage": {                       // files the crash-safe store repaired (src/streamstack/store.py)
    "warnings": [{"id": "1f2e3d4c", "path": "~/.config/streamstack/looks.json", "problem": "is damaged (…)",
                  "action": "restored-backup",   // restored-backup | defaults | ignored-lines | unreadable
                  "quarantined_to": "looks.json.corrupt-20261023T101500", "at": 1729678500.0}]
  },
  "deck": {                          // the Stream Deck; see docs/deck.md
    "render": true,                  // Pillow is installed here: api/deck/key.png works
    "device": {"connected": true, "source": "usb", "model": "xl", "name": "Stream Deck XL", "serial": "…",
               "rows": 4, "cols": 8, "size": 96},  // source: usb | demo; "error" when the service cannot open one
    "profile": {"layout": {"rows": 4, "cols": 4}, "model": "virtual-4x4", "home": "main", "pristine": true,
                "mismatch": false,   // the saved profile's grid differs from the connected deck's
                "settings": {"brightness": 70, "sleep_minutes": 15},
                "pages": [{"id": "main", "name": "Live", "keys": [KEY, ...]}]},
    "surface": {"rows": 4, "cols": 4, "page": {"id": "main", "name": "Live"}, "can_back": false, "fitted": false,
                "keys": [KEY, ...]},  // what the deck itself shows (the profile re-flowed to its grid)
    "models": [{"id": "xl", "name": "Stream Deck XL", "rows": 4, "cols": 8, "size": 96}],
    "palette_rev": "1a2b3c4d"        // changes when the options list does (api/deck/palette)
  },
  "hotkeys": {                       // global hotkeys and MIDI; see docs/hotkeys.md
    "available": true, "enabled": true, "session": "x11",   // x11 | wayland | none
    "keys": {"backend": "x11", "ok": true, "reason": null},  // x11 | portal | demo | none
    "midi": {"ok": true, "reason": null, "devices": [{"id": "midiC1D0", "name": "nanoKONTROL2", "status": "ok"}],
             "last": null},
    "bindings": [{"id": "b1", "trigger": {...}, "action": "audio.mute", "params": {}, "trigger_text": "Ctrl+Alt+M",
                  "status": "ok", "detail": "", "last": {"ok": true, "time": "10:15:00", "text": "Done"}}],
    "learn": {"active": false, "event": null}, "events": [...], "limits": {"bindings": 128}
  },
  "diagnostics": {                   // opt-in telemetry and crash reports (docs/telemetry.md)
    "telemetry": {"enabled": false, "path": "/metrics", "url": null,   // url: where a scraper reads it, no token
                  "scrapes": 0, "last_scrape": null, "token_set": false},
    "crashes": {"dir": "~/.local/state/streamstack/crashes",
                "reports": [{"id": "20261023T101500-tray-4242-exception", "time": 1729678500.0, "component": "tray",
                             "kind": "exception",   // exception | handled | unraisable | native
                             "type": "RuntimeError", "message": "…", "version": "0.0.8"}]}
  },
  "meetings": {                      // calendar-aware setup; see docs/meetings.md
    "enabled": true, "lead_minutes": 2, "next": {}, "upcoming": [], "prep": {}, "sources": []
  },
  "call": {                          // call awareness (docs/call-awareness.md)
    "available": true, "state": "in_call",   // idle | joining | in_call
    "active": true, "service": "Teams", "app": "msedge", "apps": ["msedge"], "camera": true,
    "mic": "muted",                  // open | muted | none: inferred from the app's capture stream, never from audio
    "reason": "…", "since": "2026-10-27T10:15:00",   // when the call began (null when idle)
    "sources": {"audio": true, "video": true},       // which evidence could be read
    "external": null,                // {"muted": bool, "source": "extension"} while an adapter reports the app's own mute
    "settings": {"adapters": false},
    "muted_warning": {"enabled": true, "sensitivity": "medium", "cooldown_s": 20.0, "sound": false,
                      "count": 0, "last_at": null, "talking": false,
                      "range": [5.0, 300.0], "sensitivities": ["high", "medium", "low"]},
    "demo": false
  },
  "network": {                       // network quality monitor (docs/network-monitor.md)
    "available": true, "enabled": true,
    "rating": "good",                // good | degraded | poor | offline | unknown
    "text": "…", "cause": "",        // why, when not good
    "rtt_ms": 24.0, "jitter_ms": 3.0, "loss_pct": 0.0,
    "targets": [], "gateway": null, "wifi": null, "history": [], "interval_s": 5.0,
    "bandwidth": {"state": "idle", "at": null, "error": null, "up_mbps": null, "down_mbps": null,
                  "verdict": "unknown", "needed_up_mbps": 3.0, "blocked": null},
    "settings": {"targets": [], "defaults": [], "max_targets": 8},
    "demo": false
  },
  "echo": {                          // the echo / speaker-bleed check (docs/echo-check.md)
    "available": true, "state": "done",      // idle | running | done | error
    "sink": "alsa_output.pci-0000_00_1f.3.hdmi-stereo", "started_at": "2026-10-27T10:20:00",
    "blocked": null,                 // why a check cannot start right now (a call is on), else null
    "result": {"at": "…", "sink": "…", "label": "Studio Monitor", "kind": "hdmi", "text": "Bleed at -34 dB from …",
               "heard": true, "coupling_db": -34.2, "limit_db": -65.0, "delay_ms": 24.1, "coherence": 0.9,
               "loop": false, "loop_gap_ms": null,
               "level": "strong"},   // none | slight | moderate | strong | feedback | unmeasured
    "results": {"<sink name>": {"at": "…", "label": "…", "level": "strong", "coupling_db": -34.2,
                                "delay_ms": 24.1, "loop": false, "text": "…"}},   // the last result per output, kept in console-echo.json
    "error": null, "demo": false
  },
  "preflight": {                     // the pre-meeting check (docs/preflight.md)
    "available": true, "state": "done",      // idle | running | done
    "phase": null,                   // "listening" while the person says their name, then "checking"
    "prompt": "Say your name", "listen_s": 6.0, "budget_s": 20.0, "started_at": "…", "started_ts": 1761560000.0,
    "result": {"at": "…", "took_s": 9.2, "score": 100, "verdict": "ready",   // ready | check | not-ready
               "problems": 0, "text": "Everything checked is ready.",
               "checks": [{"id": "mic", "title": "Microphone", "state": "ok", "detail": "…",   // ok | warn | fail | skip
                           "fix": null, "measured": {}}]},   // fix: {"action", "params", "label"} or null
    "history": [{"at": "…", "score": 100, "verdict": "ready", "checks": {"mic": "ok"}}],   // newest first, 30 kept
    "faults": [], "demo": false
  },
  "safejoin": {                      // Safe join: off air until Go live (docs/safe-join.md)
    "available": true, "mode": "warn", "modes": ["off", "warn", "safe"],
    "auto_release_s": 0.0, "range": [0.0, 600.0],   // 0 = never go live by itself
    "holding": false, "held": {"privacy": false, "mute": false},   // what it took and still holds
    "service": null, "since": null, "release_in_s": null,
    "events": [{"at": "…", "service": "Teams", "mode": "safe", "did": ["privacy on", "microphone muted"],
                "kept": [], "notes": []}],   // newest first, 20 kept
    "demo": false
  }
}
```

A deck `KEY` is `{"i", "empty", "label", "icon", "lit", "tone", "value", "armed", "dim", "rev", "raw"?}`: `rev` changes
exactly when the key's picture would, and `raw` (profile pages only) is the saved `{action, params, label?, icon?}`.

`meters` events (and nothing else) carry live audio levels:

```jsonc
{"t": 1729605791.12,
 "mic":   {"peak_db": -18.2, "sample_db": -18.9, "rms_db": -27.5, "clip": false, "latched": false,
           "lufs_m": -26.1, "lufs_s": -27.0},                       // the raw microphone
 "voice": {"peak_db": -14.0, "sample_db": -14.3, "rms_db": -22.1, "clip": false, "latched": false,
           "lufs_m": -22.4, "lufs_s": -23.0,
           "gr_db": 3.5}}                                           // StreamStack Mic; null when absent
```

The tap is the source's own 48 kHz, 32 bit float, in 20 ms frames (`console/loudness.py`, pure Python, about 1-3 % of
one core per source). `peak_db` is the true peak in dBTP (ITU-R BS.1770-4: 4x oversampled) and the loudest of
the frames since the previous event, so no peak is missed between events; `sample_db` is the sample peak in dBFS;
`lufs_m` and `lufs_s` are momentary (400 ms) and short-term (3 s) K-weighted loudness. A mono tap is measured
as one channel: the same signal from two speakers reads 3 LU louder. Silence is `-90.0`. `clip` is true when a frame
since the previous event reached -1 dBTP or digital full scale; `latched` stays true from then until
`audio.clip_clear` (the lamp on every mic meter), whatever happens to the signal afterwards. A frame is about 260 bytes,
so the stream costs about 8 kB/s.

## Actions

`POST /api/action` with `{"action": name, ...}`. Unknown names, bad types and
out-of-range values are HTTP 400. A helper that is not running is 409 with a
human message ("The program camera is off — turn on Camera first."). Actions that
take seconds (studio start, mic check, auto once) return at once with
`{"ok": true, "message": "Starting…"}` and report through `state` and `toast`.

| Action | Params | Effect |
| --- | --- | --- |
| `studio.start` | | `streamstack studio` |
| `studio.stop` | | `streamstack stop` |
| `video.scene` | `name` | switch the program scene |
| `video.privacy` | `on: bool` | on: black out the program picture (an engine command, no restart); off: the picture returns |
| `video.source` | `name` | choose which camera source the look controls edit |
| `video.look` | `values: {key: number\|bool}` (partial), `source?` | set look values now (no ramp; the UI throttles drags to ≤ 15/s) |
| `video.look.reset` | `source?` | ramp back to the neutral look |
| `video.looks.apply` | `name`, `source?` | ramp to a saved look over 1.5 s |
| `video.looks.save` | `name` | save the current look (`~/.config/streamstack/looks.json`) |
| `video.looks.delete` | `name` | delete a saved (non-builtin) look |
| `video.blur` | `on?`, `strength?` (1–20), `rate?` (1–6), `source?` (omitted: every camera) | background blur: creates the `Background Blur` filter on the camera if it is missing, then sets its enabled flag and settings; 409 when the plugin is missing or not loaded (turning it off always works) |
| `video.blur.plugin` | `installed: bool` | demo only: pretend the plugin is (not) installed, to see the install hint |
| `video.blur.arc` | `mode` | demo only: put the native blur path in a state (`off` keeps the plain demo blur, `running`, `downloading`, `unavailable`, `failed`, `stalled`) |
| `video.virtualcam` | `on` | start/stop the program camera (the native engine) |
| `video.keepalive` | `on` | Keep awake: never idle the engine and the Canon live view, and wake them now if they rest (saved in `video.json`; see [camera-delivery.md](camera-delivery.md#idle-when-no-app-reads-the-camera)) |
| `video.idle.demo` | `state`: `running` \| `idle` \| `waking` | demo only: put the simulated engine (and Canon body) in an idle state to see the page |
| `video.record` | `on` | start/stop recording |
| `video.stream` | `on` | start/stop streaming |
| `video.canon_set` | `id`, `value` | set a Canon setting (`iso`, `shutter`, `aperture`, `exposure_comp`, `whitebalance`, `kelvin`, `picturestyle`, `focusmode`, `liveviewsize`); 409 with the camera's own reason when it refuses (for example the mode dial). The answer message says when the camera kept a different value |
| `video.canon_action` | `name`: `autofocus` \| `af_cancel` \| `focus` (`step` -3..-1 nearer, 1..3 farther) | live-view autofocus and focus nudges |
| `video.canon_refresh` | | re-read every Canon setting from the body |
| `audio.gain` | `db` | microphone input gain, clamped to `gain_range` |
| `audio.mute` | `on` | mute/unmute the microphone (the raw source, so every app is muted) |
| `audio.preset` | `name` | load a voice preset |
| `audio.bypass` | `on` | A/B: `on` plays the raw microphone (time-aligned, crossfaded) instead of the chain |
| `audio.tone` | any of `bass_db`, `presence_db`, `treble_db` | the EQ stage's shelves and presence band, live |
| `audio.chain` | `params`: stage settings by key (`comp_ratio`, `hp_hz`, `gate_on`, ...; the keys are in `chain.stages`) | live-adjust any stage; values are checked against their ranges (400) |
| `audio.chain_reset` | | back to the chosen preset's own values |
| `audio.preset_save` | `name`, `blurb?` | keep the current sound as your own preset |
| `audio.loudness` | `on?`, `target_lufs?` (-14, -16 or -23) | the loudness leveller (issue #38), see `docs/audio-chain.md` |
| `audio.denoise` | `engine` (`rnnoise` or `deepfilternet`) and/or `atten_limit_db` (0 to 100) | noise removal engine and DeepFilterNet's attenuation limit, live; 409 for an engine whose LADSPA plugin is missing |
| `audio.revive` | | restart a stalled microphone: suspend/resume the source, then cycle the card profile; 409 while one is running |
| `audio.demo_stall` | `on` | demo only: put the demo microphone into (or out of) the stalled state |
| `audio.clip_clear` | | clear the clip latch (every window shows it cleared) |
| `audio.demo_clip` | | demo only: trip the clip latch |
| `audio.demo_check_hold` | `on` | demo only: stand the mic-check timeline still (each take then ends only on `audio.demo_check_step`); it lets go when the check is done |
| `audio.demo_check_step` | | demo only: end the current mic-check take now; 409 when no check is running |
| `audio.mic_check` | `seconds?` (2–15, default 6) | record raw then processed; clips appear in state |
| `audio.output_volume` | `name`, `percent` (0 to 100) | volume of one output (`wpctl set-volume`) |
| `audio.output_mute` | `name`, `on` | mute or unmute one output |
| `audio.output_default` | `name` | make an output the default; clears the lost-default warning |
| `audio.output_test` | `name` | play the test sound on that output only, the default stays; 409 for a route, a muted output, volume 0, a missing player or a test already running |
| `audio.output_test_all` | | the same, one output after the other |
| `audio.output_test_stop` | | end a running test |
| `audio.output_result` | `name`, `heard` | the person's verdict on the test that just played; 409 before a test has played |
| `audio.output_dismiss` | | dismiss the default-output-lost warning |
| `audio.demo_output_plug` | `name`, `plugged` | demo only: unplug or plug one of the four simulated outputs |
| `audio.demo_output_reset` | | demo only: stop any test, plug everything back, forget volumes and results |
| `storage.dismiss` | `id?` | forget one repaired-file notice (all of them without `id`) |
| `call.set` | `enabled?`, `sensitivity?` (`high`\|`medium`\|`low`), `cooldown_s?` (5 to 300), `sound?`, `adapters?` (all bool except the first two numbers/words) | the "you are muted" warning settings, saved in `console-call.json` |
| `call.report_mute` | `muted: bool`, `source` (1 to 40 chars) | an adapter reports the meeting app's own mute button; refused (409) until `adapters` is on |
| `call.demo` | `app` (or null), `camera?`, `capture?`, `corked?`, `talk?`, `title?` | demo rig only: script a call app and the room (tests and the e2e suite) |
| `call.demo_reset` | none | demo rig only: no call, default settings |
| `network.set` | `enabled?`, `targets?` (list of `host:port` or `{host, port, kind}`, at most 8), `reset_targets?` | the network monitor settings, saved in `console-network.json` |
| `network.bandwidth` | `confirm?` | run the bandwidth test; refused (409) during a call unless `confirm` is true, and at most once a minute |
| `network.demo` | `rtt_ms?`, `jitter_ms?`, `loss_pct?`, `route?`, `gateway_rtt_ms?`, `gateway_loss_pct?`, `wifi?`, `up_mbps?`, `down_mbps?`, `bw_fails?` | demo rig only: script the link |
| `network.demo_reset` | none | demo rig only: a healthy link, default settings |
| `echo.check` | `name?` (an output; the default output when omitted) | the echo check (docs/echo-check.md): a quiet sweep on that output only, the raw microphone listens; 409 with the reason during a call, with an output test or another check running, for a muted/zero-volume output, a missing/muted/stalled microphone or a missing `pw-play`/`parecord` |
| `echo.stop` | | stop a running check (the sound ends at once) |
| `echo.demo` | `coupling_db?` (or null for no bleed), `delay_ms?`, `loop_gap_ms?`, `noise_db?` | demo rig only: script what the simulated microphone hears |
| `echo.demo_reset` | | demo rig only: forget the script and every result |
| `meetings.enable` / `meetings.settings` / `meetings.sync` / `meetings.prepare` | `on`; `lead_minutes?`, `only_online?`, `steps?`; none; `id?` | calendar-aware setup (`docs/meetings.md`) |
| `meetings.m365.connect` / `.cancel` / `.disconnect` | `client_id?`, `tenant?` | Microsoft 365 device-code sign-in |
| `meetings.ics.add` / `meetings.ics.remove` | `link`, `name?`; `id` | calendar links |
| `preflight.run` | `listen?` (bool, default true) | start the pre-flight check; 409 while one is running |
| `preflight.fix` | `check` (a check id) | run the Fix of that check's last finding, then re-judge it; 409 without a result or while running |
| `preflight.demo_fault` | `fault` (`noise_floor`, `no_voice`, `clipping`, `no_face`, `jitter`, `gpu_busy`), `on?` | demo rig only: inject a fault |
| `preflight.demo_reset` | none | demo rig only: no faults, no result, no history |
| `safejoin.set` | `mode?` (`off`\|`warn`\|`safe`), `auto_release_s?` (0 to 600) | what happens when a call app opens the camera, saved in `console-safejoin.json` |
| `safejoin.go_live` | none | release the privacy and mute Safe join took; a no-op when nothing is held |
| `safejoin.demo_reset` | none | demo rig only: Warn mode, nothing held, no events |
| `telemetry.set` | `on: bool` | switch the opt-in `/metrics` endpoint on or off (saved in the user env file) |
| `telemetry.scrape_config` | | message: a Prometheus `scrape_configs` block with the scrape token; 409 while telemetry is off |
| `telemetry.rotate_token` | | create a new scrape token; the old one stops working |
| `crash.clear` | | remove all crash reports |
| `interface.set` | `id`, `value`, `confirm?: bool` | change one control of the detected interface (`id` and `value` as the widget reports them: bool for a switch or Autogain, option index for a choice, number for a slider). Switching 48V on needs `confirm: true`, else 400 |
| `interface.rescan` | | look for the interface again now |
| `interface.demo_profile` | `profile`: `scarlett-2i2-gen4` \| `scarlett-2i2-gen3` \| `wave-xlr` \| `none` | demo only: simulate another interface |
| `auto.on` / `auto.off` | | auto mode service |
| `auto.once` | | one Claude pass now |
| `auto.reset` | | undo every auto change |
| `lighting.power` | `id` (a light id or `all`), `on` | turn one light, or every connected light, on or off |
| `lighting.brightness` | `id`, `percent` (1-100) | brightness, as a percentage of that model's lumen range |
| `lighting.temperature` | `id`, `kelvin` (2700-6500) | colour temperature, rounded to 100 K |
| `lighting.preset` | `name` | apply a built-in or saved lighting preset to every connected light |
| `lighting.presets.save` / `lighting.presets.delete` | `name` | save the current lights as a preset (`~/.config/streamstack/lighting.json`) / delete a saved one |
| `lighting.identify` | `id` | pulse one light three times, then restore it |
| `lighting.rescan` | | look for lights now |
| `slots.mode` / `slots.fade` / `slots.profile` / `slots.clear` / `slots.save_current` / `slots.starter` | see docs/slots.md | time-of-day profiles: settings and the six profiles |
| `slots.apply` / `slots.undo` / `slots.resume` / `slots.dismiss` / `slots.import` / `slots.reset` | see docs/slots.md | apply a profile now, put it back, end a hold, drop a suggestion, import or reset |
| `eyeline.enable` | `on` | turn the eye-line coach on or off (off by default; 409 when no face finder is available) |
| `eyeline.calibrate` | | record the lens pose from about 10 samples; look into the lens first; 409 while off or without a face |
| `eyeline.calibration.clear` | | forget the calibration |
| `eyeline.reminder` | `on`, `minutes?` (1 to 30) | remind after this long looking away during a call |
| `eyeline.demo.gaze` / `.reset` / `.away` | demo rig only | drive the simulated person (`gaze`, `offset`; `seconds`) |
| `framing.mode` | `mode`: `off` \| `gentle` \| `tight` \| `follow` | auto-framing mode; 409 when the extra or model is missing |
| `framing.shot` | `shot`: `auto` \| `close` \| `medium` \| `wide` \| `presenter` \| `desk` | shot preset; a preset other than `auto` turns framing on (Gentle) when it is off; 409 when unavailable |
| `framing.subjects` | `subjects`: `group` \| `one` | frame everyone, or only the main face |
| `framing.handoff` | `on` | move the shot to whoever is talking (group only) |
| `framing.hold` | `on` | freeze or release the crop; 409 while off |
| `framing.reset` | | recentre: forget the crop and find the face again; 409 while off |
| `auto.persona` | `id` | choose the director's persona |
| `auto.persona_save` | `persona` | save an edited or new persona (the full document, validated) |
| `auto.persona_reset` | `id` | drop your edits of a shipped persona, or delete a custom one |
| `auto.location` | `name`, `latitude`, `longitude` or `clear` | the property location for sunrise and sunset |
| `deck.press` | `key` | press a key of the page the deck is on |
| `deck.page` / `deck.goto` / `deck.back` / `deck.home` | `page` | navigate the deck's pages |
| `deck.key_set` / `deck.key_clear` / `deck.key_move` | `page`, `key`, `spec` / `to`, `to_page` | edit the profile (saves it) |
| `deck.page_add` / `deck.page_rename` / `deck.page_delete` / `deck.page_move` | `name`, `page`, `to` | edit the pages |
| `deck.settings` | `brightness` 5..100, `sleep_minutes`, or `step` | the deck's brightness and sleep timer |
| `deck.layout` / `deck.import` / `deck.reset` | `model` or `rows`+`cols` / `profile` | lay out for another deck, import, reset to the default |
| `deck.attach` / `deck.detach` | `name`, `serial`, `rows`, `cols`, `size` or `error` | the deck service reporting its device (demo: `model`) |
| `hotkeys.add` / `hotkeys.update` | `binding`: `{trigger, action, params?, label?, scale?}` (+ `id` to update) | add or change a binding. The binding is nested because the request's own `action` names the action |
| `hotkeys.remove` / `hotkeys.clear` / `hotkeys.fire` | `id` | remove a binding, remove all, or run one as if its trigger fired |
| `hotkeys.enable` / `hotkeys.learn` | `on` | switch all hotkeys on or off; listen for the next MIDI control |
| `hotkeys.import` | `document` | replace the bindings with an exported file |
| `health.refresh` | | re-run the checks now |
| `open.app` | `name`: `qpwgraph` \| `pavucontrol` \| `qv4l2` | open the full tool |

## Presets

The `presets` snapshot block, the `presets.*` actions and `GET /api/presets/catalog` are described in
[presets.md](presets.md). The Presets page is `#/presets` (Alt+8).

## Stream

The `stream` snapshot block (status, uptime, bitrate, dropped frames, latency, `cpu_percent`, `gpu_percent`, encoders,
destinations with per-destination health, recording, settings, output, last swap, events, probe) and the actions
`stream.start`, `stream.stop`, `stream.set`, `stream.probe`, `record.start`, `record.stop`, `ingest.start` and `ingest.stop` (SRT input) are described in
[stream.md](stream.md). They answer 501 on a rig without a stream backend (`stream.status` is then `unavailable`).
The Stream page is `#/stream` (Alt+0).

- **Go live / Stop stream** and **Record / Stop recording** with a clear state word (Offline, Connecting, On air,
  Degraded, Encoder failed). Stop is a hold-to-confirm button. The top bar shows ON AIR and REC while they run.
- **Meters**: encoder fps, bitrate, dropped frames, latency, CPU and GPU, each with a one-minute sparkline built in
  the page from the pushed state ("n/a" where the host reports nothing).
- **Destinations**: SRT, RTMP (classic, Enhanced, Enhanced only) and WHIP (StreamStack's own client; shown unavailable with the probe's
  reason and the install line when the `whip` extra is missing; RTT and loss are shown next to the bitrate), with the
  local recording pinned on top. Add, edit, remove, include or leave out of Go
  live, start or stop one alone. Each shows its health (Live, Connecting, Reconnecting, Failed), codec, bitrate,
  latency, drops, reconnect count and last error. The key is typed into a password field and sent once as `key` (or as
  a reference such as `env:NAME` as `key_ref`); the state shows only `"key": "set"`, so the page can never display it,
  and editing a destination keeps the stored key unless a new one is typed or "Remove the stored key" is ticked.
- **Live controls**: rate control, video bitrate, keyframe interval, quality (QP) and audio bitrate change the running
  stream through the hot-swap path, with no reconnect. A swap takes about a second and the server refuses a second one
  while it runs (409), so the page sends one change at a time, at least 1.3 s apart, and merges what was moved in
  between into the next send. Not streaming: changes are saved and apply at the next start.
- **Encoder**: the codec buttons and the encoder list come from `stream.probe`; an encoder that failed its trial is
  disabled with the reason, and the transports (WHIP included) say why one is unavailable.
- **Picture and codec** (resolution, frame rate, codec) reconnect every destination once, so they are staged and
  applied with one button that says so ("Apply and reconnect once").

Push: the page has no channel of its own. The console's Server-Sent Events stream (`api/events`) already pushes the
whole snapshot, `stream` included, whenever it changes (checked every second, and about 0.25 s after any action), so
the page re-renders from `store` on those pushes and does not poll.

## Backend modules

Each domain module exposes a controller class with this shape, plus a `Demo…`
twin with the same methods that simulates the rig (used by `--demo`, the tests
and the e2e suite). Controllers never raise into the server for expected
failures: they raise `ActionError(message, status=409|400)` from
`streamstack.console.errors`, and return state fragments as plain dicts.

```python
class AudioController:          # console/audio.py  (+ DemoAudio)
    def state(self) -> dict: ...                 # the "audio" object
    def meters(self) -> dict: ...                # the latest meters frame, non-blocking
    def start(self) -> None: ...                 # start metering threads
    def stop(self) -> None: ...
    def act(self, action: str, params: dict) -> str | None: ...   # "audio.*" actions; returns a message
    def clip_path(self, clip_id: str) -> Path | None: ...

class VideoController:          # console/video.py  (+ DemoVideo)
    def state(self) -> dict: ...                 # the "video" object
    def act(self, action: str, params: dict) -> str | None: ...   # "video.*" (video.canon_* go to console/canon.py)
    def frame(self, source: str, width: int) -> bytes | None: ... # one JPEG for previews
    def start(self) -> None: ...
    def stop(self) -> None: ...

class InterfaceController:      # console/interface.py (+ DemoInterface)
    def state(self) -> dict: ...                 # the "interface" object (cached; polled in a thread)
    def act(self, action: str, params: dict) -> str | None: ...   # "interface.*"
    def start(self) -> None: ...
    def stop(self) -> None: ...

class StudioController:         # console/studio.py (+ DemoStudio)
    def state(self) -> dict: ...                 # {"studio", "auto", "health", "system"}
    def act(self, action: str, params: dict) -> str | None: ...   # studio.*, auto.*, health.*, open.*
    def start(self) -> None: ...
    def stop(self) -> None: ...
```

class LightingController:       # console/lighting.py (+ DemoLighting, two simulated Litra Beams)
    def state(self) -> dict: ...                 # the "lighting" object
    def act(self, action: str, params: dict) -> str | None: ...   # "lighting.*"
    def health_checks(self) -> list[dict]: ...   # one System-page check: lights found, permission ok
    def start(self) -> None: ...                 # scan thread (hotplug) and read-back thread
    def stop(self) -> None: ...
```

The USB protocol itself lives in `streamstack/lighting.py` (see
[lighting.md](lighting.md)).

`console/rig.py` composes the controllers (`Rig.live(cfg)` / `Rig.demo()`), routes
actions by prefix and assembles the snapshot. `console/hub.py` polls
`Rig.snapshot()` (every 1 s, and right after each action), pushes `state`
only when it changed, and pushes `meters` at 30 Hz to every SSE client.
Controllers cache slow probes (v4l2-ctl, pactl) in their own threads so
`state()` and `meters()` return in well under 10 ms.

### Audio notes

- Meters: one `parec --raw --format=s16le --rate=16000 --channels=1
  --latency-msec=50` per source, read in a thread, peak/RMS per 50 ms frame
  with `array`/`math` (no `audioop`, gone in Python 3.13). Restart a reader
  when its source appears or disappears; never more than two readers.
- Gain and mute: `pactl set-source-volume` / `set-source-mute` on the raw mic
  chosen by `system.microphone(cfg)`.
- The voice chain (`audiochain/`, docs/audio-chain.md) is a PipeWire filter-chain StreamStack generates and hosts
  itself; tone, denoise and every stage fader write straight to its live controls (`pw-cli set-param`), at most
  20 times a second and only the controls that changed.

- Outputs (`console/outputs.py`, zero dependencies, PipeWire's own tools only): one `pw-dump` gives the Audio/Sink
  nodes, their volume and mute (Props) and the default (the "default" metadata); it is polled every 5 s and
  refreshed at once when `pw-dump --monitor` reports a change to a sink, a device or the default, so a plugged or
  unplugged device shows within a second; the watch restarts itself if it dies. Volume, mute and default go through
  `wpctl` by node id. Sinks with `node.virtual` become routes. The test sound is generated
  with `wave`/`struct` into a temp file (stereo: one low beep on the left, two higher beeps on the right, then a
  two-note chime on both), played with `pw-play --target <sink>` under a deadline (`system.play`), so the default sink
  is never touched. The verdict (heard or not) and the time are kept in `console-outputs.json` under the state dir.
  Losing the default output raises one warning and a toast; a missing `pw-dump`, `wpctl` or `pw-play`, a dead audio
  server or a failed play each come back as a plain sentence, never a traceback.
  The demo rig's `DemoOutputs` runs the same code over four simulated sinks and plays nothing.

### Video notes

- The native engine (`console/native.py`, `engine/process.py`) is one supervised ffmpeg child. A look change, a
  crop window and the privacy blackout are commands on its stdin and land between two frames; only a camera
  change restarts it. `video.record`, `video.stream` and `video.blur` answer 409 on the real rig until their
  engine paths land (recording and streaming through the engine's encode tap, blur with the OpenVINO mask).
- Preview frames come from the engine's graded output (`source=program`), or one ffmpeg frame grab from the camera
  node (cached for a moment, so the Console, framing and the director share one grab). The demo serves a
  generated test-card JPEG shipped in `data/console/demo/`.
- Out-of-process callers (the auto director, `streamstack auto reset`) use these same routes through
  `streamstack.consolelink`; the port and token come from the runtime file.
- Built-in looks: Natural (neutral), Warm Studio, Cool Daylight, Cinematic
  (LUT on), Punchy, Soft Skin. User looks live in
  `~/.config/streamstack/looks.json`.

### Background blur on the native engine

With the native video engine (the default) the blur is computed on the Arc through OpenVINO, not by a plugin; see
`docs/native-video-engine.md`. `video.blur` keeps the same keys (`on`, `strength` 1 to 20, `rate` 1 to 6, `limits`,
`presets`) and adds:

```jsonc
"native": true,
"available": true,               // false while unavailable or failed
"arc": {
  "state": "checking|unavailable|idle|downloading|loading|running|stalled|failed",
  "reason": null,                // one sentence when unavailable or failed, e.g. "the openvino package is not installed (pip install 'streamstack[blur]')"
  "device": "Intel Arc A380",
  "fps": 30.0,                   // masks computed per second, measured
  "infer_ms": 9.2, "infer_p50_ms": 8.3,   // GPU inference time, measured over the last 90 masks
  "age_ms": 0.0                  // age of the mask when it is applied
}
```

`cost` says whether the Arc keeps up (`ok`), is still measuring, or is `over` (a mask takes longer than the frames
between two masks, or went stale). `video.blur` answers 409 `Background blur is unavailable: <reason>` when there is no
OpenVINO, no model or no GPU (turning it off always works); `on`, `strength` and `rate` are applied to the running
engine without a restart and saved. The first switch-on downloads the 6 MB model (`arc.state` is `downloading`).
`video.blur.arc` (`mode`: off, running, downloading, unavailable, failed, stalled) exists on the demo rig only and puts
the page in one of these states. Settings: `STREAMSTACK_ENGINE_BLUR` (`auto` or `off`) and
`STREAMSTACK_ENGINE_BLUR_DEVICE`.

### Background blur notes

- What does not depend on how the mask is made lives in `streamstack.blur`: strength is the number of blur passes
  (1-20; 0 would remove the background, so it is never written), smoothness is the mask rate (Light 3, Balanced
  2, Smooth 1: recompute every Nth frame), and the per-frame cost gate decided before measuring
  (`docs/hardware-ai-evaluation.md`): added p95 frame time under 8 ms and added CPU under 1 percentage point.
- Health: the `background-blur` check (System page) reports the Arc blur state.

## Frontend

`data/console/` holds `index.html`, `app.js` (state store, SSE, router,
actions, keyboard), `theme.css` (tokens), `components/*.js` (custom elements)
and `pages/*.js`. Hash routing: `#/live` (default), `#/camera`, `#/audio`,
`#/auto`, `#/system`, `#/mini`.

- **Live**: the cockpit. Program picture with ON AIR / REC / VIRTUAL CAM
  badges, scene pads, a large mic fader with meter and MUTE, PRIVACY, voice
  preset pads, four quick look faders, auto mode with Claude's last note, and a
  status rail. Everything a streamer needs mid-show without scrolling at
  1280×720.
- **Camera**: large preview with thirds grid / safe-area / zebra overlays and
  a live histogram and RGB parade computed in the browser from the preview,
  full grading controls, LUT and sharpen, the looks library with thumbnails,
  source picker, virtual cam / record / stream.
- **Audio**: two sections (keys I and O). Input is a channel strip: raw vs
  processed meters with peak hold, gain fader, mute, tone knobs (bass,
  presence, treble), the Chain tab (a fader per stage, meters, loudness), preset pads with their character, bypass A/B,
  in-app mic check with playback, and a 60 s loudness history. Output is one
  mixer strip per PipeWire output (kind, bus, volume fader, mute, default
  marker, set as default) with a sound test: left, right, then a chime on that
  output only, lamps that follow it, and a Heard / Didn't hear verdict that is
  remembered. Test all runs every output in turn; virtual sinks are
  shown as routes; the list follows hot-plug and warns when the default output
  goes away. The Live page's microphone card carries a compact output picker
  with a Test button.
- **Lighting**: Litra lights as big power pads and brightness / warm-to-cool
  temperature faders, an All lights master, preset pads and saved presets. A
  small lights control also sits in the Live status rail and the Mini window.
- **Deck**: a Stream Deck as a control surface: a live picture of the keys, drag-and-drop from the options list,
  key inspector, pages, brightness and sleep, model picker, export and import (`docs/deck.md`). Alt+9.
- **Auto**: the AI director: on/off, ask now, undo, Claude's notes and a
  timeline of every change.
- **System**: health checks as cards, the services, the full tools, and the
  Teams/Zoom settings checklist.
- **Mini**: a compact strip (mute, meter, privacy, scenes) for a small window
  next to the call.
- Keyboard: `M` mute, `P` privacy, `1`–`9` scenes, `B` bypass, `G` grid,
  `Ctrl+K` command palette, `?` shortcuts, `Alt+1…9` and `Alt+0` pages (Alt+9 is Deck, Alt+0 is Stream).
- Dark broadcast-console theme by default, light theme available, works from
  360 px (phone as a remote) to 4K, `prefers-reduced-motion` respected,
  everything reachable by keyboard and labelled for screen readers.

## Frontend implementation notes

Code lives in `src/streamstack/data/console/` (no build step, ES modules, CSP safe).

- `app.js` is the facade pages import from their `ctx`: `store`, `act`, `actStream`, `flushStream`, `toast`, `navigate`, `registerKeys`, `registerCommands`, `ui`, `icon`, `ensureCss`, `ops`.
- `core/` holds the implementation (api, store, connection, actions, router, keys, theme, toast, ops).
- `components/` holds the design system custom elements (`ss-fader`, `ss-knob`, `ss-button`, `ss-pad`, `ss-toggle`, `ss-status-chip`, `ss-card`, `ss-toast`, `ss-meter`, `ss-preview`, `ss-palette`, `ss-shortcuts`). Each file starts with a header comment documenting attributes, properties and events.
- `pages/index.js` is the page registry. A page module exports `async function mount(host, ctx)` and may return a cleanup function. Load page CSS with `await ctx.ensureCss(new URL('./x.css', import.meta.url).href)`.
- Deviations from the contract above: the app is split into `core/*.js` behind `app.js`; extra files `shell.css`, `icon.svg` and per-page CSS; extra elements `ss-card`, `ss-toggle`, `ss-shortcuts`; a `ss:reconnected` window event restarts the preview stream.

## Mic watchdog

A USB microphone can stay `RUNNING` in pactl while nothing reaches the call. The watchdog
(`streamstack/micwatch.py`, shared by the CLI and the Console) detects this and offers a one-press
**Revive mic**. It never revives by itself.

Signal. The ground truth is the kernel's ALSA PCM, not PipeWire's own views: `pw-dump` dumps a stalled
node like a healthy one, and `pw-top -b` only reports the driver that ran in the sampled cycle (a healthy
mic flips between `R 1024 48000` and `R 0 0 ??? ???` when a Bluetooth sink is a second driver), so neither
is used. For an ALSA source the card/device/subdevice come from its pactl properties (`alsa.card`,
`alsa.device`, `alsa.subdevice`, `device.api`) and the watchdog reads
`/proc/asound/card{C}/pcm{D}c/sub{S}/status` once a second. A healthy 48 kHz mic advances `hw_ptr` and
`appl_ptr` by about 48000 frames a second. The mic is stalled when it is present, unmuted and RUNNING in
pactl and, over at least 5 s, the PCM is not `RUNNING`, or `hw_ptr` does not advance (no audio from the
device), or `hw_ptr` advances but `appl_ptr` does not (PipeWire is not reading). The Console flag clears
2 s after the PCM moves again. Our own meter `parec` receiving no bytes is only corroboration (it is
named in the check detail), never a trigger. Non-ALSA sources (Bluetooth, virtual) are not judged:
`stalled` stays `false` and there is no `mic-stall` check.

Digital silence. The pointers can advance perfectly while every sample is exactly 0 (2026-10-27, Wave XLR
at 75 dB gain: the device's internal UAC feature-unit mute/volume was out of sync with ALSA's cached values, and
`arecord -D hw:XLR` read zeros too). A live preamp always has a noise floor (about -43 dBFS there), so a whole window
(4 s) of exact zeros is never a quiet room. The Console's meter reader tracks the unbroken run of exact-zero frames;
`doctor` and `mic-revive` capture a few seconds with `parec` alongside the PCM sampling. It is reported as its own
fault (`mic-digital-silence` check, `mic.stall_kind: "silence"`, a different banner title) with its own fix, never as a
stall. A muted source is not judged, and a stall always wins over silence. The Console state carries `stall_kind`
(`""`, `"stall"` or `"silence"`).

Revive escalation (`streamstack mic-revive`, the Console button, Shift+R, palette "Revive mic"), each step
re-verified with the same PCM signal (for digital silence also that a short capture is no longer all zeros):

0. digital silence only: re-write the capture switch (off, then its old value) and the capture volume (one step
   away, then its old value) of the interface the hardware Plan resolved (else the microphone's own card), through
   `amixer` (package **alsa-utils**; `doctor` checks that it is installed). Every control ends where it started, so a
   muted microphone stays muted, and each control's cached value is logged with the step (the kernel caches these
   values and never reads them back from the device);
1. suspend the source, resume it;
2. cycle the card profile off and back to the previous one, restore the default source;
3. otherwise report: replug the USB cable, or `systemctl --user restart wireplumber` (never run for you).
   `streamstack mic-revive --reprobe` is the explicit opt-in that runs it as the last step: restarting WirePlumber
   briefly drops every audio stream of the user, so it is never part of the watchdog or the Console button.

Lost duplex profile. After a reboot or a replug the Wave XLR's card can come up with output profiles only
(`output:analog-stereo`), so PipeWire has no microphone for it; a second local user's WirePlumber probing the card at
the same moment is the suspected cause. `doctor` fails with `hw-audio-duplex` when the Plan's interface is attached,
its card is not `off`, and its profile has no `input:` part (`hardware/duplex.py`). With no microphone there is nothing
to revive, so `streamstack mic-revive` explains this and `--reprobe` restarts WirePlumber and waits for the input
profile to return.

Default source. `doctor` warns (`default-source`) when PipeWire's default source is not StreamStack Mic (voice chain
running) or the resolved microphone (chain stopped), for example the Cam Link's HDMI audio after a replug.
`streamstack default-source` sets it (`pactl set-default-source`); the watchdog and `doctor` never change it. Making the
voice chain's source the default while it runs is the voice chain's job (`audiochain`), not the watchdog's.

Capture-hold (Wave XLR). The XLR shares one full-speed USB link between capture and playback; a reported Linux
ordering bug returns bit-exact zeros for the life of a capture stream opened after a playback stream. StreamStack ships
`packaging/wireplumber/wireplumber.conf.d/52-streamstack-wave-xlr-capture-hold.conf` (installed to
`/etc/wireplumber/wireplumber.conf.d` by `make install`), which sets `node.always-process` on the XLR capture node, and
`doctor` reads the effective property from `pw-dump` (`wave-xlr-capture-hold`). `tools/xlr_capture_order.py` reproduces
the ordering (playback first, then capture, N cycles, counts all-zero captures); see docs/hardware/dm313red.md.

`audio.revive` and `streamstack mic-revive` refuse (409 / exit 0) while the PCM shows audio flowing or
cannot be judged; `--force` (CLI) or `{"force": true}` (action) cycles it anyway. The Shift+R key and the
palette command only exist while the mic is stalled. The `mic-stall` health check (`doctor`, the System
page) fails while stalled; `doctor` samples for about 5 s when the mic is capturing.
