# Lighting: Logitech Litra lights

The Lighting page drives Logitech Litra Glow, Beam and Beam LX lights over
USB. It talks to the light directly through the kernel's `hidraw` interface,
in pure Python, with no extra package and no vendor software.

## Supported lights

| Light | USB id | Brightness range | Feature byte |
| --- | --- | --- | --- |
| Litra Glow | `046d:c900` | 20-250 lm | `0x04` |
| Litra Beam | `046d:c901` | 30-400 lm | `0x04` |
| Litra Beam (second hardware id) | `046d:b901` | 30-400 lm | `0x04` |
| Litra Beam LX | `046d:c903` | 30-400 lm | `0x06` |

Colour temperature is 2700-6500 K in 100 K steps on all of them. The page shows
brightness as a percentage (1-100) of the model's lumen range.

Not supported: the Beam LX back light (RGB zones). The Console never sends the
back-light commands.

## One-time host setup: the udev rule

`/dev/hidraw*` nodes are root-only by default, so the first time the page finds
a light it says "permission denied" and names the rule file. The repository
ships it as `packaging/udev/60-streamstack-litra.rules` (`make install` puts it
in `lib/udev/rules.d`; the workstation role copies it to `/etc/udev/rules.d`).
The only step that needs root is installing it:

```
sudo install -m 644 packaging/udev/60-streamstack-litra.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=hidraw
```

The file has one line per product, each of this form (the compact equivalent
for all four is `SUBSYSTEM=="hidraw", ATTRS{idVendor}=="046d",
ATTRS{idProduct}=="c900|c901|b901|c903", TAG+="uaccess"`):

```
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="046d", ATTRS{idProduct}=="c901", TAG+="uaccess", GROUP="plugdev", MODE="0660"
```

`uaccess` gives the logged-in desktop user access to exactly these four
products. The rule is scoped by product id on purpose: other Logitech devices
(a Lightspeed receiver, a mouse) are never touched. If the lights were plugged
in before the rule existed, unplug and replug them. `tests/test_udev.py` pins
the id list.

The constants `UDEV_RULE` and `UDEV_RULE_FILE` in `streamstack/lighting.py` name the rule in
the hint shown in the Console.

## How it works

`streamstack/lighting.py` has three layers:

1. **Discovery** reads `/sys/class/hidraw` only (it never opens a device). A
   Litra exposes several hidraw interfaces; the one whose report descriptor
   declares vendor usage page `0xff43` is the control interface. Lights are
   identified by product id plus the USB serial (or the physical port when the
   light has none), so they survive a replug.
2. **Protocol**: 20-byte output reports `11 ff <feature> <command> <payload>`.

   | Operation | Command | Payload / answer |
   | --- | --- | --- |
   | power set | `0x1c` | `1` on, `0` off |
   | power get | `0x01` | answer byte 4 is `1` when on |
   | brightness set | `0x4c` | big-endian u16 lumen |
   | brightness get | `0x31` | big-endian u16 lumen in bytes 4-5 |
   | temperature set | `0x9c` | big-endian u16 kelvin |
   | temperature get | `0x81` | big-endian u16 kelvin in bytes 4-5 |

3. **Transport**: `HidrawTransport` opens the node non-blocking, writes the
   report and reads the answer with `select`. Errors become a `LightingError`
   whose `kind` is `permission`, `gone` (unplugged) or `io`.

`streamstack/console/lighting.py` is the Console controller. It rescans every
3 s (hotplug: a light appearing or disappearing raises a toast), reads the real
state back every 5 s so a change made with the light's own buttons shows up,
and owns the presets. Saved presets live in
`~/.config/streamstack/lighting.json` (mode 600).

Identify has no command in the protocol, so it pulses the brightness
100% / 1% three times and then restores the previous value.

For the auto director or a script, `lighting.preset` (`{"name": "Soft"}`) is
the one call that sets every light, see [console.md](console.md).

## The AI Director

Auto mode (`docs/auto-mode.md`) drives the lights through the same HID protocol, opening a light only for the moment of
a read or a write: brightness within the model's lumen range and colour temperature, never power, so a light you
turned off stays off. It follows the persona's light for the time of day. If the Console has the light open at the
same moment the director's write fails with a message and is retried on the next pass; the concurrent-open behaviour
is unverified on real hardware.

## Sources

Protocol bytes and ids were cross-checked against two independent open
implementations:

- timrogers/litra-rs 3.3.0 (`src/lib.rs`): product ids, usage page `0xff43`,
  feature bytes, commands, lumen ranges.
- kharyam/litra-driver (PyPI `litra-driver`): the same bytes for Glow `c900`
  and Beam `c901`, and a udev example keyed on `idProduct`.

The npm `litra` package documents 20-400 lm for the Beam and Beam LX; litra-rs
documents 30-400. StreamStack follows litra-rs, so the very bottom of the range
on a real Beam may be a little brighter than the light's own minimum.

## Checking a real light

The Lighting code has only been exercised against simulated lights and
fixtures. With a real light plugged in and the rule installed:

```
python3 -c "from streamstack import lighting; print(lighting.discover())"   # reads /sys only
streamstack console          # open the Lighting tab; the System page lists "Litra lights"
```

Then move each fader and press each pad, and confirm the light follows. Press
the light's own brightness button and check the fader follows within 5 s.
