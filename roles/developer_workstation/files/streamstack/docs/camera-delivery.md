# Camera delivery to call apps

Work package C of the milestone "Camera delivery to call apps" (#86, #87, #89; #88 is decided in ADR 0004). This page
is the operator's view: what StreamStack does so Teams, Zoom and Chrome always see "StreamStack Camera", how to check
it, and what only a person on a real call can prove.

## Why the camera can vanish

`/dev/video43` ("StreamStack Camera") is a v4l2loopback node with `exclusive_caps=1` (ADR 0004). Such a node is only
a *camera* to Chromium, Edge, Teams and Zoom while a process writes it. On dm313red on 2026-10-27 the external PTP
bridge feeding `/dev/video42` died after a reboot (its ffmpeg pipe closed). The engine had nothing to read, so nothing
wrote `/dev/video43`, and Teams in Edge listed only the Cam Link. Nothing told the person.

## Resilience (Console, `streamstack doctor`)

The Console keeps the program node fed from the outside of the engine (`console/delivery.py`, `engine/slate.py`):

1. **Guard before every start.** The engine supervisor asks "can the source be read?" (the node exists and, for the
   Canon's loopback, has a producer) before it starts ffmpeg. While the answer is no it waits and asks again every
   second, instead of failing in a loop. When the answer turns yes it starts by itself.
2. **A stalled child is stopped.** ffmpeg blocks in the V4L2 read when a loopback producer disappears. A child whose
   frame counter does not move for 4 s (10 s before its first frame) is stopped and handled like any other exit.
3. **The slate covers the gap.** While no engine child writes the node, a second small ffmpeg writes it at the engine's
   own size, YUYV and frame rate: a branded "Camera reconnecting" card (`STREAMSTACK_CAMERA_LOSS=slate`, default) or the
   last frame darkened and blurred under the same line (`hold`). Exactly one writer exists at any moment: the guard
   releases the slate before the engine child opens the node. The call app keeps its handle; it sees a short freeze,
   then live picture again. Teams does not lose the device.
4. **One continuous writer while the source is missing or silent (#177).** The guard's "is it there" test (node exists,
   loopback has a producer) says yes for a Cam Link with no HDMI signal, so the engine used to start a child, wait
   4 s for a frame, stop it and start another: `/dev/video43` changed writer every 4 to 10 s, the engine logged "the
   camera stopped delivering frames" 500 times an hour, and Chromium `getUserMedia` read black (deskmeet1, Canon off
   USB, 0.0.22 and 0.0.23). After any fault (a child that exited, or was stopped by the watchdog, whatever its uptime)
   the guard now **reads one frame from the source with a separate read-only ffmpeg** (`delivery.read_one_frame`,
   which never opens the program node) and releases the slate only when that read succeeds. Until then the slate is
   the one writer; the question is repeated after 1, 2, 4, 8 and then every 15 s. A child that dies soon after a good
   read makes the pause grow again; one that ran 30 s or more starts the pauses over. The card's reason changes in
   place (drawtext `reload=1`), so even a changed sentence does not restart the writer, and the engine logs an identical
   warning once per five minutes ("repeated N more times").

   *Why this design, and not a persistent writer fed by switchable inputs.* A single long-lived ffmpeg that owns the
   node and selects between the slate and the engine's picture would need the engine's graph to leave through a pipe
   into a second process: an extra copy and a process on the hot path of every frame, for every user, to fix a fault
   that only happens while the source is silent (and against the latency budget of #141 and the rig of #89). An
   atomic handover with two writers is not available either: the contract allows one writer on the node
   (`loopback-contract.md`, Competing producers). The churn came from *starting the real writer on a source that had not
   proven a picture*, so the fix is to prove the picture first: the writer only changes at a real recovery, once. At
   that moment the card holds its last frame while the engine child starts (about a second, GPU graph set-up); the
   consumer keeps its handle, sees the card freeze, then live video, never black.
5. **Fail over to the Cam Link** (`STREAMSTACK_CAMERA_FALLBACK=auto`, default). When the picked camera has had no producer
   for 6 s and another camera delivers, the engine is re-pointed at it, and goes back after the first camera has been
   steady for 5 s. `off` keeps the picked camera and relies on the slate. The Cam Link is only used when it is
   connected; with no HDMI signal it delivers no frame, so the picture proof in step 4 keeps the slate up instead.

Where it shows: the Console's System page row **StreamStack Camera is fed by ...** (ok), **... is on Cam Link 4K, not
Canon EOS 50D** (warning, with the restart command), **... showing 'camera reconnecting'** (failure, with the reason and
the command), a toast when it starts and when the camera is back, and `state.video.engine.delivery`
(`mode`: live, fallback, reconnecting, starting, off). `streamstack doctor` adds a separate `video-chain` row that
says the sentence a person needs: "StreamStack Camera is not listed by Teams, Zoom or Chrome: nothing feeds
/dev/video43", naming the broken link (the source has no producer, or the engine is not running).

## The Console preview never reads the camera node (#181)

v4l2loopback answers a second opener's `VIDIOC_S_FMT` with `EBUSY` while another opener is streaming from the node.
The Console's live preview used to run `ffmpeg -f v4l2 -i /dev/video43` (and the same on `/dev/video42` for the Canon
tile) for as long as a page was open, so Chromium and Edge `getUserMedia` on StreamStack Camera failed with
`NotReadableError: Could not start video source` (strace: `S_FMT` returning `EBUSY`). A person with the Console open
could not start a call camera.

Now the engine's own graph feeds the preview. The program picture is split once, before the YUYV pack (the same seam
the encode taps use), shrunk to 1280 wide at 5 fps, JPEG-encoded and replaced atomically in
`$XDG_STATE_HOME/streamstack/preview/program.jpg` (`Spec.preview_path`). `console/video.py` `PreviewFile` reads that
file; a picture older than 2 s counts as no picture and the preview falls back to the engine's low-rate detection
frame. While no engine child runs, the slate (see Resilience) writes the same file at 2 fps next to the node, so the
preview shows the "Camera reconnecting" or idle card instead of a stale frame. The Canon tile uses the detection frame. The Console opens no `/dev/video*` node for the preview, and the
engine remains the only process that holds `/dev/video42` (reader) and `/dev/video43` (writer). No module parameter
changes and no reload are needed: the fix ships with the Console. The writer already sets one fixed format
(yuyv422 at the source size, see the format contract in [loopback-contract.md](loopback-contract.md)).

