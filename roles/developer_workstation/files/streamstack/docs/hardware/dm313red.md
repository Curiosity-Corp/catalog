# dm313red

Studio host with the Elgato Wave XLR. Captured 2026-10-24 12:09 local; raw capture in
`tests/fixtures/hardware/dm313red/inventory.txt`. Compared with deskmeet1 in [README](README.md).

## Platform
- Ubuntu 26.04.1, kernel 7.0.0-38-generic, PipeWire 1.6.2 with WirePlumber, v4l2loopback 0.15.3 (DKMS, loaded).
- Intel Core i7-14700, 62 GB RAM, 16 GB swap file. Python 3.14.4, ffmpeg, gphoto2, gst-launch, pipx, OBS present
  (OBS goes away with !48).

## Audio
- **Elgato Wave XLR**, `0fd9:007d`, ALSA card `XLR`, USB full speed on the xHCI at `00:14.0`.
  - Playback: S24_3LE, 2 channels, 48000 and 96000 Hz, asynchronous with a sync endpoint, no implicit feedback.
  - Capture: S24_3LE, 1 channel (mono), 48000 and 96000 Hz. PipeWire runs the card at the profile
    `output:analog-stereo+input:mono-fallback`, graph rate 48000 Hz s24le.
  - Vendor USB control (gain, mute, 48V, headphone volume) is handled by `hardware/wavexlr.py`.
  - Capture hold (issue #126). The card's capture and playback endpoints share one full-speed link, and a reported
    Linux bug returns bit-exact zeros for the life of a capture stream that was opened after a playback stream (capture
    first is fine). StreamStack ships its own WirePlumber drop-in,
    `packaging/wireplumber/wireplumber.conf.d/52-streamstack-wave-xlr-capture-hold.conf` (`node.always-process = true`
    on the XLR capture node, matched by node name), installed to `/etc/wireplumber/wireplumber.conf.d` by `make
    install`; `streamstack doctor` reads the effective property from `pw-dump` and warns with the fix when it is
    absent. Cost: the capture endpoint streams continuously (about 144 kB/s of USB) and the device is never idle; the
    call privacy mute stays in the voice chain. Ownership: the workstation role owns
    `50-curiosity-streamstack-alsa.conf` (it sets `node.pause-on-idle = false` on these nodes); the StreamStack file
    agrees with it and never edits it. Whether the bug reproduces in our `output:analog-stereo+input:mono-fallback`
    profile is unproven: run `python3 tools/xlr_capture_order.py --cycles 20` (playback first) and `--capture-first`
    (the control) with the voice chain and calls stopped, before and after installing the drop-in. Manual repro:
    start `pacat` on the XLR sink, then `parec` on its source, and look for a capture of only zeros.
  - Digital silence and the cached gain. The kernel caches `Mic Capture Switch`/`Mic Capture Volume`/`PCM Playback
    Volume` and never reads them back (an owner's archive of another MK.1 showed 74 dB cached against 48 dB in the
    device block). `streamstack mic-revive` re-writes them (see docs/console.md, Mic watchdog).
- Elgato Cam Link 4K audio: capture only, S16_LE, 2 channels, 48000 Hz. Never a call microphone.
- Onboard Raptor Lake HDA and the Arc's HDMI audio: card profile `off`.
- Defaults: sink is a Bluetooth headset, source is the Wave XLR mono capture. `streamstack_mic` filter node loaded.

## Cameras
- Elgato Cam Link 4K `0fd9:0067`: UVC, `/dev/video0` (capture) and `/dev/video1` (metadata). Formats and controls
  were not captured because the node is not readable from a non-seated SSH session (ACL for the seat user only).
- Canon EOS 50D `04a9:319b`: gphoto2 bridge into `/dev/video42` ("Canon EOS 50D", v4l2loopback).
- Program output: `/dev/video43` ("StreamStack Camera", v4l2loopback).
- Loopback options: `devices=2 video_nr=42,43 card_label="Canon EOS 50D,StreamStack Camera" exclusive_caps=1,1 max_buffers=2`.

## Canon 50D picture paths: the measured matrix (#129)

Status: **tooling ready, nothing measured yet.** The numbers need the camera, the Cam Link and the owner at the desk
for the camera menus, and a measurement reads the picture while nobody is on a call. Until the table below is
filled, the working default for calls stays path A (what runs today); the README's "HDMI is sharper" remains a
claim, not a result.

| Path | What |
| --- | --- |
| A | USB live view through libgphoto2 into `/dev/video42` (today; 1056x704, about 20 fps by the README) |
| B | HDMI into the Cam Link 4K as the camera sends it (live view with the AF frame and overlays, per the #120 session) |
| C | HDMI with overlays removed (Magic Lantern "Kill Canon GUI" style option; unverified on a 50D) |

Procedure (owner at the desk, no call, same static evenly lit scene for every row, ISO 800 then 1600 set on the
camera). `tools/canon_path_matrix.py` is read-only on the device and refuses during a call:

```
python3 tools/canon_path_matrix.py capture --path A --device /dev/video42 --iso 800 --overlay no   # dry run first
python3 tools/canon_path_matrix.py capture --path A --device /dev/video42 --iso 800 --overlay no --execute \
    --cpu-of streamstack --latency-ms <from the #89 bench> --out paths.jsonl
# repeat for --iso 1600, then path B with --device /dev/video0 (the Cam Link), then C if approved
python3 tools/canon_path_matrix.py table paths.jsonl      # paste the output here
```

It records delivered fps (`v4l2-ctl --stream-mmap`, median per second), the negotiated frame, the active picture
(ffmpeg `cropdetect`, so black bars do not count), noise (mean luma difference between consecutive frames on the
static scene; lower is quieter, comparable only within one scene), CPU of a named process, and the overlay and
latency the owner observed.

Path C needs the owner's go-ahead: Magic Lantern runs from the SD card and is undone by removing its files from the
card (or booting a card without them) and, if installed through the camera menu, by re-running Canon's firmware
update; record the build and the camera firmware in `--note`. It is not verified that clean HDMI works on a 50D.

| Path | ISO | Delivered fps | Frame | Active picture | Overlay | Noise | Latency ms | CPU % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 800 | not measured | not measured | not measured | no | not measured | not measured | not measured |
| A | 1600 | not measured | not measured | not measured | no | not measured | not measured | not measured |
| B | 800 | not measured | not measured | not measured | not measured | not measured | not measured | not measured |
| B | 1600 | not measured | not measured | not measured | not measured | not measured | not measured | not measured |
| C | 800 | not approved | | | | | | |

Default for calls: path A until the table says otherwise. Quiet mode: not decided. The Camera page and presets (#96)
follow the named paths once they exist.

## GPU and encode
- Intel Arc A380 (DG2, `8086:56a5`, i915) is the only GPU and is `renderD128`. iHD 26.1.2, VA-API 1.23.
- Encode on the Arc (per-node `vainfo`): H.264 High, HEVC Main and Main10 and AV1 Profile0, all low-power
  (`EncSliceLP`). The full profile list is in the fixture.
- ffmpeg encoders present: `av1_vaapi`, `av1_qsv`, `h264_vaapi`, `h264_qsv`, `hevc_vaapi`, `hevc_qsv`, Vulkan
  encoders, NVENC names (unusable, no NVIDIA), `libsvtav1`, `libx264`.
- No NVIDIA, no NPU, no `/dev/accel`, `vulkaninfo` not installed, OpenVINO not importable from system Python.

## Lights and controls
- Logitech Litra Beam `046d:c901` over USB HID; `streamstack-lights.service` is active.
- No Stream Deck. No network lights discovered (no mDNS tooling installed; the lighting code is USB only).

## Gaps to close in the first on-device pass
Cam Link formats and controls, `vulkaninfo`, and OpenVINO inside the StreamStack environment, from a seated session.
