# WHEP preview of the program picture

Issue #103 (radar item 35). The Live page's program picture arrives over WebRTC instead of MJPEG, so it is a fraction of a
second behind instead of a second or more, and MJPEG stays as the automatic fallback. The decision to keep the web Console
as the shell, and the trigger that reopens it if this preview misses its target, is
[ADR 0006](adr/0006-app-shell.md).

## How it works

```text
 browser (Live page)                              Console (streamstack console)
 ┌───────────────────────┐  POST /api/whep       ┌──────────────────────────────────────────┐
 │ <ss-preview            │  Content-Type:        │ server.py       routes, token, limits   │
 │   transport="auto">    │  application/sdp ───▶ │ console/whep.py WhepService             │
 │ core/whep.js           │ ◀─── 201 + SDP answer │   FrameTrack: Rig.frame() JPEG -> decode │
 │   RTCPeerConnection    │      Location: id     │   aiortc: VP8 or H.264, ICE, DTLS-SRTP │
 │ <video muted>          │ ◀══ RTP video ═══════ │                                          │
 └───────────────────────┘  DELETE /api/whep/id   └──────────────────────────────────────────┘
        │ no WebRTC, error, 6 s without a picture
        └──▶ <img src="/api/preview.mjpg"> (the old path, unchanged)
```

- It is WHEP (the player side of WHIP, [ADR 0002](adr/0002-whip-publishing.md)): one POST with an SDP offer, an SDP
  answer back, DELETE to end. No signalling channel. Candidates are gathered before the offer is sent, so there is no
  trickle.
- The server side is Python, in `streamstack.console.whep`, on aiortc, the library WHIP publishing already uses (the
  radar suggested a Rust or Go library; a second runtime for the preview is exactly what ADR 0006 avoids, and aiortc was
  already a dependency). The picture is the same JPEG the MJPEG preview serves (`Rig.frame("program", width)`), pulled at
  the preview frame rate, decoded once per *changed* picture and encoded by aiortc as VP8 or H.264, whichever the
  browser prefers. It does not touch the camera, the relay or the GPU encoders.
- The offer must ask for exactly one receive-only video track; anything else is refused with 400. At most 4 sessions
  exist; opening a fifth closes the oldest (a reload leaves a stale session until it times out). A session that never
  connects is dropped after 15 s, one whose source shows nothing for 5 s ends its track, and a closing page sends
  DELETE.
- Same token and Host rules as every `/api/` route.

The picture's RTP clock is the wall clock since the first frame, not the frame count. The frame schedule restarts when the
host is starved for a second; a count that restarted at 0 sent timestamps from the past, the browser's jitter buffer
dropped every frame as stale, and the picture froze for as long as the stream had run (#158, the "freezes after a few
scene switches" of #151). `test_the_rtp_clock_never_goes_back_when_the_frame_schedule_restarts` pins it.

## Turning it on

```bash
pip install 'streamstack[whep]'      # aiortc; the same package as the whip extra
```

Without aiortc nothing breaks: `GET /api/whep` reports `{"available": false, "reason": "aiortc is not installed ..."}`,
`POST` answers 501, and the page shows MJPEG. `<ss-preview transport="auto">` is set on the Live page's program
picture only. The Camera page keeps MJPEG because its scopes read pixels from the `<img>`.

## Fallback behaviour (`transport="auto"`)

MJPEG takes over, within a moment, when any of these happens: the browser has no `RTCPeerConnection`; the Console
answers anything but 201 (501 not installed, 503 no picture, 504 timeout); the peer connection fails or the track
ends; no frame is playing after 6 s; or a picture that was playing freezes (the decoded frame count stands still for
5 s; the server sends a frame every tick even for a still program, so a stall means the picture is stale, issue #151). The page tries WebRTC again a minute later. `data-transport` on the element says
which one is showing (`whep` or `mjpeg`); `el.stats()` returns codec, frame rate, jitter buffer and round trip while
WebRTC shows.

## Measured latency

`tests/e2e/test_page_whep.py::test_preview_latency_is_measured` measures glass to glass in a real browser: it learns
what two demo scenes look like on screen, confirms the picture shows the scene it switches away from, switches scene
through the Console API and times how long until the pixels match the new scene (sampled every animation frame).
Three rounds of four switches; it writes median, p95, max and the quietest round's median to `whep-latency.json` in the
e2e artifacts. Like the framing cost gate, the figure that counts is the quietest round, and it fails above 1000 ms
only with `E2E_LATENCY_GATE=1` (an uncontended runner or a developer machine). The shared CI runners are CPU-starved
(infrastructure#639), so there it reports and does not gate; the functional guarantee is
`test_a_scene_switch_reaches_the_preview` (every switch shows on screen within 20 s, over either transport) and
`test_a_frozen_webrtc_picture_falls_back_to_mjpeg`.

| Where | Median | Max | Samples |
|---|---|---|---|
| Developer machine, Chromium headless, demo rig, VP8, 15 fps | 132 ms | 145 ms | 8 |

What this does and does not cover: the demo rig produces the program picture from a stored JPEG, so the figure is the
preview pipeline end to end (Console frame fetch, decode, VP8 encode, RTP, jitter buffer, browser decode, paint), not
the camera's own capture delay. On the real rig the program picture is also at most one `FRAME_TTL` (1/15 s) old
when the frame is fetched. Every CI run records its own figure in `e2e-artifacts/whep-latency.json`.

The acceptance asked for the measurement "on the demo rig"; a figure on the real camera chain belongs with the latency
table of #89 once that merges, and feeds trigger R1 of ADR 0006.

## Tests

- `tests/test_console_whep.py`: offer validation, the unavailable and limit paths, a real aiortc viewer receiving
  frames from the service, stats, eviction of the oldest session, and the HTTP routes with a real aiortc client.
- `tests/e2e/test_page_whep.py` (Chromium): the Live page shows the picture over WebRTC, stats, the session ends when
  the page leaves, the scene switch reaching the screen, the freeze fallback, the latency measurement, and the two fallbacks (WHEP refused with 501, and no picture arriving).
