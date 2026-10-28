# Audio interfaces

StreamStack finds the audio interface on the desk by USB id, picks a profile and shows that interface's own
controls on the Console's Interface page. Two interfaces are first-class targets, auto-detected and given every
control they can honestly offer:

| Profile | USB id | Controls | How |
| --- | --- | --- | --- |
| `scarlett-2i2-gen4` | `1235:8219` | preamp gain, Autogain (+ targets), Air (Off / Presence / Presence + Drive), Line / Inst, Safe, link, input select, 48V, direct monitor | kernel `scarlett2` ALSA controls through `amixer` |
| `scarlett-2i2-gen3` | `1235:8210` | Air, Line / Inst, 48V (+ persistence), direct monitor | the same, only the controls the card lists |
| `wave-xlr` | `0fd9:007d` | gain, mute, 48V, headphone volume, low-impedance headphones | vendor USB control transfers, **off by default** |

The Audio page's PipeWire software gain stays the fallback for every interface (and the only gain for an interface
without a profile). Settings: `STREAMSTACK_INTERFACE` (`auto`, `off`, a profile id or a USB id) and
`STREAMSTACK_WAVE_XLR_USB` (`off`, `read`, `write`).

## How a profile works

`streamstack/hardware/`: `usb.py` lists USB devices and their sound cards from sysfs and `/proc/asound`
(read-only), `profiles.py` maps USB id to profile and selects one, `alsa.py` parses `amixer` output, `scarlett.py`
and `wavexlr.py` are the drivers, `widgets.py` is the vocabulary a driver reports (switch, choice, slider, action,
readout), `sim.py` is the simulated hardware behind `--demo` and the tests. `console/interface.py` polls the
active driver in a thread and caches the state, so the Console never waits on hardware. A new interface is a new
`Profile` and a driver that returns widgets; the page needs no change.

Every write is validated, sent, then read back; a value the device did not keep is reported, not assumed.
48V is switched on only by a press-and-hold in the page *and* `confirm: true` on the request.

## Scarlett: the exact kernel control names

All names come from `sound/usb/mixer_scarlett2.c` in the Linux kernel (master, read on 2026-10-22; line numbers
are from that file and move between releases). They are built there as `"Line In %d %s Capture %s"` and siblings
by `scarlett2_add_line_in_ctls` (L6821-L7010), so they are the same on every Scarlett with the feature. The
driver offers exactly the controls the card lists: it never guesses from the model.

| Control name (amixer) | Meaning | Kernel source |
| --- | --- | --- |
| `Line In N Level Capture Enum` | Line / Inst | L6832, items L5036 |
| `Line In N Air Capture Switch` | Air on/off (Gen 3) | L6851 |
| `Line In N Air Capture Enum` | Off / Presence / Presence + Drive (Gen 4) | items L5279 |
| `Line In 1-2 Phantom Power Capture Switch` | 48V, one switch for both 2i2 inputs | L6887 |
| `Phantom Power Persistence Capture Switch` | keep 48V across power cycles (Gen 3) | L6911 |
| `Line In N Gain Capture Volume` | preamp gain, 0..70, TLV 0..69 dB (Gen 4) | max L190, TLV L641, L6944 |
| `Line In N Autogain Capture Switch` | writing 1 starts Autogain | put L3911, L6956 |
| `Line In N Autogain Status Capture Enum` | Running, Success, SuccessDRover, WarnMinGainLimit, FailDRunder, FailMaxGainLimit, FailClipped, Cancelled, Root, Invalid | texts L396 |
| `Autogain Mean Target`, `Autogain Peak Target` (`Hot` on other models) | -30..0 dB | L6974, range L353 |
| `Line In N Safe Capture Switch` | Safe mode (clip protection) | L6988 |
| `Line In N Link Capture Switch` | link the two inputs | L6930 |
| `Input Select Capture Enum` | Input 1 / Input 2 | L6922 |
| `Direct Monitor Playback Enum` | Off / Mono / Stereo | L7313 |
| `Firmware Version`, `Minimum Firmware Version`, `Sync Status` | read-outs | L3524, L3564, L3585 |

Facts worth knowing:

- Gen 3 has **no** software gain or Autogain: its gain knobs are analogue. The page shows what the card lists.
- The Gen 4 2i2 has a single gain control (the preamp, 0 to 69 dB). There is no separate "digital gain" control in
  the driver, so the page shows none.
- The driver has no sample-rate or clock-source control. The rate shown is read from `/proc/asound`.
- Autogain locks gain, Air, Level, Safe and link while it runs; the page shows them locked with the reason.
- `device_setup` bit `0x04` disables the scarlett2 driver and `0x08` selects the FCP driver; with either, the
  controls do not exist and the page says no interface was found. A firmware older than the driver's minimum also
  hides them (see `Minimum Firmware Version`).

## Wave XLR: research findings (issue #18)

**There is no HID interface.** On this host the XLR (`0fd9:007d`, bcdDevice 0131) exposes interface 0 audio control
and 1/2 audio streaming (all `snd-usb-audio`), interface 3 a vendor class `ff/f0` with no endpoints and **no
driver bound**, and interface 4 a DFU class `fe/01/01`. There is no `/dev/hidraw` node for it. The control
protocol is vendor USB class requests on the control endpoint.

