# Live call evidence, release 0.0.22

Captured 2026-10-29 with `zoomcall.py` (beside this file): Chromium 1248 in a headed Xvfb browser joins Zoom's public
test meeting (zoom.us/test, web client) with StreamStack Camera and StreamStack Mic picked as a user picks them.
Each host ran the installed, signed 0.0.22 release (`streamstack --version`), with no call in progress (checked with
`fuser /dev/video42 /dev/video43` and `pactl list source-outputs` first).

## dm313red: in the call

| File | Shows |
|---|---|
| `dm313red-zoom-prejoin.png` | Zoom's join page previewing StreamStack Camera |
| `dm313red-zoom-in-call-camera.png` | In meeting as "StreamStack dm313red": Zoom's "Do you see yourself?" check names **StreamStack Camera** over the live picture |
| `dm313red-zoom-in-call-audio-test.png` | Still in the meeting, at Zoom's audio check |
| `dm313red-doctor.txt` | `streamstack doctor`: 51 ok, 2 warnings, 0 failures |

The browser's captured tracks were `audio:StreamStack Mic` and `video:StreamStack Camera`. The meeting toolbar read
"Test Zoom Meeting | Mute | Video | Participants | Chat | ... | Leave | StreamStack dm313red". Doctor shows the
Wave XLR at 48000 Hz s24le, which is native and needs no resampling, and the camera at 1280x720 YUYV 30 fps. The two warnings are
another account's WirePlumber and the Cam Link on a USB 2 port.

## deskmeet1: camera delivered, call blocked

| File | Shows |
|---|---|
| `deskmeet1-streamstack-camera-frame.png` | A frame read from /dev/video43 (StreamStack Camera): StreamStack's "Camera reconnecting" card |
| `deskmeet1-zoom-prejoin.png` | Zoom's join page opening StreamStack Camera with no device error |
| `deskmeet1-doctor.txt` | `streamstack doctor`: 38 ok, 11 warnings, 1 failure |

Chromium opens StreamStack Camera at 720x480. It holds the card because the Canon EOS 50D is not on USB. The
doctor failure is the voice chain: the audio interface and the Canon both fail USB enumeration (`error -71`)
on hub ports 1-10.1.4 and 1-2.4.3. A software port power-cycle did not recover them, so there is no StreamStack Mic
until the cables are reseated. Zoom's reCAPTCHA also refused the automated join from this host three times. The
in-call screenshot for deskmeet1 is still open, after the reseat.
