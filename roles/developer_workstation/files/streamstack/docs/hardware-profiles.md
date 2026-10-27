# Hardware profiles and probing

How StreamStack learns what is plugged into a machine and what it does with that. The decision is
[ADR 0003](adr/0003-hardware-profiles-and-capability-probing.md); the recorded inventories of the two hosts are in
[docs/hardware](hardware/README.md).

## Layers

1. **Probe** (`streamstack.hardware.probe.snapshot()`): reads USB, PCI, DRM render nodes with their VA-API
   entrypoints, ALSA cards and streams, PipeWire cards, nodes and profiles, V4L2 capture nodes and formats,
   v4l2loopback and the ffmpeg encoders. Each source is a runner (reads a file or runs a tool) and a pure parser
   (`hardware/parsers.py`). A source that cannot be read reports `denied` (the file or tool exists but this user may
   not use it) or `absent` (not installed, not present) with the reason, and the rest still run.
2. **Profiles** (`src/streamstack/data/hardware/*.toml`): device profiles (audio interfaces, GPUs, capture cards,
   controls) and rig profiles. The loader rejects unknown keys, duplicate ids and a wrong `schema` version, so a typo
   is a startup error naming the file and key, never a silent default.
3. **Plan** (`streamstack.hardware.plan.resolve(snapshot, catalogue, overrides)`): a pure function. It chooses the
   rig, assigns GPU roles (`grade`, `decode`, `encode`, `blur`), picks the encoder, the audio interface and its rate,
   the capture card and the loopback pair. Nothing in a Plan depends on a `/dev` number, a hostname or the order the
   kernel enumerated devices: an Arc on `renderD128` and on `renderD129` give the same Plan apart from the path.

Other code asks the Plan, for example `plan.gpu("encode").node_path` or `plan.audio_interface()`.

## Choosing the rig

`STREAMSTACK_RIG` (settings file or environment; the environment wins) names a rig. With `auto`, each rig is scored by
the devices it expects: +3 per required device present, +1 per optional one, -3 per required one missing, and a rig
with at most half its required devices present is not eligible. A tie is broken by `machine_ids` in the rig file (a
hash of `/etc/machine-id` or the DMI uuid, printed by `streamstack hardware --identity`), then by file order. When no
rig is eligible the generic Linux fallback applies. The hostname is never read.

## Hot-plug

`udevadm monitor --udev` (subsystems usb, drm, sound, video4linux) marks the hardware as possibly changed. One probe
runs after a quiet second; a change is published only when the new snapshot differs, with the plan sections that
changed. Without `udevadm` the same probe runs every 10 seconds.

## Inspecting a machine

    streamstack hardware            # the rig, devices, GPU roles, encoder, audio
    streamstack hardware --json     # the same as the Console's `hardware` state
    streamstack hardware --identity # this machine's hash, for `machine_ids`

    streamstack doctor --hardware   # the hardware group alone: each device against the rig, exit 1 on a failure
    streamstack doctor --json       # the rig header, a summary and every check (hardware group first)

`doctor` opens with `rig: <id> (selected by <reason>)`. The hardware group checks the live snapshot against the
resolved rig: each device present or absent (a missing required device is a failure, a missing optional one is fine),
a device the user may not open is `denied` with the fix (the `render` or `video` group, a seat login), the GPU roles
and the encoder, the audio interface at its rate and format, the capture card's modes, and the loopback devices found
by label. Every row that is not good names the fix. An audio interface the rig prefers that is not enumerating on USB (the
Scarlett 2i2 on the deck studio) is a warning, not a failure and not green: nothing on the host is broken, but there is
no call microphone, and a failure would block verifying an install on that machine. The hint is the owner's physical
action: re-seat it in a direct rear USB port with a short known-good cable.

The Console shows it on the System page (Hardware card, and the Hardware group of the health checks). The demo rig replays the packaged recordings in
`src/streamstack/data/hardware/recordings/` (a recorded inventory reduced to the sections the replay reads) and can
switch between the two hosts.

## Adding hardware

Add a device profile to the matching TOML file (USB or PCI ids, the class, what it provides) and list it in the rigs
that use it; `tests/test_hw_schema.py` validates the shipped files. To add a recorded host, put its inventory at
`tests/fixtures/hardware/<host>/inventory.txt` (no serial numbers, MAC addresses or usernames), regenerate the
recording with `inventory.trim`, and add the host to `tests/hardware_support.HOSTS`; the parser and Plan tests
parametrise over it.

## Which GPU does which job (#119)

Every video job asks the Plan for the GPU of its role and takes that GPU's render node, never a node number and never
a host name (`streamstack.hardware.roles.choose`):

