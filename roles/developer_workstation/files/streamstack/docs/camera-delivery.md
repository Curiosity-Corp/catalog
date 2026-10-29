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
4. **Fail over to the Cam Link** (`STREAMSTACK_CAMERA_FALLBACK=auto`, default). When the picked camera has had no producer
   for 6 s and another camera delivers, the engine is re-pointed at it, and goes back after the first camera has been
   steady for 5 s. `off` keeps the picked camera and relies on the slate. The Cam Link is only used when it is
   connected; with no HDMI signal it delivers no frame, the stall watchdog stops it and the slate covers.

Where it shows: the Console's System page row **StreamStack Camera is fed by ...** (ok), **... is on Cam Link 4K, not
Canon EOS 50D** (warning, with the restart command), **... showing 'camera reconnecting'** (failure, with the reason and
the command), a toast when it starts and when the camera is back, and `state.video.engine.delivery`
(`mode`: live, fallback, reconnecting, starting, off). `streamstack doctor` adds a separate `video-chain` row that
says the sentence a person needs: "StreamStack Camera is not listed by Teams, Zoom or Chrome: nothing feeds
/dev/video43", naming the broken link (the source has no producer, or the engine is not running).

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

