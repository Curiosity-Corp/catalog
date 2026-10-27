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
- Elgato Cam Link 4K audio: capture only, S16_LE, 2 channels, 48000 Hz. Never a call microphone.
- Onboard Raptor Lake HDA and the Arc's HDMI audio: card profile `off`.
- Defaults: sink is a Bluetooth headset, source is the Wave XLR mono capture. `streamstack_mic` filter node loaded.

## Cameras
- Elgato Cam Link 4K `0fd9:0067`: UVC, `/dev/video0` (capture) and `/dev/video1` (metadata). Formats and controls
  were not captured because the node is not readable from a non-seated SSH session (ACL for the seat user only).
- Canon EOS 50D `04a9:319b`: gphoto2 bridge into `/dev/video42` ("Canon EOS 50D", v4l2loopback).
- Program output: `/dev/video43` ("StreamStack Camera", v4l2loopback).
- Loopback options: `devices=2 video_nr=42,43 card_label="Canon EOS 50D,StreamStack Camera" exclusive_caps=1,1 max_buffers=2`.

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
