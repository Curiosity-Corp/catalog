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

## dm313red: two-party Microsoft Teams call, release 0.0.26

Captured 2026-11-03 with `teamscall.py` (beside this file), closing the two-party Teams item of #87. Two Chromium 1248
browsers, no other app on the camera or microphone (`fuser /dev/video42 /dev/video43` and `pactl list source-outputs`
checked first):

- **Party A** is the organiser, signed in to Teams web as the CEO's own work account, on the real devices. getUserMedia is
  pinned to StreamStack Camera and StreamStack Mic by label. The captured tracks were `video:StreamStack Camera` and
  `audio:StreamStack Mic`.
- **Party B** is an anonymous guest ("StreamStack guest") with Chromium's fake camera and microphone. It waited in the
  lobby and A admitted it from the People panel.
- The meeting was made with Teams "Meet now". The join link lived in memory only (not in this folder, a screenshot or the
  issue), and the meeting ended when both left after about two minutes.

| File | Shows |
|---|---|
| `dm313red-teams-b-prejoin.png` | B's guest prejoin with the fake camera |
| `dm313red-teams-b-lobby.png` | B waiting in the lobby |
| `dm313red-teams-a-lobby-admit.png` | A in the call with StreamStack Camera on, B in the lobby, A about to admit |
| `dm313red-teams-a-in-call.png` | A alone: its own StreamStack Camera picture, "Turn camera off" offered, so the camera is live |
| `dm313red-teams-b-in-call-receiving-a.png` | **B's view: A's StreamStack Camera picture full screen** and B's own fake test pattern in the corner |
| `dm313red-teams-a-in-call-with-b.png` | A's view of the same call: B's feed large, A's own tile small |
| `dm313red-teams-getstats.json` | B's `RTCPeerConnection.getStats()` sampled twice, 8 s apart, plus A's outbound counters |
| `dm313red-teams-doctor.txt` | `streamstack doctor` on 0.0.26 before the call |

What B received from A (`getStats`, B's inbound-rtp):

| | First sample | Second sample |
|---|---|---|
| video `framesDecoded` | 534 | **700** (1280x720, still growing) |
| video `bytesReceived` | 6 190 055 | 7 799 767 |
| audio `totalAudioEnergy` | | **0.0219** (`audioLevel` 0.0103, 36.6 s of samples, 1255 packets) |

A's own outbound side agrees: 702 video frames sent at 1280x720, and audio packets sent. Teams' live captions in A's window
transcribed the desk's room sound, so the microphone is a real signal and not digital zero.

### Finding: the Console's live preview blocks Chromium from opening StreamStack Camera

On this desk `tools/camera_probe.py` failed (`NotReadableError: Could not start video source`) for both loopback nodes, in
Playwright Chromium 1248 and in Edge, until the Console's preview reader was out of the way. `strace -e ioctl` on Chromium shows
the cause: its `VIDIOC_S_FMT` on `/dev/video43` returns `EBUSY` while another opener is streaming. The other opener was the Console's
`ffmpeg -f v4l2 -i /dev/video43 ... -f mjpeg -` (`console/video.py`, `CameraStream`), which the open Console window kept alive for
hours. ffmpeg never sets the format, so it coexists with the writer, but Chromium does, so any Chromium-based call app that opens the
camera while the Console preview runs gets the same error. `teamscall.py` works around it by stopping that one preview process by
exact argv and PID right before each camera open; the Console restarts it by itself, and it was running again after the call
(Chromium then holds the node and the preview coexists). Tracked in #181 so the Console reads the picture without holding the
loopback (for example from the engine's own output), and `streamstack doctor` flags it.