## Recording and streaming read the engine, not the node (#184)

Recording and streaming were the same bug: every encoder generation ran `ffmpeg -f v4l2 -i /dev/video43`, so a running
recording or stream made a call app's `VIDIOC_S_FMT` fail with `EBUSY` just as the preview did. They now read the
engine's own frames:

    engine ffmpeg --[rw] raw NV12 pipe--> FrameHub (engine/framehub.py) --unix socket--> every encoder (ffmpeg -i unix:...)

- The program graph is split before the YUYV pack (the seam the encode taps and the preview use) into a raw NV12 branch
  on a pipe. The Console's frame hub reads that pipe and serves it on `~/.local/state/streamstack/feed.sock`, next to
  `feed.sock.json`, which says what the socket carries (size, rate, pixel format).
- `stream/service.py` `source()` returns `Source("feed", ...)` whenever the hub describes itself, and the encoder
  command is `-f rawvideo -pix_fmt nv12 -use_wallclock_as_timestamps 1 -i unix:<socket>`: the same wall clock the
  loopback source was stamped with, so the relay's cut-over between encoder generations is unchanged. Several
  generations may read at once (the make-before-break swap).
- A reader joins on a frame boundary and gets the last frame at once. A reader that stops reading loses its own oldest
  frames (two-frame queue) and is dropped after 5 s; the engine never waits for it. A change of output size closes
  every reader (they would misparse) and the encoder generation restarts.
- While the engine sends nothing (camera lost, slate on the node) the hub repeats the last frame, so a running stream
  or recording holds a still picture instead of stalling.
- `STREAMSTACK_ENGINE_FEED=off` (default `on`) removes the branch and the encoders read the loopback again, as does any
  host where the hub is not up (the engine has not started yet).

Cost: one more NV12 conversion and about 93 MB/s through a pipe at 1080p30 while the engine runs, plus one memory copy
per frame in the hub; encoders only add a socket write each.

`streamstack doctor` still lists any other process holding the node. A leftover OBS virtual camera or a stray ffmpeg
reading `/dev/video43` has the same effect on a call app, so close those before a call.

## One format, whatever feeds the node

