# Global hotkeys and MIDI

Hotkeys and MIDI controls run the same actions as a Stream Deck key (`src/streamstack/deck/catalog.py`): mute, privacy,
looks, scenes, a fader that sets the mic gain, or any Console action. They work while the call window (Teams, Zoom,
Meet) has focus, which is the point: a browser shortcut would not.

Set them up on the **Deck** page, card **Hotkeys and MIDI**: click the Keys box and press the shortcut, choose what it
does, press Add. For MIDI, pick the trigger type, press Learn and move the control, then choose what it does.

## Bindings

A binding is one trigger and one action (`src/streamstack/hotkeys.py`):

```json
{"id": "b1", "trigger": {"kind": "key", "combo": "ctrl+alt+m"}, "action": "audio.mute", "params": {}}
{"id": "b2", "trigger": {"kind": "midi", "type": "cc", "channel": 1, "number": 7},
 "action": "audio.gain", "params": {}, "scale": {"param": "db", "min": -12, "max": 12}}
```

- Keys: modifiers (`ctrl`, `alt`, `shift`, `super`) then a key. A plain letter or digit alone is refused, because it
  would steal typing from every window. Function keys, media keys and the navigation block may stand alone.
- MIDI: `note` (note on), `cc` (control change) or `program`, on channel 1 to 16 or any (0). A `cc` button fires once
  when it crosses 64 upward. With `scale`, a `cc` fires on every change and sets the action's parameter to the value
  scaled into `min..max` (a fader).
- Actions that flip a state (Mute, Privacy) toggle, exactly as a deck key does. Actions that need confirmation (stop
  the stream) arm on the first press and run on a second press within a few seconds.
- A held key repeats: one press runs once (0.3 s debounce).

Bindings are saved in `~/.config/streamstack/hotkeys.json` (mode 600, written atomically; a damaged file is set aside
and reported, see `streamstack.store`). Export and Import on the card move them between machines
(`GET /api/hotkeys/bindings.json`, action `hotkeys.import`).

## How keys are caught

| Session | Backend | Notes |
| --- | --- | --- |
| X11 | `hotkeys_x11.py` (`XGrabKey` through libX11, no extra install) | grabs the combo on the root window, with and without NumLock and CapsLock. A combo another program already holds is marked "In use elsewhere" and left alone. A keyboard layout change re-grabs. |
| Wayland | `hotkeys_portal.py` (XDG Desktop Portal `GlobalShortcuts` over D-Bus) | needs `pip install 'streamstack[shortcuts]'` and a desktop with the portal (KDE Plasma 6, GNOME 48 or later). The desktop asks once to confirm the shortcuts and lets the person move one to another key; the binding keeps working. |
| none | | the card says why (no display, no portal, extra missing). MIDI still works. |

## How MIDI is read

`hotkeys_midi.py` reads the raw ALSA MIDI nodes (`/dev/snd/midiC*D*`) with no library, rescans every second, and so a
controller plugged in during a call works at once. A port another program holds is shown as busy and retried. A machine
without sound devices reports that and leaves MIDI off.

## Stream Deck

Hotkeys and the deck share one action registry, so anything a key can do a hotkey can do, and the Options list in the
card is the deck's. A hotkey press and a deck press go through the same `resolve_press`, so they stay identical.

## Testing without hardware

`streamstack console --demo` runs a fake keyboard and a fake pad. The e2e suite (`tests/e2e/test_page_deck_hotkeys.py`)
drives them with `hotkeys.demo.key`, `hotkeys.demo.midi` and `hotkeys.demo.conflict`. The X11 backend is tested against
a real Xvfb, the portal backend against a fake portal on a private D-Bus, and MIDI against FIFOs.
