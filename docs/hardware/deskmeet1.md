# deskmeet1

Shared workstation (unix user `mrh`, uid 1001) with the same capture and lighting hardware but no external audio
interface and two Intel GPUs. Captured 2026-10-24 12:09 local; raw capture in
`tests/fixtures/hardware/deskmeet1/inventory.txt`. Compared with dm313red in [README](README.md).

## Platform
- Ubuntu 26.04.1, kernel 7.0.0-38-generic, PipeWire 1.6.2 with WirePlumber, v4l2loopback 0.15.3 (DKMS, loaded).
- Intel Core i7-14700, 62 GB RAM, 16 GB swap file and 31 GB zram, up 5 days. About 21 agent processes, the user
  slice at 4.6 GB of 56 GB. `hyperharness doctor --fleet` is green except that 10 sessions run outside the slice.

## Audio
- **No external audio interface** has appeared in the kernel log in five days; only the Cam Link and the Stream
  Deck enumerate on USB.
- Cards: Realtek ALC897 on the PCH (analog out, HDMI 0 to 3, analog capture, alt analog capture), the Arc's HDMI
  audio, and the Cam Link audio (capture, S16_LE, 2 channels, 48000 Hz). Every card is at profile `off`, so
  PipeWire exposes no sinks or sources except **Dummy Output** (the default).
- Configured default sink is the PCH HDMI stereo node and the default source is the PCH analog stereo node;
  neither exists while the profiles are off.
- No `streamstack_mic` filter node is loaded; no StreamStack configuration exists in `~/.config/streamstack`.

## Cameras
- Elgato Cam Link 4K `0fd9:0067`: `/dev/video0` (capture, YUYV 1920x1080 at 29.97 fps, the only mode while the
  HDMI source is as connected) and `/dev/video1` (metadata). Controls: brightness -128 to 127, contrast 0 to 255,
  saturation 0 to 255, hue -128 to 127.
- Canon EOS 50D `04a9:319b`: v4l2loopback `/dev/video42` ("Canon EOS 50D", 1280x720 as last negotiated).
- Program output `/dev/video43` ("StreamStack Camera", 640x480 default, no producer).
- Loopback options identical to dm313red.

## GPU and encode
- **Two render nodes.** `renderD128` is the Raptor Lake-S UHD 770 iGPU (`8086:a780`, i915): H.264 High and HEVC Main and
  Main10 encode, no AV1 encode. `renderD129` is the Arc A380 (`8086:56a5`): the same plus AV1 Profile0 encode. Both use iHD 26.1.2.
- The default `vainfo` display call opens the iGPU first, so an unqualified probe understates the machine.
- No NVIDIA, NPU, `/dev/accel`, `vulkaninfo`; OpenVINO not importable from system Python.

## Lights and controls
- Logitech Litra Beam `046d:c901` over USB HID; no lights service running and no lighting config yet.
- Elgato Stream Deck original V2 `0fd9:006d` (hidraw), plus udev rules for it.
- No network lights discovered.

## Gaps to close in the first on-device pass
OpenVINO inside the StreamStack environment, `vulkaninfo`, and the Scarlett 2i2 once it enumerates (README, item 2).