Call apps read the one format the program node offers, so the node must not follow a weak source down. When the Canon
is off USB the engine reads the Cam Link, which on some hosts captures 720x480 at 21.8 fps; the node used to be
renegotiated to 480p and `streamstack doctor` warned "offers only 480p". Now the engine scales the picture to fit
1280x720, centres it on black and repeats frames up to 30 fps, and the slate is written at the same format
(`engine/source.py` `output_plan`, `engine/graph.py` `output_stage`, #175). Sources at 720p or above and 24 fps or more
are passed through unchanged. The contract is in `docs/loopback-contract.md`.

## Idle when no app reads the camera

A running engine keeps the GPU, the lights and the Canon's mirror and sensor busy even when nobody looks at the picture.
So the engine rests, and wakes when something wants it (`engine/idle.py`, `console/native.py`).

- **States.** `running`, `idle`, `waking`. It idles after `STREAMSTACK_ENGINE_IDLE_AFTER` seconds (default 10) without a
  reader and without demand, and only while the engine is running well: a faulting engine is never put to sleep.
  `STREAMSTACK_ENGINE_IDLE=off` never idles. `STREAMSTACK_ENGINE_IDLE_IGNORE` lists process names that do not count as
  readers (a monitoring tool that keeps the node open).
- **Who counts as a reader.** Any process outside StreamStack that holds the program node (`/dev/video43`) open, found
  by scanning `/proc/<pid>/fd` (never by opening the device: a probe would count as a reader itself). inotify on the node
  is only the trigger for a scan; a safety scan runs every 3 s. A scan that fails or is stale counts as a reader: the
  sensor failing never puts the camera to sleep. PipeWire or WirePlumber holding the node open would keep the engine
  awake; the Console shows who holds it (`state.video.engine.idle.readers`).
- **Demand inside StreamStack.** The Console preview, a running stream or recording read the node through StreamStack's
  own processes, which the scan treats as plumbing, so they ask for the picture explicitly. Preview demand lasts 6 s
  after the last frame was asked for.
- **Idle.** The engine child stops. The "Camera idle" placeholder (the same slate writer, at 5 fps, no warning, no toast)
  holds the node, so `exclusive_caps=1` still lists StreamStack Camera for Teams and Edge. The Canon bridge ends live view
  (`rest` request: `viewfinder=0` on the shared libgphoto2 session, or stopping `gphoto2 --capture-movie`) and keeps
  re-sending its last frame so `/dev/video42` never goes dark. The Camera page shows the Canon card as Idle with the
  reason; its controls are locked until live view returns.
- **Rest is a lease.** The Console renews it every 5 s; the bridge resumes live view by itself 15 s after the last
  renewal, so a Console that dies never leaves the body dark.
- **Wake.** A reader seen for 0.25 s wakes the engine (an Edge probe that opens and closes the device in less than that
  does not flap it). The Console resumes the Canon live view, the engine's guard releases the placeholder and starts
  ffmpeg. `video.engine.idle.wake_ms` is the time from the decision to a running engine; the first real frame follows
  within one frame interval. Measure the whole path with the latency rig (#89).
- **Keep awake.** The switch on the Camera page (`video.keepalive`, remembered across restarts) turns idling off for now.
- **Where it shows.** The `ENGINE idle` chip on the Camera page, `state.video.engine.state` = `idle` with
  `state.video.engine.idle` (`mode`, `state`, `keep_alive`, `after_s`, `since_s`, `reason`, `wakes`, `wake_ms`,
  `readers`, `sensor`, `demand`), `state.video.canon.state` = `idle`, `state.video.engine.delivery.mode` = `idle`, and the
  `engine-idle` row of `streamstack doctor`. With `STREAMSTACK_CAMERA_LOSS=off` nothing may write the node but the
  engine, so the Console does not idle in that mode.
- **Switching the program camera off and on** leaves idle at once; the camera switch reads on while idle because the
  camera is still listed.

Fixing the feeder itself (the restart policy of `canonctl-ptp-webcam.service` and its ffmpeg logging) is the canonctl
repository's job; StreamStack only guarantees the node stays listed while that is down.

## The PipeWire path (#88)

`StreamStack Camera` is also a PipeWire `Video/Source`, published by WirePlumber, not by StreamStack (ADR 0004
addendum). Check it: `pw-dump | python3 -c "import sys,json;[print(o['id'],o['info']['props'].get('node.nick')) for o in json.load(sys.stdin) if o.get('info',{}).get('props',{}).get('media.class')=='Video/Source']"`
or read the `video-pipewire` row in `streamstack doctor`. Verified on dm313red and deskmeet1's OS image
(PipeWire 1.6.2, WirePlumber): the three v4l2 cameras including StreamStack Camera are Video/Source nodes. Legacy apps:
`pw-v4l2 <app>`. Chromium lists PipeWire cameras only with the flag above; Firefox with `media.webrtc.camera.allow-pipewire`.
Zoom and Teams use the v4l2 node directly; whether any build uses the portal is a question for a person on a call.

**After a boot it is not published until WirePlumber probes the node again (#176).** With `exclusive_caps=1` the
loopback offers only OUTPUT capability until the Console writes it. WirePlumber probes a device when udev announces it
(module load, early boot) and v4l2loopback sends no uevent when a writer attaches, so WirePlumber keeps the stale
OUTPUT-only answer (`api.v4l2.cap.capabilities` ending in `2` in `pw-dump`, while `v4l2-ctl -d /dev/video43 --info`
says Video Capture) and lists only the Cam Link. deskmeet1 showed exactly this (WirePlumber started at boot, the node
fed later); dm313red showed the camera only because its WirePlumber had been restarted after the node was fed. No
modprobe or WirePlumber config differs between the hosts. `streamstack camera-publish` is the fix: it waits until the
node is fed, does nothing when PipeWire already lists it, refuses while an app holds `/dev/video43` or any program
other than StreamStack captures audio (exit 3; `--force` overrides), and otherwise runs
`systemctl --user restart wireplumber` once and waits for the node to appear. The workstation role runs it after
`streamstack-console.service` starts (`streamstack-camera-publish.service`, user unit); `streamstack doctor` names it in
the `video-pipewire` hint.

