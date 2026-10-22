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
  undo, and a failing helper (OBS closed, mic unplugged) degrades one card,
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
                                                    │   │   gain, mute, Easy Effects │
                                                    │   ├ console/video.py  OBS:     │
                                                    │   │   scenes, look, outputs    │
                                                    │   └ console/studio.py studio,  │
                                                    │       auto, health, apps       │
                                                    └───────────────────────────────┘
```

- `streamstack console` starts the server on `127.0.0.1` (port 0 = any free
  port unless `--port`), writes `$XDG_RUNTIME_DIR/streamstack-console.json`
  (`{"pid", "port", "token"}`, mode 600) and opens the window. A second
  `streamstack console` finds the running server through that file and only
  opens another window.
- `--no-window` serves only (tests, a second screen); `--demo` uses the
  simulated rig; `--mini` opens the compact window (`#/mini`).
- The window is `google-chrome`/`microsoft-edge`/`chromium` with
  `--app=URL --class=StreamStack --user-data-dir=$XDG_CACHE_HOME/streamstack/console-browser`,
  falling back to `xdg-open`. `STREAMSTACK_CONSOLE_BROWSER` overrides.
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
| GET | `/api/preview.mjpg?source=program\|<obs source>\|camera&fps=1..15&width=160..1280` | `multipart/x-mixed-replace` JPEG stream |
| GET | `/api/preview.jpg?source=…&width=…` | one JPEG (look thumbnails) |
| GET | `/api/clips/<id>.wav` | a mic-check recording (kept for 10 minutes, at most 4) |

## State snapshot

Every key is always present (use `null` for unknown) so the frontend never
guards against missing keys. Times are ISO-8601 local; levels in dBFS; look
values in OBS's own units.

```jsonc
{
  "version": "0.2.0",
  "demo": false,
  "studio": {
    "running": true,                 // OBS is running
    "busy": false,                   // a start/stop is in progress
    "camera_for_calls": "graded",    // graded | raw | obs-idle | off
    "program_label": "StreamStack Camera",
    "camera_label": "Canon EOS 50D",
    "bridge": true,                  // Canon bridge unit active (or external producer live)
    "voice_chain": true              // Easy Effects Source exists
  },
  "video": {
    "obs": "connected",              // connected | offline | no-websocket | error
    "obs_error": null,
    "scenes": [{"name": "Canon", "active": true}],
    "program_scene": "Canon",
    "privacy": false,                // "Be Right Back" is on air because of the privacy button
    "privacy_scene": "Be Right Back",
    "sources": ["Canon EOS 50D", "Cam Link 4K"],   // sources carrying the Lighting & Color filter
    "selected_source": "Canon EOS 50D",
    "look": {"brightness": 0.0, "contrast": 0.0, "gamma": 0.0, "saturation": 0.0,
             "warmth": 0.0, "sharpen_enabled": false, "sharpen_amount": 0.08,
             "lut_enabled": false, "lut_amount": 0.35},
    "limits": {"brightness": [-0.5, 0.5], "contrast": [-1, 1], "gamma": [-1, 1],
               "saturation": [-1, 1], "warmth": [-1, 1], "sharpen_amount": [0, 1], "lut_amount": [0, 1]},
    "looks": [{"name": "Natural", "builtin": true, "values": {…look…}}],
    "blur": {                         // background blur (obs-backgroundremoval), see "Background blur notes"
      "available": true,              // the filter kind is registered in the running OBS; null while OBS is down
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
      "state": "running",            // disabled | offline | starting | running | paused | stalled | stopped
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
    "chain": {"running": true, "available": true, "preset": "StreamStack Voice",
              "presets": [{"name": "StreamStack Voice", "label": "Voice", "blurb": "default broadcast voice"}],
              "bypass": false},
    "tone": {"bass_db": 2.0, "presence_db": 2.5, "treble_db": 1.5, "pitch_semitones": 0.0,
             "supported": true},       // false when Easy Effects cannot be driven live
    "tone_ranges": {"bass_db": [-12, 12], "presence_db": [-12, 12], "treble_db": [-12, 12], "pitch_semitones": [-6, 6]},
    "mic_check": {"state": "idle", "seconds": 6, "clips": [{"id": "…", "label": "raw microphone", "url": "api/clips/….wav"}]}
                                       // state: idle | recording-raw | recording-processed | done | error
  },
  "auto": {
    "running": false, "backend": "claude-cli", "can_undo": true, "busy": false,
    "last_note": "Face slightly under-exposed; lifted gamma.", "last_time": "2026-10-22T14:03:11",
    "journal": [{"time": "…", "event": "ai", "text": "…", "kind": "video|audio|ai|error"}]  // newest first, ≤ 30
  },
  "framing": {                         // auto-framing, docs/auto-framing.md
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
  }
}
```

`meters` events (and nothing else) carry live audio levels:

