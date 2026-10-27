# Stream Deck

StreamStack drives an Elgato Stream Deck natively: every option and preset in the Console can sit on a key, the keys
show live state (mute lit red, the program scene, the lights), and the Console's **Deck** page shows the same keys
live, so you can build the layout without touching the device. It works with every model the
[`streamdeck`](https://pypi.org/project/streamdeck/) library supports (Mini, Original/MK.2, Neo, XL, Plus). The default
profile fills a 4x4 deck, and any other key layout is re-flowed to fit.

Linux has no first-party Elgato app. If `streamdeck-linux-gui` is installed it knows nothing about StreamStack and
holds the device, so stop it first (see below).

## Setup

1. Install the optional extra (it needs Pillow and the `streamdeck` library):

   ```
   pip install 'streamstack[deck]'
   ```

   The library needs the system `libhidapi-libusb0` (Debian and Ubuntu: `sudo apt install libhidapi-libusb0`).
   The core StreamStack and the Console work without the extra; the Deck page then works as an editor with no device,
   and draws its keys in the browser.

2. Let your user open the device. Hosts that ran `streamdeck-linux-gui` already have
   `/etc/udev/rules.d/60-streamdeck.rules` (with `TAG+="uaccess"`). If not, install the rules from the
   `python-elgato-streamdeck` project, then `sudo udevadm control --reload && sudo udevadm trigger` and re-plug.

3. Stop anything else that holds the deck. Only one program can open it:

   ```
   systemctl --user disable --now streamdeck-linux-gui 2>/dev/null; pkill -f streamdeck
   ```

   and remove it from your session autostart (`~/.config/autostart/streamdeck_ui.desktop`).

4. Start the service:

   ```
   streamstack deck enable      # installs and starts the systemd user unit streamstack-deck.service
   streamstack deck status      # service, connected device, current page (exit 0 when a deck is connected)
   ```

   `make install` also ships the unit (`packaging/systemd/user/streamstack-deck.service`); `streamstack setup` enables
   it when the extra is installed. `streamstack deck run` runs the service in the foreground.

## How it works

The Console is the brain and the service is a terminal:

- The Console (`console/deck.py`) holds the profile, the open page, and what every key looks like for the rig's
  current state (`state.deck`). Presses arrive as `deck.press` and run the same actions the page's buttons run.
- `streamstack deck run` (`deck/service.py`) opens the USB device, draws the keys the Console describes with Pillow,
  sends presses back, applies brightness, sleeps the deck after the idle time (the press that wakes it does nothing
  else), reconnects when the deck is unplugged and plugged in again, and starts a headless Console
  (`streamstack console --no-window`) when none is running, unless started with `--no-autostart`. It tells the Console
  which deck it holds every 5 seconds (`deck.attach`).
- Because both the page and the device read the same state, they always agree. The page's **Play** mode presses keys
  with the mouse.

## The profile

The profile is pages (folders) of keys, saved in `~/.config/streamstack/deck.json` (mode 600). Until you edit
something the default profile is shown and follows the rig: one key for every scene, voice preset and lighting preset
that exists. The first edit saves it. Each key is `{action, params, label?, icon?}`:

- `action` is a Console action name such as `video.scene`; `params` are its parameters. Keys light, change label or
  ask for a second press (stopping a recording or stream) according to rules in `deck/catalog.py`.
- `deck.page` (open a page), `deck.back`, `deck.home` are keys too. A page that does not fit a smaller deck gets Back
  and More keys, so nothing is lost on a Mini.

On the Deck page: **Edit** to drag options from the list onto keys (or select a key and click an option), drag keys to
swap them or onto the trash, shape a key in the inspector (label, icon, action and JSON parameters), add, rename,
reorder and delete pages, set brightness and sleep, export and import the profile as JSON, and reset to the default
(hold the button). When no deck is connected a model picker sets the layout being edited.

## Any Console action

The options list is built from `deck/catalog.py`, not hard-coded in the page, and it reads the scenes and presets from
the live snapshot. To make a new Console action a first-class option (a label, an icon, when it lights), add a rule to
`RULES` there. Until then, any action works through the **Any Console action** entry: put it on a key and type the
action name and parameters in the inspector (for example `lighting.rescan`, or `video.scene` with `{"name": "Canon"}`).

## The demo rig

`streamstack console --demo` has a simulated 4x4 deck (no hardware, no Pillow needed). The e2e tests press its keys and
watch state change; `deck.attach {"model": "xl"}` swaps the simulated model and `deck.detach` unplugs it.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Banner: "the deck extra is not installed" | `pip install 'streamstack[deck]'` for the Python that runs `streamstack`. |
| Banner: cannot open the device / permission denied | the udev rule above, then re-plug the deck. |
| Banner: another application has the deck | stop `streamdeck-linux-gui` (step 3). |
| "No Stream Deck is connected" with a deck plugged in | `streamstack deck status`; `journalctl --user -u streamstack-deck`. |
| Keys are blank | the Console is not running; the service shows "Console offline" and starts one unless `--no-autostart`. |