Prior art (open source, MIT): `rikkichy/openwave` (`crates/openwave-core/src/profiles.rs`, `protocol.rs`,
`docs/protocol.md`), `philipredstone/elgato-wave-linux`, `RamaAditya49/cadiswave`, `emaspa/openxlr`,
`Michael-Brooks/openxlr`. They agree on:

- control transfer `bmRequestType` `0xA1` read / `0x21` write, `bRequest` `0x85` read / `0x05` write, `wIndex`
  `0x3303` (not `0x3300`: interface 0 belongs to `snd-usb-audio`, so the kernel's unclaimed interface 3 is used);
- `wValue` `0x0000` is the 34-byte config, `0x0001` a 10-byte meter block, `0x000A` the 51-byte device info;
- config (little endian): gain u16 at byte 0 (raw / 256 dB), mute byte 4, 48V byte 6, headphone volume i16 at byte
  9 (raw / 256 dB), knob mode byte 14 (2 = headphones), low-impedance byte 33; device info has the API version at
  bytes 0-1, firmware at 6-8 and the serial at 27-46;
- a write is the whole config block, so the driver reads, changes the named fields, writes and reads back; unknown
  bytes are written back unchanged;
- only one process can own the transfers at a time (Wave Link, OpenWave, StreamStack): a second one gets `EIO`;
- the firmware forgets its gain at power loss (philipredstone).

**Clipguard and low cut have no documented register.** OpenWave's low cut is a software DSP stage. The page lists
both under "Not available on this interface"; use the high-pass stage of the StreamStack voice chain (docs/audio-chain.md) as the low cut.

**Update 2026-10-27 (#18).** `emaspa/openxlr` (GPL-3.0, read and re-derived, not copied) lists gain, mute,
headphone volume and low impedance as verified by testers on the MK.1, and 48 V (config byte 6) as coded; hardware low
cut, ClipGuard and the mic/PC crossfade are still unmapped there, so they stay unavailable here (the software
equivalents are the voice chain's high-pass and limiter). Two consequences are implemented:

- **48 V is never switched while a call may be live.** The driver is handed the call monitor; a call (joining or in
  call), or a monitor that fails, refuses the write (409). Switching on also still needs the explicit confirmation.
- **The vendor block is the truth, ALSA is not.** The kernel caches `Mic Capture Volume` and never reads it back (74 dB
  cached against 48 dB in the device block on another unit). The page reads the device block and, when the mixer states
  a dB scale and its cached gain differs by more than 6 dB, shows a "Kernel mixer gain" warning that points to
  `streamstack mic-revive`.

Still to do by a person with a quiet moment: set `STREAMSTACK_WAVE_XLR_USB=read` on dm313red with the udev rule above,
compare the page with Wave Link, then `=write`.

**Status: unverified on hardware.** The protocol is reverse engineered by others and the projects themselves call it
"not a physical-validation certificate". The maximum gain is 75 dB here (OpenWave allows 80); nothing on this host
confirmed either. Because of that the write path is disabled by default:

| `STREAMSTACK_WAVE_XLR_USB` | Effect |
| --- | --- |
| `off` (default) | the device is never opened; the page explains how to enable it |
| `read` | the state is read back and shown, every control locked |
| `write` | controls enabled, with read-back after every change |

Access to `/dev/bus/usb/BBB/DDD` needs a udev rule (no sudo, no group change):

```text
# /etc/udev/rules.d/70-streamstack-wave-xlr.rules
SUBSYSTEM=="usb", ATTR{idVendor}=="0fd9", ATTR{idProduct}=="007d", TAG+="uaccess"
```

Quit Elgato Wave Link and OpenWave first, as only one program may talk to the vendor interface.

## The AI Director

Auto mode (`docs/auto-mode.md`) uses these drivers to level the voice with the preamp gain, before the software gain.
It reaches only the gain sliders (`gain`, `gain.N`): a request for any other control id (48V, Autogain, Air, Safe,
Pad, Link, Line/Inst) is refused before it reaches the driver. The Wave XLR is only used when
`STREAMSTACK_WAVE_XLR_USB` is enabled; while it is `off` the device is never opened and the software gain is used.

## What needs a real-hardware check

Nothing here was run against a real Scarlett or a real XLR by the author: the `amixer` fixtures under
`tests/fixtures` are synthesized from the kernel source, and the XLR blocks follow the published layout. Read-only
checks that settle it (none of them writes to the device):

```bash
# Scarlett: does the card list the controls named above? (N = card number from `cat /proc/asound/cards`)
cat /proc/asound/cards
cat /proc/asound/cardN/usbid                   # 1235:8219 (Gen 4) or 1235:8210 (Gen 3)
amixer -c N contents | grep -E "Line In|Autogain|Phantom|Direct Monitor|Firmware|Sync"
amixer -c N contents > scarlett.txt            # compare with tests/fixtures/amixer_scarlett_2i2_*.txt

# Wave XLR: the interfaces, and that nothing else claims interface 3
lsusb -d 0fd9:007d -v 2>/dev/null | grep -E "bInterfaceNumber|bInterfaceClass"
ls /sys/bus/usb/devices/*/1-*:1.3/driver 2>/dev/null   # absent = unclaimed
cat /proc/asound/cardN/stream0 | grep -E "Momentary freq|Format|Channels"
```

Reading the XLR itself (`STREAMSTACK_WAVE_XLR_USB=read`) is a control transfer on the vendor interface, not an
audio capture, but it is the first contact with the real device: do it when nobody is on a call.
