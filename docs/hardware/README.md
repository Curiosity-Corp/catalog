# Studio hardware: dm313red and deskmeet1

Read-only inventory taken 2026-10-24 on both studio hosts (Ubuntu 26.04.1, kernel 7.0.0-38-generic, PipeWire and
WirePlumber 1.6.2, v4l2loopback 0.15.3 DKMS, ffmpeg with VA-API, QSV, NVENC, Vulkan and software encoders,
Intel iHD 26.1.2). Per-host detail: [dm313red](dm313red.md), [deskmeet1](deskmeet1.md). The raw, scrubbed captures
(no addresses, serials or MACs) are the recorded fixtures in `tests/fixtures/hardware/<host>/inventory.txt`; the
hardware-profile tests replay them (see [ADR 0003](../adr/0003-hardware-profiles-and-capability-probing.md)).

## Side by side

| Area | dm313red | deskmeet1 | Consequence for the app |
|---|---|---|---|
| CPU | Core i7-14700, 28 threads | Core i7-14700, 28 threads | none |
| RAM | 62 GB, 16 GB swap file | 62 GB, 16 GB swap file plus 31 GB zram | none; both run ~20 agent sessions |
| Render nodes | one: `renderD128` is the Arc A380 (`8086:56a5`, i915) | two: `renderD128` is the UHD 770 iGPU (`8086:a780`), `renderD129` is the Arc A380 (`8086:56a5`) | the render node number cannot be configured; select the GPU by PCI id |
| AV1 hardware encode | Arc: yes | Arc: yes, iGPU: no (H.264 and HEVC only) | encoder choice is per GPU; "first node that can encode" lands on the iGPU |
| Other encode paths | none (no NVIDIA, no NPU, no `/dev/accel`) | none | CPU fallback is libsvtav1 and libx264 |
| OpenVINO / NPU | not installed, no accelerator node | not installed, no accelerator node | blur and framing on the Arc need the `blur` extra on both |
| External audio interface | Elgato Wave XLR (`0fd9:007d`): playback and capture, 48000 and 96000 Hz, S24_3LE, capture mono, playback stereo | **none present** (no USB audio interface in the kernel log for 5 days) | DAC graph and native rate are per interface; deskmeet1 has nothing to match yet |
| Onboard audio | Raptor Lake HDA (`8086:7a50`) and Arc HDMI audio, both card profile `off` | Realtek ALC897 on the PCH, plus Arc HDMI audio, all card profiles `off` | cards with profile `off` expose no sinks; deskmeet1 falls to PipeWire's Dummy Output |
| Default sink and source | Bluetooth headset sink, Wave XLR mono source | Dummy Output sink, no source | the call mic and monitor must be chosen by profile, not by default |
| Capture card audio | Cam Link 4K, S16_LE 2ch 48000 Hz only | Cam Link 4K, S16_LE 2ch 48000 Hz only | identical; never a call microphone |
| Voice filter chain | `streamstack_mic` filter node loaded | not loaded | generated per profile (#90) |
| Capture card | Elgato Cam Link 4K (`0fd9:0067`), `/dev/video0` and `video1` | Elgato Cam Link 4K (`0fd9:0067`), `/dev/video0` and `video1` | same model, different serial; the node number still moves, match by USB id |
| Cam Link modes | not captured: `/dev/video0` is not readable from a non-seated SSH session | 1080p29.97 YUYV only (set by the HDMI source) | modes are probed live; the fixture for dm313red is recorded by the session on the console |
| Camera | Canon EOS 50D over USB (`04a9:319b`), gphoto2 | Canon EOS 50D over USB (`04a9:319b`), gphoto2 | same |
| v4l2loopback | options `devices=2 video_nr=42,43 exclusive_caps=1,1 max_buffers=2`, labels "Canon EOS 50D" and "StreamStack Camera" | identical options | same; the nodes exist on both (`video42`, `video43`) |
| Lights | Logitech Litra Beam (`046d:c901`) over USB HID; lights service active | Litra Beam over USB HID; no StreamStack config or lights service | no network lights on either host; Litra discovery is USB only |
| Stream Deck | none | Stream Deck original V2 (`0fd9:006d`) | the deck service applies on deskmeet1 only |
| Wireless dongles | Logitech Lightspeed and a Telink receiver | Logitech Unifying and Lightspeed | irrelevant |
| StreamStack | 0.3.1 installed (old numbering), config and lights service in place | 0.3.1 installed, no config, no services | both need the CI-built 0.0.8 release |
| udev rules | `60-curiosity-streamstack*.rules`, Litra, Stream Deck | same four files | same |
| Seat | `mrh` uid 1000, video group not needed (seat ACL) | `mrh` uid 1001 (the shared workstation), video group member | device access is by uaccess ACL or group; the probe must report a permission error as such |

## What the diff means

1. **The GPU layout is the real divergence.** The native engine documents `renderD128` as the Arc. On deskmeet1
   that node is the iGPU, so the graph would run libplacebo on the UHD 770 and any "first encoder" logic would
   pick H.264 instead of AV1. The Arc must be selected by PCI id `8086:56a5`, resolved to a node at start.
2. **The audio interface is not the same machine twice.** dm313red has the Wave XLR; deskmeet1's interface is a
   Focusrite Scarlett 2i2 (owner decision 2026-10-24), but it is not enumerating on USB and its three sound cards are
   all switched off, so it is on a dummy sink. Re-seating it in a direct rear USB port is an owner action.
3. **Everything else is the same hardware** (CPU, Cam Link, Canon, Litra, loopback nodes), so one profile can
   serve both and only the GPU order, the audio interface and the Stream Deck differ.
