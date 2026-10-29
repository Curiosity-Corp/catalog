# Proving the camera reaches a call (#87, #89)

What can be proven by a machine, how to run it, and what still needs a person on a real call.

## 1. Is StreamStack Camera listed, and does it deliver frames?

    python3 tools/camera_probe.py                    # headless Chromium on this machine's real cameras
    python3 tools/camera_probe.py --channel msedge   # the browser Teams runs in
    python3 tools/camera_probe.py --fake             # Chromium's test camera (CI)

The probe enumerates the cameras the way a call app does, finds one by label, opens it with `getUserMedia`, and
watches frames for three seconds. Checks, in order (it stops at the first failure, like a call app would):
listed, opens, enough frames (at least 10, at least 10 fps), the picture changes, the picture is not black or flat.
Exit 0 all passed, 1 a check failed (the rows say which and the fix), 2 the probe could not run.

The failure it exists for: with `exclusive_caps=1` a node nothing writes is not a camera, so the browser lists only the
other cameras (dm313red, 2026-10-27: Teams offered only the Cam Link). The row reads `StreamStack Camera is not listed
by the browser`, names what the browser did list, and points at `streamstack doctor`.

It only reads: the browser opens a camera for reading and closes it. It needs `pip install playwright`
and a browser (`playwright install chromium`, or an installed Edge with `--channel msedge`).

CI (`tests/e2e/test_camera_probe.py`) runs it against Chromium's fake camera: it proves the probe lists a camera, opens
it, sees frames, exits 0, and reports "not listed" with exit 1 for an absent label. CI has no kernel module and no
camera, so this proves the probe, not a machine. The real answer is the same command on the studio PC.

## 2. Glass-to-glass latency per path

`tools/latency_rig.py` paints the wall-clock time into the top of the picture as a strip of 60 black and white blocks
(sync, 48 time bits, CRC-8) and decodes it in the browser. Everything runs on one host clock, so no synchronisation.
Wide blocks and the CRC mean blur, scaling and lossy coding do not flip a bit, and a damaged frame is counted as
undecoded, never as a wrong number. Output: count, min, p50, p95, max and mean in milliseconds, `--json` for samples.

| Path | Command | What the number includes |
|---|---|---|
| Loopback and browser | `latency_rig.py run --device /dev/video50 --label "Latency scratch"` | the v4l2loopback copy, Chromium capture and decode |
| Engine | `latency_rig.py feed --device <engine input node>` with the engine reading it, then `measure --label "StreamStack Camera"` | the above plus the ffmpeg graph on the Arc GPU |
| Everything we can measure | `latency_rig.py paths ...` (above) | the loopback and engine legs as a table |
| Glass | `latency_rig.py glass --label "Cam Link 4K"`, camera pointed at the screen | display, the camera, capture, loopback, browser |

### Published numbers (#89)

One command reproduces them (needs two scratch loopback nodes, which only root can create, and no call running):

    sudo v4l2loopback-ctl add -n "Latency scratch" -x 1 /dev/video50 && sudo chmod 666 /dev/video50
    sudo v4l2loopback-ctl add -n "Latency scratch out" -x 1 /dev/video51 && sudo chmod 666 /dev/video51
    python3 tools/latency_rig.py paths --device /dev/video50 --out-device /dev/video51 --label "Latency scratch"
    sudo v4l2loopback-ctl delete /dev/video50 && sudo v4l2loopback-ctl delete /dev/video51

Measured on dm313red (Ubuntu 26.04, kernel 7.0.0-38, v4l2loopback 0.15.3, Arc A380 engine, Chromium from Playwright
1.64), 2026-10-27, 10 s per row, 30 fps, nothing else running on the nodes:

| Path | Size | p50 ms | p95 ms | max ms |
|---|---|---|---|---|
| loopback + browser (Chromium): stamped frame written to the node until the browser decodes it | 1280x720@30 | 39 | 42 | 64 |
| engine: GPU graph + loopback copy + reader (`streamstack engine bench`) | 1280x720@30 | 75.9 | 82.5 | 108.1 |
| engine: same | 1920x1080@30 | 107.6 | 119.3 | 150.2 |
| engine then browser, estimated sum (the two legs are measured separately) | 1280x720@30 | 114.9 | | |