| Job | Role | Setting that wins |
| --- | --- | --- |
| Native engine graph and its VA-API MJPEG decode (it shares the graph's device), QSV encode taps | `grade` | `STREAMSTACK_ENGINE_RENDER_NODE` |
| Background blur (OpenVINO, found by `GPU_DEVICE_ID`, not by `GPU.n`) | `blur` | `STREAMSTACK_ENGINE_RENDER_NODE` |
| Stream encoders AV1 / HEVC / H.264 (VA-API, QSV) | `encode` | `STREAMSTACK_STREAM_RENDER_NODE` |
| Canon bridge MJPEG decode | `decode` | `STREAMSTACK_CANON_RENDER_NODE` |

On dm313red every role is the Arc on `renderD128`. On deskmeet1 the UHD 770 enumerates first (`renderD128`) and the
Arc is `renderD129`: grade, encode and blur follow the Arc (so AV1 and every other codec are encoded there), and the
Canon decode takes the iGPU, which is what the `studio-deck` rig prefers for `decode`. With no profile match the
generic choice applies as before: the first Intel render node, a discrete GPU before the integrated one (PCI slot
`00:02.x`), else the first node. Stream trials record the PCI id of the GPU each one ran on, and the probe cache is
keyed by node and PCI id, so a renumbering after hot-plug is probed again and the role follows the PCI id.
`streamstack hardware` prints a `video` line per job with the node, GPU and why (`rig`, `override`, `generic`).

## Cameras and virtual cameras (#120)

Capture cards and the Canon are found by the USB ids in the device profiles (`capture.toml`): the Cam Link 4K lists
both `0fd9:0067` (both recorded hosts) and `0fd9:0066`; `system.find_camlink` and the Canon discovery use those ids.
Each rig has a `[rig.loopback]` block (`max_buffers` and one `[[rig.loopback.device]]` per `camera` / `program` role with
`video_nr`, `card_label`, `exclusive_caps`). The nodes are found by label, the profile's number is only where a missing
node goes, and `packaging/modprobe.d/streamstack-v4l2loopback.conf` is pinned by a test to the line the profile builds.

    streamstack loopback status                       # per role: ok / missing / mislabelled, and the wanted options
    streamstack loopback apply [--root DIR] [--dry-run]   # write the modprobe.d and modules-load.d files; add a missing
                                                          # node on a loaded module with v4l2loopback-ctl

`apply` is idempotent and never changes a node that exists; reloading the module needs its users stopped and is left to
the person.

## Scarlett: no controls, and why (#130)

The in-kernel `scarlett2` mixer driver creates no controls when the interface is in Mass Storage Device ("Easy Start",
MSD) mode, and none when its firmware is older than the first configuration set it knows (2115 for the 2i2 Gen 4;
firmware 2417 moved the direct-monitor gain). It still creates `Firmware Version`, `Minimum Firmware Version` and, while
MSD is on, `MSD Mode Switch`, and it logs `Focusrite <series> firmware version N is too old; need M` and
`Focusrite <series> Mixer Driver enabled (pid=0x...)` / `disabled by modprobe options`. `streamstack doctor` reads those
(`amixer contents` of the Scarlett's card, then `journalctl -k`, then `dmesg`; read-only, the log only when the controls
are not already healthy) and the `hw-scarlett` row names the state and the fix:

| State | Row | Fix printed |
|---|---|---|
| not on USB | covered by `hw-audio-interface`; `hw-scarlett` warns only when the kernel log has USB errors, and quotes them | re-seat in a direct rear port |
| on USB, no sound card | fail, quotes the kernel's USB error lines | another rear USB 3 port and cable, `journalctl -k` |
| card, no scarlett2 controls | fail: MSD on, firmware too old, driver switched off by `device_setup`, or unknown; quotes the kernel line | hold 48V for five seconds at power-on or `amixer ... 'MSD Mode Switch' off` and power-cycle; the `scarlett2 update` command; remove `device_setup=1` |
| healthy | ok: firmware, driver minimum and MSD state | none |

StreamStack never flashes, resets or power-cycles the interface and links nothing GPL-3.0: the update tool
(geoffreybennett/scarlett2) is only named in the printed command, and the firmware files are not bundled. The kernel log
needs the `systemd-journal` group; without it the row says so and judges from the controls alone. The Console's System
page re-reads the evidence at most every 30 s. `tests/fixtures/hardware/scarlett2/` holds the synthesized listings and
log lines (derived from the driver's format strings); the capture from the real 2i2 is posted on #130 once it enumerates.

## Not part of this layer

The audio chain (#118) and `doctor` (#122) read the Plan; the engine, the stream and the camera setup read it as
described above (#119, #120).
