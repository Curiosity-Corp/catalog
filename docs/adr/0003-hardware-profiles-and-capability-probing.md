# ADR 0003: hardware profiles and capability probing

Status: proposed. Date: 2026-10-24. Owner: the StreamStack session. Evidence: [docs/hardware](../hardware/README.md).

## Context

StreamStack must run, to the same standard, on two studio hosts with near-identical hardware: dm313red and
deskmeet1 (Core i7-14700, Arc A380, Cam Link 4K, Canon EOS 50D, Litra Beam, v4l2loopback). The inventory shows
three real differences and one risk:

1. **GPU order.** deskmeet1 has a UHD 770 iGPU as `renderD128` and the Arc as `renderD129`; dm313red has only the
   Arc, as `renderD128`. The native-video-engine ADR and `STREAMSTACK_ENGINE_RENDER_NODE=""` ("picks the Intel
   GPU") both assume `renderD128` is the Arc. On deskmeet1 that puts the grade graph on the iGPU, and "first node
   that can encode" gives H.264 instead of AV1.
2. **Audio interface.** dm313red uses an Elgato Wave XLR (48000 and 96000 Hz, S24_3LE, mono capture). deskmeet1 has
   no external interface and every sound card is at profile `off` (Dummy Output). `audiochain/graph.py` has
   `RATE = 48000` as a constant and the call microphone is chosen by a name heuristic.
3. **Stream Deck** is present only on deskmeet1.
4. **Risk.** Fixing this with `if hostname == ...` would rot the first time a device moves between hosts, which can
   happen at any time (the Wave XLR is the obvious candidate).

What exists today: `hardware/profiles.py` (a USB-id catalogue of audio-interface drivers, selected by
`STREAMSTACK_INTERFACE`), `encoder.py` (VA-API probe over every render node, OBS-keyed, removed by !48), `system.py`
(USB-id camera lookup), `lighting.py` (sysfs hidraw discovery) and `cli doctor` (structured checks in groups video,
audio, apps). Each probes its own corner and none of them knows what the rig is supposed to look like.

## Decision

Hardware knowledge becomes **data plus one probe**, resolved once per start and again on every hot-plug event.

### 1. Probe layer: `streamstack.hardware.probe`

One function, `snapshot() -> Snapshot`, returns an immutable, serialisable record of what is present: USB devices
(vid, pid, bus path, node permissions), DRM render nodes (PCI id, driver, node path, VA-API encode and decode
entrypoints per node, Vulkan device when available), ALSA cards with their stream capabilities (rates, formats,
channels, from `/proc/asound/card*/stream*`), PipeWire cards with active profile and nodes, V4L2 capture nodes with
formats, and v4l2loopback devices. Hardware is identified by **PCI id and USB id, never by `/dev` number or
hostname**. Every source is a small runner (reads a path or runs a tool with the existing `system.run` timeout) plus
a pure parser, so each source can fail alone: the snapshot records `{source, ok, error}` and the rest still works.
A permission error is reported as `denied`, not `absent`.

### 2. Profile schema: data, versioned, validated

Profiles live in `src/streamstack/data/hardware/*.toml` and are loaded and validated at start (schema version,
unknown keys rejected, ids unique). Two kinds:

- **Device profile** (what a device can do): `id`, `class` (`audio-interface`, `capture-card`, `gpu`, `light`,
  `deck`, `camera`), `match` (USB ids or PCI ids), and capabilities. Audio interface: `rates`, `formats`, `channels`
  per direction, `preferred_rate`, `driver` (the existing driver family, so `profiles.py` entries move here without
  changing the Console), `clock` notes (async USB). GPU: `encoders` in preference order with the VA-API profile and
  entrypoint each needs, and `roles` (`grade`, `encode`, `decode`, `blur`). Capture card: wanted modes in order.
- **Rig profile** (what a studio is expected to contain): `id`, `expects` a list of device profile ids marked
  `required` or `optional`, and `preferences` (which GPU role goes where, which interface is the call microphone).
  A rig is selected by `STREAMSTACK_RIG` when set, else by the best overlap with the snapshot, with the machine
  identity (a hash of `/etc/machine-id` or the DMI product uuid) only as a tie-breaker listed inside the rig file.
  The hostname is never read. Two rigs ship, `studio-wave-xlr` (dm313red today) and `studio-deck` (deskmeet1
  today); no host is named in code.

### 3. Resolution: snapshot plus profiles gives a `Plan`

`resolve(snapshot, profiles, overrides) -> Plan` is a pure function. Precedence is explicit environment settings,
then the rig profile, then device profiles, then **generic Linux defaults** (any VA-API encoder else CPU, the
default PipeWire source and sink, any UVC capture node, 48000 Hz). The Plan names:

- **Audio:** the call-microphone and monitor nodes, the graph sample rate and bit depth (the intersection of the
  interface's `rates` and the chain's preference, so a Wave XLR runs at its native 48000 Hz s24le and a 96000 Hz
  interface is reported honestly rather than silently resampled), and the generated filter-chain parameters (#90).
  `RATE` in `audiochain/graph.py` becomes a Plan input; the conferencing path stays at 48000 Hz.
- **Video:** the render node for each role by PCI id (grade, encode and blur on the Arc; decode and thumbnails may
  use the iGPU), the encoder (AV1 on the Arc, else HEVC, else H.264, else SVT-AV1 or x264 on the CPU) with the
  settings the existing bitrate rules give, and the camera modes, with the loopback nodes found by label rather than
  by number.
- **Devices:** the deck and lights enabled when present.

The Plan is the only thing the engine, the audio chain and the encoder read. `STREAMSTACK_ENGINE_RENDER_NODE=""` now
means "resolved by profile"; an explicit node still wins.

### 4. Hot-plug

A supervised monitor (`udevadm monitor` on the `usb`, `drm`, `sound` and `video4linux` subsystems, plus PipeWire
card-profile events from the existing micwatch path) debounces for one second, takes a new snapshot, re-resolves,
and applies only the **diff** of the Plan: an interface appearing regenerates the voice chain (guarded by the
existing `GRAPH_VERSION`), a GPU change restarts the engine child, a Cam Link replug re-probes modes. A device that
leaves degrades the Plan to the next fallback and shows as a doctor warning, never a crash.

### 5. `streamstack doctor` hardware group

`doctor` gains a `hardware` group (and `streamstack doctor --hardware --json`): the detected snapshot against the
selected rig, one row per expected device (ok, missing, wrong mode, denied), the resolved Plan (which GPU for which
role, the encoder, the audio rate and bit depth), and the fallback in use when a preferred item is absent. The
Console renders the same rows as cards. The exit code is non-zero on any `fail`.

### 6. Tests

The two hosts' scrubbed captures are committed as fixtures (`tests/fixtures/hardware/<host>/inventory.txt`). Parsers
are tested against them, and `resolve` is tested against both (Arc on `renderD128` versus `renderD129`; Wave XLR at
48000 Hz versus Dummy Output). A fake sysfs and `/proc/asound` builder covers hot-plug and the generic fallback. CI
runs these with no hardware; the on-device checks stay in the release verification (`verify_install`).

## Alternatives rejected

- **Per-host `if hostname` branches or per-host env files.** Breaks when a device moves, hides the real reason, and
  cannot fall back.
- **Probing only, no profiles.** Probing says what exists, not what a good plan is: the Wave XLR's 48000 Hz s24le and
  its USB control driver, or "the Arc does grade and encode, the iGPU does neither", are knowledge, and a profile is
  where that lives and gets reviewed.
- **Profiles only, no probing.** A profile cannot know that a node is `denied`, that a card is `off`, or that the
  HDMI source limits the Cam Link to 1080p29.97 today.
- **Disable the iGPU in firmware.** Changes the host, not the app, and removes the iGPU's decode capacity.

## Consequences

- One resolved Plan replaces scattered `STREAMSTACK_*_DEVICE` guesses; the existing keys remain as overrides.
- `profiles.py`'s catalogue moves into data without changing the interface drivers.
- `encoder.py` is gone after !48, so the encoder section is built against the native engine and the stream service
  (#97, #98, WHIP #113), not OBS.
- Adding a third host or swapping a DAC is a data change plus a fixture.

## Open decisions (owner)

1. **Which audio interface deskmeet1 uses.** Decided 2026-10-24: a Focusrite Scarlett 2i2 (gen 4 `1235:8219` or gen 3
   `1235:8210`), both profiles exist. It is not enumerating on USB yet; until it does, the doctor audio row is a named
   warning with the fix (re-seat it in a direct rear USB port with a short known-good cable, an owner action).
2. **Call evidence.** A signed-in Teams or Zoom account and a live HDMI source on deskmeet1.

## Related

#90 (generate the filter chain from config), #95 (zero-copy), #97 (platform profiles), #106 (Vulkan Video on the Arc),
!48 (OBS removal), ADR 0001, the native video engine ADR.