How to read them: at 30 fps one frame is 33 ms, so the loopback and browser leg is about one frame of buffering plus
the copy and decode; the engine leg is the GPU graph (upload, look, download) and a loopback hop. The camera's own
sensor and USB time (the Canon bridge, the Cam Link) and the call app's encode and network are not in these numbers:
add the camera's with the glass path (needs the real camera and a screen), and the app's from a two-party call.
The browser asks for 1280x720 at 30 fps (what Teams asks for), so the browser rows are 720p30 whatever the feed size.

`streamstack engine bench` still measures the engine alone. Use a scratch loopback node for `feed`
(`modprobe v4l2loopback video_nr=50 card_label="Latency scratch" exclusive_caps=1`, with no call running): the rig
refuses `/dev/video42`, `/dev/video43`, the configured program and camera nodes, any node a process holds, and any
node that is not a v4l2loopback node. The glass path needs a screen showing the stamp and a camera filming it, and a
display refresh adds up to one frame (16 ms at 60 Hz) of its own; say so when quoting it.

Not measured here: the call app's own encode, network and the other party's display. A two-party call is the only way to
see those.

## 3a. The scripted matrix (a machine runs it)

    python3 tools/call_matrix.py            # this machine's real cameras
    python3 tools/call_matrix.py --fake     # each browser's own test camera (CI)

Runs the probe of section 1 through Chromium, Chrome, Edge, "Teams in Edge" (Edge with the media path Teams uses: the
camera by device id at 1280x720, 30 fps, prompt auto-accepted, no sign-in) and Firefox, then prints one table. A
browser that is not installed is a skip, not a failure; exit 0 all installed passed, 1 one failed, 2 nothing ran.
CI (`tests/e2e/test_call_matrix.py`) runs it on Chromium's and Firefox's own test cameras.

Result on dm313red, 2026-10-27, StreamStack Camera fed by the engine, no call running, cameras opened read-only for
3 s each: Chromium, Chrome, Edge, Teams in Edge and Firefox all listed StreamStack Camera, opened it and got a moving
picture with content (luma 41 to 42, spread 56 to 58). That is the compatibility matrix for browsers. Desktop Teams
and the Zoom client (native and Electron) cannot be driven here and stay in section 4.

Camera-in-use is already visible in the Console: the Lighting automation banner reads "Camera in use" with the app
name (`camerause.py` reads `/proc/<pid>/fd` only and never opens a node), and the System page shows the cameras.

## 3. The Teams and Zoom matrix (a person runs it)

On the studio PC, with `streamstack doctor` green and the probe passing:

1. Edge (Teams web) and Teams desktop: Settings, Devices, Camera lists "StreamStack Camera" and the preview moves.
2. Join a call with a second person. Choose StreamStack Camera. They confirm the picture, its colour and its smoothness.
3. Stop the camera feeder (the bridge or the engine source) for 20 seconds. Teams must keep the device and show the
   "camera reconnecting" slate, then the picture returns on its own. The doctor shows the video-delivery row meanwhile.
4. Start and stop OBS virtual camera: `streamstack loopback guard` names the holder and the call keeps working.
5. Repeat 1 and 2 in Zoom desktop and Chrome (Meet).
6. After a kernel update and reboot: `streamstack loopback guard` is green before the first call.

## 4. What still needs a human two-party call

- A real Teams call in Edge, signed in, with a second participant who sees the picture.
- Zoom desktop and Meet in Chrome, same.
- Glass-to-glass numbers with the real Canon and Cam Link on the real desk, and what the other party perceives.
- The dm313red bridge-failure retest: stop `canonctl-ptp-webcam.service` during a call and watch the slate appear and
  the picture return (step 3 above). The unit's own restart policy is in the canonctl repo.
- Whether Teams keeps the device through the engine restart on every Teams build; this only a call shows.