```jsonc
{"t": 1729605791.12,
 "mic":   {"peak_db": -18.2, "sample_db": -18.9, "rms_db": -27.5, "clip": false, "latched": false,
           "lufs_m": -26.1, "lufs_s": -27.0},                       // the raw microphone
 "voice": {"peak_db": -14.0, "sample_db": -14.3, "rms_db": -22.1, "clip": false, "latched": false,
           "lufs_m": -22.4, "lufs_s": -23.0}}                       // Easy Effects Source; null when absent
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
human message ("OBS is not running — start the studio first."). Actions that
take seconds (studio start, mic check, auto once) return at once with
`{"ok": true, "message": "Starting…"}` and report through `state` and `toast`.

| Action | Params | Effect |
| --- | --- | --- |
| `studio.start` | | `streamstack studio` |
| `studio.stop` | | `streamstack stop` |
| `video.scene` | `name` | switch the program scene |
| `video.privacy` | `on: bool` | on: remember the scene, cut to the privacy scene; off: back to the remembered scene |
| `video.source` | `name` | choose which camera source the look controls edit |
| `video.look` | `values: {key: number\|bool}` (partial), `source?` | set look values now (no ramp; the UI throttles drags to ≤ 15/s) |
| `video.look.reset` | `source?` | ramp back to the neutral look |
| `video.looks.apply` | `name`, `source?` | ramp to a saved look over 1.5 s |
| `video.looks.save` | `name` | save the current look (`~/.config/streamstack/looks.json`) |
| `video.looks.delete` | `name` | delete a saved (non-builtin) look |
| `video.blur` | `on?`, `strength?` (1–20), `rate?` (1–6), `source?` (omitted: every camera) | background blur: creates the `Background Blur` filter on the camera if it is missing, then sets its enabled flag and settings; 409 when the plugin is missing or not loaded (turning it off always works) |
| `video.blur.plugin` | `installed: bool` | demo only: pretend the plugin is (not) installed, to see the install hint |
| `video.virtualcam` | `on` | start/stop the OBS virtual camera |
| `video.record` | `on` | start/stop recording |
| `video.stream` | `on` | start/stop streaming |
| `video.canon_set` | `id`, `value` | set a Canon setting (`iso`, `shutter`, `aperture`, `exposure_comp`, `whitebalance`, `kelvin`, `picturestyle`, `focusmode`, `liveviewsize`); 409 with the camera's own reason when it refuses (for example the mode dial). The answer message says when the camera kept a different value |
| `video.canon_action` | `name`: `autofocus` \| `af_cancel` \| `focus` (`step` -3..-1 nearer, 1..3 farther) | live-view autofocus and focus nudges |
| `video.canon_refresh` | | re-read every Canon setting from the body |
| `audio.gain` | `db` | microphone input gain, clamped to `gain_range` |
| `audio.mute` | `on` | mute/unmute the microphone (the raw source, so every app is muted) |
| `audio.preset` | `name` | load a voice preset |
| `audio.bypass` | `on` | Easy Effects global bypass |
| `audio.tone` | any of `bass_db`, `presence_db`, `treble_db`, `pitch_semitones` | voice tone, live |
| `audio.revive` | | restart a stalled microphone: suspend/resume the source, then cycle the card profile; 409 while one is running |
| `audio.demo_stall` | `on` | demo only: put the demo microphone into (or out of) the stalled state |
| `audio.clip_clear` | | clear the clip latch (every window shows it cleared) |
| `audio.demo_clip` | | demo only: trip the clip latch |
| `audio.mic_check` | `seconds?` (2–15, default 6) | record raw then processed; clips appear in state |
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
| `framing.mode` | `mode`: `off` \| `gentle` \| `tight` \| `follow` | auto-framing mode; 409 when the extra or model is missing |
| `framing.hold` | `on` | freeze or release the crop; 409 while off |
| `framing.reset` | | recentre: forget the crop and find the face again; 409 while off |
| `auto.persona` | `id` | choose the director's persona |
| `auto.persona_save` | `persona` | save an edited or new persona (the full document, validated) |
| `auto.persona_reset` | `id` | drop your edits of a shipped persona, or delete a custom one |
| `auto.location` | `name`, `latitude`, `longitude` or `clear` | the property location for sunrise and sunset |
| `health.refresh` | | re-run the checks now |
| `open.app` | `name`: `obs` \| `easyeffects` \| `qpwgraph` \| `pavucontrol` \| `qv4l2` | open the full tool |

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
Controllers cache slow probes (v4l2-ctl, pactl, OBS) in their own threads so
`state()` and `meters()` return in well under 10 ms.

### Audio notes

- Meters: one `parec --raw --format=s16le --rate=16000 --channels=1
  --latency-msec=50` per source, read in a thread, peak/RMS per 50 ms frame
  with `array`/`math` (no `audioop`, gone in Python 3.13). Restart a reader
  when its source appears or disappears; never more than two readers.
- Gain and mute: `pactl set-source-volume` / `set-source-mute` on the raw mic
  chosen by `system.microphone(cfg)`.
- Easy Effects 8 is a Qt app with a local socket server; tone control uses
  whatever Easy Effects 8 supports live (research its local server commands
  first). If no live parameter path exists, tone changes are written to a
  managed `StreamStack Live` preset and loaded with `easyeffects -l`,
  debounced to at most one load per 400 ms.

### Video notes

- One long-lived `OBS` connection in a thread with reconnect/backoff
  (1, 2, 5, 10 s); `obs: "offline"` while OBS is not running.
- Preview frames come from `GetSourceScreenshot` of the program scene (or a
  named source) at the requested width; with OBS offline, `camera` falls back
  to one ffmpeg frame grab from the raw camera node every `1/fps` s. The demo
  serves a generated test-card JPEG shipped in `data/console/demo/`.
- Built-in looks: Natural (neutral), Warm Studio, Cool Daylight, Cinematic
  (LUT on), Punchy, Soft Skin. User looks live in
  `~/.config/streamstack/looks.json`.

### Background blur notes

- Plugin: [obs-backgroundremoval](https://github.com/royshil/obs-backgroundremoval) 1.4.1. Detection
  (`streamstack.blur.detect`) reads files only: `obs-backgroundremoval.so` in OBS's plugin folders (including
  `/usr/local/lib/obs-plugins`), the models next to it, the version from dpkg (or the OBS log), and the
  inference providers the binary imports. It is CPU only: upstream has no OpenVINO value for `useGPU`, so an
  Arc GPU path is a carried build (issue #26) and shows up as an extra device once it exists.
- The filter is `Background Blur` (kind `background_removal`), disabled in `scenes.json` on both cameras after
  the colour chain. Existing OBS profiles that predate it get it created on the first enable, with the same
  low-latency settings (`streamstack.blur.LOW_LATENCY`). Strength is the plugin's `blur_background` (Kawase
  blur passes, 1–20; 0 would remove the background, so it is never written); smoothness is
  `mask_every_x_frames` (Light 3, Balanced 2, Smooth 1). The plugin has no resolution or fps key: the network
  input size is fixed by the model file and the effective mask rate is the camera fps divided by
  `mask_every_x_frames`.
- obs-websocket requests: `GetSourceFilterKindList`, `GetSourceFilter`, `CreateSourceFilter`,
  `SetSourceFilterSettings` (overlay), `SetSourceFilterEnabled`, `GetStats`.
- Cost guard: while the blur is on, once a second `GetStats` gives OBS's mean frame render time and CPU use.
  Against the numbers from just before it was switched on, the verdict is `over` when the added p95 frame time is
  8 ms or more, the added CPU is 1 percentage point or more, or any frame was skipped since. OBS reports a
  smoothed mean, not a per-frame distribution, so the p95 here is over one-second readings; the benchmark
  (issue #22) measures the real distribution. `over` raises one toast and a `background-blur` health warning.
- Health: the static `background-blur` check (doctor and System page) warns with an install hint when the plugin
  or its models are missing; the live check replaces it when OBS is connected.

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
- **Audio**: a channel strip: raw vs processed meters with peak hold, gain
  fader, mute, tone knobs (bass, presence, treble, pitch), preset pads with
  their character, bypass A/B, in-app mic check with playback, and a 60 s
  loudness history.
- **Lighting**: Litra lights as big power pads and brightness / warm-to-cool
  temperature faders, an All lights master, preset pads and saved presets. A
  small lights control also sits in the Live status rail and the Mini window.
- **Auto**: the AI director: on/off, ask now, undo, Claude's notes and a
  timeline of every change.
- **System**: health checks as cards, the services, the full tools, and the
  Teams/Zoom settings checklist.
- **Mini**: a compact strip (mute, meter, privacy, scenes) for a small window
  next to the call.
- Keyboard: `M` mute, `P` privacy, `1`–`9` scenes, `B` bypass, `G` grid,
  `Ctrl+K` command palette, `?` shortcuts, `Alt+1…5` pages.
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

Revive escalation (`streamstack mic-revive`, the Console button, Shift+R, palette "Revive mic"), each step
re-verified with the same PCM signal:

1. suspend the source, resume it;
2. cycle the card profile off and back to the previous one, restore the default source;
3. otherwise report: replug the USB cable, or `systemctl --user restart wireplumber` (never run for you).

`audio.revive` and `streamstack mic-revive` refuse (409 / exit 0) while the PCM shows audio flowing or
cannot be judged; `--force` (CLI) or `{"force": true}` (action) cycles it anyway. The Shift+R key and the
palette command only exist while the mic is stalled. The `mic-stall` health check (`doctor`, the System
page) fails while stalled; `doctor` samples for about 5 s when the mic is capturing.
