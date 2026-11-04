# ADR 0009: Wave XLR hardware controls go over vendor USB control transfers, not HID; Clipguard and low cut stay in software

Status: accepted (2026-11-01), hardware read-back still to be verified by a person. Issue #18, milestone
"Hardware-native". Supersedes the findings scattered across `docs/interfaces.md` and the `hardware/wavexlr.py`
docstring as the single decision record.

## Context

Issue #18 asked for the Wave XLR's own gain, Clipguard, low cut and mute state, assuming a USB HID interface. Only the
PipeWire source volume was controlled. The spike was to be read-only.

## Measurements (dm313red, 2026-11-01, sysfs and `lsusb -v` only, nothing written, no device opened)

Elgato Wave XLR `0fd9:007d`, bcdDevice 1.31, USB 2.0 high speed, one configuration, bMaxPower 100 mA, on hub port
`1-2.4.1`:

| Interface | Class / sub / proto | Endpoints | Driver |
| --- | --- | --- | --- |
| 0 | audio / control (01/01/00) | 1 (EP3 IN, interrupt) | snd-usb-audio |
| 1 | audio / streaming (01/02/00), alt 0 and 1 | EP1 OUT, EP1 IN | snd-usb-audio |
| 2 | audio / streaming (01/02/00), alt 0 and 1 | EP2 IN | snd-usb-audio |
| 3 | vendor `ff/f0/00`, string "Elgato Wave XLR Controls" | none | none bound |
| 4 | application specific `fe/01/01` (DFU), string "Elgato Wave XLR DFU" | none | none bound |

- No interface has class 3 (HID). There is no `report_descriptor` below the device in sysfs and no `hidraw` node
  belongs to it. The nine hidraw nodes on this host belong to a Logitech receiver, a keyboard, and the Cam Link 4K.
- The only HID interface from this vendor on the desk is the Cam Link 4K's (`0fd9:0067`, interface 2, `hidraw1`,
  vendor usage page 0xFF00, report IDs 1 to 7 and more). It is the Cam Link's own control channel and is not the
  Wave XLR's. It is noted because it is the only place an Elgato "HID" claim could have come from on this desk.
- Interface 3 has no endpoints, so it cannot carry interrupt or bulk traffic. The only channel it offers is control
  transfers on endpoint 0, which matches the published protocol.

## Prior art (re-checked 2026-11-01)

- `rikkichy/openwave` (MIT): Wave XLR supported with gain, mute, 48 V, headphone volume, low impedance; USB class
  control transfers on endpoint 0, `wIndex` 0x3303, 34 byte block, `bRequest` 0x85 read / 0x05 write; found by tracing
  the macOS Wave Link app with Frida. No HID. Low cut there is a software DSP stage; Clipguard is not mentioned.
- `emaspa/openxlr` (GPL-3.0, read only, nothing copied): gain, mute, headphone volume and low impedance verified by a
  community tester on the MK.1 (256 raw units per dB); 48 V on config byte 6 confirmed; **hardware low cut, ClipGuard
  and the mic/PC crossfade unmapped** ("Wave Link exposes these controls; their offsets are unknown"); its software
  low cut and limiter are used instead. No HID.
- `philipredstone/elgato-wave-linux`, `RamaAditya49/cadiswave`: same transfer, same layout.

Nobody has published a register for Clipguard or hardware low cut, and no project uses HID for this device.

## Decision

1. **Control path: vendor control transfers, as implemented in `src/streamstack/hardware/wavexlr.py`.** HID is
   dropped from the scope of #18. Interface 3 is claimed only for the length of one transfer; the audio driver is never
   detached.
2. **Read-back first, writes behind a setting.** `STREAMSTACK_WAVE_XLR_USB=off|read|write`, default `off` (the device
   is never opened). Read-back reads the vendor block, not ALSA, because the kernel caches `Mic Capture Volume` and
   never reads it back. 48 V additionally needs an explicit confirmation and is refused while a call may be live.
3. **Clipguard and hardware low cut are not offered.** The equivalents are the voice chain's high-pass stage and
   limiter (`docs/audio-chain.md`), which behave the same for the listener and are verifiable. If a register is ever
   documented (watch openwave and openxlr), add it as a new field of the 34 byte block, behind the same setting and
   read-modify-write, after a person has diffed it against Wave Link.
4. **No probing of unknown bytes on a production unit.** Finding Clipguard and low cut would mean writing unknown
   offsets and watching Wave Link, which can brick a setting, so it is out of scope here and needs a spare unit.

## Consequences

- #18's research and software parts are complete (spike, codec, read-back, gated writes, doctor and page rows,
  software equivalents). What is left is only the first contact with the real device and comparing the page with Wave
  Link; that needs a quiet moment, a service restart with the environment variable set and a person at the desk.
- Closing criteria for #18: with `STREAMSTACK_WAVE_XLR_USB=read` and the udev rule, the Wave XLR page shows gain, mute,
  48 V, headphone volume and low impedance equal to Wave Link's (or to what the front knob and LEDs show); then one
  `write` round trip of the gain with a read-back inside 0.5 dB.
