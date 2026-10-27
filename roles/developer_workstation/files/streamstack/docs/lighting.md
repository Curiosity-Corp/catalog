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
same moment the director's write fails with a message and is retried on the next pass. Two processes can hold the
same hidraw node open at once (verified on a real Litra Beam, see "Verified on a real light"), so in practice the two
rarely collide.

## Lighting automation: camera follow and the daylight schedule

The Lighting page also automates the lights. Two features, one engine (`streamstack/lightauto.py`):

- **Camera follow.** The lights switch on when something uses the camera and off a while after nothing does.
- **Daylight schedule.** Brightness and colour temperature follow the time of day whenever the lights are on, with
  smooth fades.

Everything is a setting on the Lighting page (cards "Camera follow", "Daylight schedule" and "Light settings"), stored
in `~/.config/streamstack/lighting-automation.json` (mode 600, written atomically). The status banner at the top says
why the lights are as they are, for example "Camera in use since 14:02: Teams opened the camera", and the log under
the cards lists what happened ("Litra Beam 1 switched on at 62%, 4400 K because Teams opened the camera").

### It works with the Console closed

The engine runs in its own small user service, **`streamstack-lights.service`** (`streamstack lights run`). The AI
Director only runs inside `streamstack-auto`, which is optional, and the Console is a window you close, so neither can
host something that must watch the camera all day.

- `streamstack setup` installs the unit (a packaged copy from `make install` is used when there is one, otherwise it
  writes `~/.config/systemd/user/streamstack-lights.service` with the absolute path of this `streamstack`) and starts
  it. `streamstack setup --no-service` skips that.
- `streamstack lights enable | disable | status | run` (also the Start button on the Lighting page when the service
  is down).
- The Console reads the service's status file (`~/.local/state/streamstack/lights-status.json`, refreshed every few
  seconds; older than 10 s means "not running") and edits the same settings file, which the service re-reads every
  second. Nothing in the Console has to be open.
- The service uses the same `LightingController` as the Console (hidraw discovery, hotplug, the protocol), so a Litra
  that is plugged in later is picked up by itself. With no light attached it idles.

### What counts as "the camera is on"

Detection is read-only. It never opens a `/dev/video*` node, so it cannot steal the camera or change a format.

1. **Apps.** Each second the service lists `/proc/<pid>/fd` and looks for a process that holds the program camera
   (`STREAMSTACK_PROGRAM_DEVICE`, the native engine's virtual camera: a call app opening it is the call starting), the raw camera (`STREAMSTACK_CAMERA_DEVICE`, unless
   `STREAMSTACK_CAMERA_EXTERNAL` says another tool feeds it) or an extra device you list. Teams, Meet or Zoom in a
   browser, Zoom, Cheese or any other consumer all show up this way. The switch for this source is "Apps".

StreamStack's own handles are not "use": any `streamstack` process (the Console holds the program node open), and ffmpeg/GStreamer helpers started
by one of those (the Canon bridge, preview grabs). An ffmpeg you start from a terminal does count. There is an
ignore list (process or app name) for anything else, such as a screen-share tool.

A call starts a session after `start_delay_seconds` (default 3, so a flicker does nothing). When the camera is
released, the session ends after `grace_seconds` (default 120).

### The rules

- **Only what we switched on.** At the end of a session the lights StreamStack switched on are faded out and
  switched off. A light that was already on stays on. A light you switched off during the call stays off. A light you
  changed by hand during the call stays on, as you left it.
- **A person always wins.** The engine compares what the light reports with what it last wrote itself. A difference
  means a person (the Console, the light's own buttons, a preset): that light is held, and the schedule leaves it
  alone for `hold_minutes` (default 90; 0 means until the next camera session), until the next camera session, or
  until you switch it off and on again. **Resume the schedule** (button, `R`) hands the lights back at once.
  Anything outside StreamStack counts the same way, because the engine only reads the light back: `litra toggle` from
  a shell, a mouse button bound to it (the G604 thumb button in the catalog), another app. Switching a light off that
  way during a call keeps it off, and a light you switched on that way is not switched off by the grace period.
- **Per light.** A name, a role and limits. Role `key` follows the schedule in full, `fill` at `fill_percent` of it
  (default 60%), `manual` is never touched by camera follow or the schedule. The limits keep a light inside a
  brightness and colour range whatever the schedule asks for.
- **Fades.** A big change (more than 15% of the lumen range or 600 K) is faded over `fade_seconds` (default 3) in
  small steps. Smaller changes, such as the sun moving, are plain writes. Switching off fades to 1%, switches off, and
  then puts the brightness back, so the light wakes at its old level next time.

### Who owns the light: the schedule and the AI Director

| | power | brightness and colour temperature |
| --- | --- | --- |
| camera follow | switches on, and off after a call | only sets the first level, and only when the schedule owns the light |
| daylight schedule | never | owns them, unless the Director does |
| AI Director (`streamstack auto`) | never | owns them while it drives the lights |
| a person | always wins | always wins |

The Director "drives the lights" when `streamstack-auto.service` is active and `STREAMSTACK_AUTO_LIGHTS` is on. The
schedule then yields completely (the banner says "The AI Director owns brightness and colour"), so the two never
fight. Camera follow still switches power, because the Director never does. When the Director stops, the schedule
takes the lights back within 10 seconds.

### The schedule

Two modes, one 24 hour graph (brightness line, colour-temperature strip, sunrise, sunset and now):

- **Follow the sun** (default). Five slots, night, dawn, morning, afternoon and evening, from the same offline
  sunrise and sunset code the Director uses (`streamstack/director/daylight.py`, the property location set on the Auto
  page, else the computer's time zone). The slot levels start from the Director's light priors, so the room gets more
  light when the day gives less, and a slot change is blended over an hour rather than switched.
- **24 hour table.** One level per hour of the clock, seeded from today's sun curve the first time you switch to it,
  interpolated between hour centres. Click a point on the graph or pick an hour to edit it.

### Keys

`F` camera follow on/off, `S` schedule on/off, `R` resume the schedule (all on the Lighting page, also in the command
palette). `L` (all lights) is global as before.

### Demo rig

The demo rig runs the engine inside the Console over the simulated lights and a scripted camera: two buttons on the
page pretend Teams opens and releases the camera, and a switch pretends the Director is running. The demo starts with
the schedule off so the demo lights stay where the specs put them.

### Verified on a real light, and what is assumed

Verified on a Litra Beam (`046d:c901`, `/dev/hidraw7`, the udev rule's ACL for the logged-in user) with read-only
GET reports plus one short write test that restored the light's exact state:

- Discovery finds the control interface (vendor usage page `0xff43`).
- **Two processes can open the same hidraw node at once**, and both are answered (power, brightness, temperature).
- Writes of brightness and temperature are read back exactly (`on / 4000 K / 100 lm`, `200 lm`).
- **Brightness and temperature writes are accepted while the light is off and do not turn it on.** That is what lets
  the engine fade out, switch off and restore the level.

Assumed, not yet exercised on hardware: the Glow and the Beam LX (same protocol, different feature byte, covered by
unit tests); that a browser tab using the camera is seen as
the browser (Chrome, Firefox) and not as "Meet" or "Teams", because the kernel only knows the process; and the
systemd unit on a machine other than the development one.

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
