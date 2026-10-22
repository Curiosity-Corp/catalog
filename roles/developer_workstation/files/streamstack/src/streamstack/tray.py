"""StreamStack panel menu: open the Console, start/stop the studio, pick voice presets, preview.

A desktop front end for the `streamstack` command, so a call can be prepared
without a terminal. The Console (`streamstack console`) is the main surface;
this menu stays for panels and quick one-offs. Two ways to put it on the panel:

  streamstack-tray --menu   pop the menu up at the pointer and exit when done;
                            bind it to a panel button, launcher or hotkey
                            (with `streamstack status --icon` as the button)
  streamstack-tray          a system tray icon (Ayatana AppIndicator, falling
                            back to an XEmbed status icon); a left click opens
                            the Console, the menu is on the right click

The icon shows what Teams and Zoom receive:
  red dot    the graded StreamStack Camera (OBS virtual camera) is live
  green dot  only the raw camera is live (studio stopped: lighter on laptops)
  amber      something needs attention (no voice chain, OBS without picture)
  grey       no camera picture
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):
    AppIndicator = None

from streamstack import config, system  # noqa: E402
from streamstack.cli import auto_running  # noqa: E402
from streamstack.system import detach  # noqa: E402


def _cli_path() -> str:
    """The streamstack command next to this launcher, else the one on PATH."""
    sibling = Path(sys.argv[0]).resolve().with_name("streamstack")
    return (
        os.environ.get("STREAMSTACK_CLI")
        or shutil.which("streamstack")
        or (str(sibling) if sibling.exists() else "streamstack")
    )


CLI = _cli_path()
ICONS = config.data_dir() / "icons"
POLL_SECONDS = 3
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def last_claude_note() -> str:
    """The notes of Claude's most recent review, from the auto-mode journal."""
    try:
        lines = (config.state_dir() / "auto.jsonl").read_text().splitlines()
    except OSError:
        return ""
    for line in reversed(lines[-200:]):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("event") == "ai":
            return str(entry.get("proposal", {}).get("notes", ""))
    return ""


_MODES: dict[tuple[str, str], list] = {}


def devices_with_modes() -> list[tuple[str, str, list]]:
    """Live video devices and their modes; modes are cached per device identity."""
    found = []
    for device, name in system.capture_devices():
        key = (device, name)
        if key not in _MODES:
            _MODES[key] = system.camera_modes(device)
        found.append((device, name, _MODES[key]))
    return found


def probe() -> dict:
    """Runs off the GTK thread: v4l2-ctl and pactl can take a moment."""
    cfg = config.load_settings()
    camera, program = cfg["STREAMSTACK_CAMERA_DEVICE"], cfg["STREAMSTACK_PROGRAM_DEVICE"]
    ee = system.easyeffects_running()
    return {
        "cfg": cfg,
        "obs": system.running("obs"),
        "camera_live": bool(Path(camera).exists() and system.loopback_has_producer(camera)),
        "program_live": bool(Path(program).exists() and system.loopback_has_producer(program)),
        "voice": system.EE_SOURCE in system.audio_sources(),
        "easyeffects": ee,
        "preset": system.easyeffects_preset() if ee else "",
        "presets": system.ee_presets(),
        "devices": devices_with_modes(),
        "auto": auto_running(),
        "note": last_claude_note(),
    }


def notify(summary: str, body: str = "") -> None:
    if shutil.which("notify-send"):
        subprocess.Popen(
            ["notify-send", "-a", "StreamStack", "-i", str(ICONS / "streamstack-live.svg"), summary, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class Tray:
    def __init__(self, popup: bool = False) -> None:
        self.popup = popup
        self.pending = 0
        self.windows = 0
        self.state: dict = {}
        self.updating = False
        self.busy = False
        self.bypassed = False
        self.preset_items: dict[str, Gtk.RadioMenuItem] = {}
        self.menu = self._build_menu()
        if popup:
            self.icon = self.indicator = None
            self.apply(probe())
            self.menu.connect("deactivate", lambda _m: GLib.timeout_add(400, self._maybe_quit))
            # A legacy popup works without a triggering event, which a menu
            # started from a panel click no longer has.
            GLib.idle_add(lambda: self.menu.popup(None, None, None, None, 0, Gtk.get_current_event_time()) and False)
            return
        if AppIndicator:
            self.indicator = AppIndicator.Indicator.new_with_path(
                "streamstack", "streamstack-off", AppIndicator.IndicatorCategory.HARDWARE, str(ICONS)
            )
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator.set_title("StreamStack")
            self.indicator.set_menu(self.menu)
            # Most trays show the menu on a left click and offer no way to change that;
            # a middle click (where the tray supports it) goes straight to the Console.
            self.indicator.set_secondary_activate_target(self.console_item)
            self.icon = None
        else:
            self.icon = Gtk.StatusIcon.new_from_file(str(ICONS / "streamstack-off.svg"))
            self.icon.set_title("StreamStack")
            self.icon.connect("activate", lambda _icon: self.open_console())
            self.icon.connect("popup-menu", self._popup)
        self.refresh()
        GLib.timeout_add_seconds(POLL_SECONDS, self.refresh)

    # -- menu -----------------------------------------------------------------

    def _item(self, menu: Gtk.Menu, label: str, callback=None, *args) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=label)
        if callback:
            item.connect("activate", lambda _w: callback(*args))
        else:
            item.set_sensitive(False)
        menu.append(item)
        return item

    def _submenu(self, menu: Gtk.Menu, label: str) -> Gtk.Menu:
        sub = Gtk.Menu()
        item = Gtk.MenuItem(label=label)
        item.set_submenu(sub)
        menu.append(item)
        return sub

    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        self.console_item = self._item(menu, "Open StreamStack Console", self.open_console)
        self._item(menu, "Open the compact Console", self.open_console, "--mini")
        menu.append(Gtk.SeparatorMenuItem())
        self.camera_status = self._item(menu, "Calls camera: checking...")
        self.voice_status = self._item(menu, "Microphone: checking...")
        menu.append(Gtk.SeparatorMenuItem())
        self.studio_item = self._item(menu, "Start studio (graded camera)", self.toggle_studio)
        self.auto_item = Gtk.CheckMenuItem(label="Auto-cinematographer and auto-audio (Claude)")
        self.auto_item.connect("toggled", self.toggle_auto)
        menu.append(self.auto_item)
        auto = self._submenu(menu, "Auto mode")
        self._item(auto, "Ask Claude about the picture now", self.auto_once)
        self._item(
            auto, "Recent auto changes", self.show_output, "StreamStack auto mode", "auto", "status", "--lines", "30"
        )
        self._item(
            auto, "Check auto mode (OBS, Claude, mic)", self.show_output, "StreamStack auto check", "auto", "check"
        )
        self._item(auto, "Undo auto changes (restore the look and mic gain)", self.auto_reset)
        self.note_item = self._item(auto, "Claude: no review yet")
        menu.append(Gtk.SeparatorMenuItem())

        self.voice_menu = self._submenu(menu, "Voice preset")
        self.bypass_item = Gtk.CheckMenuItem(label="Bypass effects (raw mic)")
        self.bypass_item.connect("toggled", self.toggle_bypass)
        self._item(menu, "Mic check (raw, then processed)", self.mic_check)

        self.preview_menu = self._submenu(menu, "Preview")
        self._rebuild_previews([])

        apps = self._submenu(menu, "Open")
        self._item(apps, "OBS Studio (lighting, colour, scenes)", self.open_obs)
        self._item(apps, "Easy Effects (bass, treble, pitch)", detach, ["easyeffects"])
        self._item(apps, "Audio routing (qpwgraph)", detach, ["qpwgraph"])
        self._item(apps, "Sound levels (pavucontrol)", detach, ["pavucontrol"])
        self._item(apps, "Camera controls (qv4l2)", detach, ["qv4l2"])

        menu.append(Gtk.SeparatorMenuItem())
        self._item(menu, "Check everything", self.show_output, "StreamStack check", "doctor")
        self._item(menu, "Teams and Zoom settings", self.show_output, "Teams and Zoom settings", "call-settings")
        menu.show_all()
        return menu

    def _popup(self, icon, *args) -> None:
        self.menu.popup_at_pointer(None)

    def _rebuild_previews(self, devices: list) -> None:
        """Quick picks, then every live device with each mode it offers."""
        menu = self.preview_menu
        for child in menu.get_children():
            menu.remove(child)
        self._item(menu, "Canon (raw)", self.cli_detached, "preview", "canon")
        self._item(menu, "Canon with thirds grid", self.cli_detached, "preview", "canon", "--grid")
        self._item(menu, "Cam Link 4K", self.cli_detached, "preview", "camlink")
        self._item(menu, "StreamStack Camera (what calls see)", self.cli_detached, "preview", "program")
        menu.append(Gtk.SeparatorMenuItem())
        self._item(menu, "Every camera and mode:")
        for device, name, modes in devices:
            sub = self._submenu(menu, f"{name} ({device})")
            self._item(sub, "Current mode", self.cli_detached, "preview", device)
            self._item(sub, "Current mode with grid", self.cli_detached, "preview", device, "--grid")
            if modes:
                sub.append(Gtk.SeparatorMenuItem())
            for mode in modes:
                label = system.mode_label(mode)
                self._item(sub, label, self.cli_detached, "preview", device, "--mode", label)
        if not devices:
            self._item(menu, "no camera is delivering video")
        menu.show_all()

    def _rebuild_presets(self, presets: list[str]) -> None:
        for child in self.voice_menu.get_children():
            self.voice_menu.remove(child)
        self.preset_items.clear()
        group = None
        for name in presets:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(group, name.removeprefix("StreamStack ") or name)
            group = group or item
            item.connect("toggled", self.pick_preset, name)
            self.voice_menu.append(item)
            self.preset_items[name] = item
        if not presets:
            self._item(self.voice_menu, "no presets yet")
        self.voice_menu.append(Gtk.SeparatorMenuItem())
        if self.bypass_item.get_parent():
            self.bypass_item.get_parent().remove(self.bypass_item)
        self.voice_menu.append(self.bypass_item)
        self.voice_menu.show_all()

    # -- state ----------------------------------------------------------------

    def refresh(self) -> bool:
        def work() -> None:
            try:
                state = probe()
            except Exception as exc:  # keep the icon alive whatever a probe does
                state = {"error": str(exc)}
            GLib.idle_add(self.apply, state)

        threading.Thread(target=work, daemon=True).start()
        return True

    def apply(self, state: dict) -> bool:
        if "error" in state:
            self.camera_status.set_label(f"StreamStack: {state['error']}")
            self._set_icon("streamstack-warn")
            return False
        previous = self.state
        self.state = state
        cfg = state["cfg"]
        studio = state["obs"]
        if studio and state["program_live"]:
            camera, icon = f"Calls camera: {cfg['STREAMSTACK_PROGRAM_LABEL']} (graded)", "streamstack-live"
        elif studio:
            camera, icon = "Calls camera: OBS running, virtual camera off", "streamstack-warn"
        elif state["camera_live"]:
            camera, icon = f"Calls camera: {cfg['STREAMSTACK_CAMERA_LABEL']} (raw)", "streamstack-raw"
        else:
            camera, icon = "Calls camera: no picture", "streamstack-off"
        if state["voice"]:
            voice = f"Microphone: Easy Effects Source - {state['preset'] or 'custom'}"
            if self.bypassed:
                voice += " (bypassed)"
        else:
            voice, icon = "Microphone: voice chain off (raw mic only)", "streamstack-warn"
        self.camera_status.set_label(camera)
        self.voice_status.set_label(voice)
        self.studio_item.set_label("Stop studio (raw camera, lighter)" if studio else "Start studio (graded camera)")
        self.studio_item.set_sensitive(not self.busy)
        if [d[:2] for d in state["devices"]] != [d[:2] for d in previous.get("devices", [])]:
            self._rebuild_previews(state["devices"])
        if state["presets"] != previous.get("presets"):
            self._rebuild_presets(state["presets"])
        self.updating = True
        try:
            if state["preset"] in self.preset_items:
                self.preset_items[state["preset"]].set_active(True)
            self.bypass_item.set_active(self.bypassed)
            self.auto_item.set_active(state["auto"])
        finally:
            self.updating = False
        note = state.get("note") or "no review yet"
        self.note_item.set_label("Claude: " + (note if len(note) <= 90 else note[:87] + "..."))
        self.note_item.set_tooltip_text(state.get("note") or None)
        self._set_icon(icon)
        return False

    def _maybe_quit(self) -> bool:
        if self.popup and not self.pending and not self.windows and not self.menu.get_visible():
            Gtk.main_quit()
        return False

    def _set_icon(self, name: str) -> None:
        if self.popup:
            return
        if self.icon:
            self.icon.set_from_file(str(ICONS / f"{name}.svg"))
        else:
            self.indicator.set_icon_full(name, name)

    # -- actions --------------------------------------------------------------

    def cli(self, *args: str, done=None) -> None:
        """Run a streamstack subcommand in the background, then refresh."""
        self.pending += 1

        def work() -> None:
            result = subprocess.run(
                [CLI, *args], capture_output=True, text=True, check=False, env=dict(os.environ, NO_COLOR="1")
            )
            output = ANSI.sub("", result.stdout + result.stderr).strip()
            GLib.idle_add(self._finished, done, result.returncode, output)

        threading.Thread(target=work, daemon=True).start()

    def _finished(self, done, code: int, output: str) -> bool:
        self.busy = False
        self.pending -= 1
        if done:
            done(code, output)
        if self.popup:
            self._maybe_quit()
        else:
            self.refresh()
        return False

    def cli_detached(self, *args: str) -> None:
        detach([CLI, *args])

    def open_console(self, *options: str) -> None:
        """Open the Console: the detached command is the server when none runs yet, else it just opens a window."""
        self.cli_detached("console", *options)

    def toggle_studio(self) -> None:
        self.busy = True
        self.studio_item.set_sensitive(False)
        if self.state.get("obs"):
            notify("Stopping the studio", "Calls fall back to the raw camera.")
            self.cli("stop", done=lambda code, out: notify("Studio stopped", out.splitlines()[-1] if out else ""))
        else:
            notify("Starting the studio", "Easy Effects, the camera and OBS with the virtual camera.")
            self.cli(
                "studio",
                done=lambda code, out: notify("Studio started" if code == 0 else "Studio needs attention", out),
            )

    def open_obs(self) -> None:
        if self.state.get("obs"):
            notify("OBS is already running", "Find it in the window list.")
        else:
            self.cli("studio")

    def pick_preset(self, item: Gtk.RadioMenuItem, name: str) -> None:
        if self.updating or not item.get_active():
            return
        self.cli(
            "voice", name, done=lambda code, out: notify("Voice preset" if code == 0 else "Voice preset failed", out)
        )

    def toggle_bypass(self, item: Gtk.CheckMenuItem) -> None:
        if self.updating:
            return
        self.bypassed = item.get_active()
        detach(["easyeffects", "-b", "1" if self.bypassed else "2"])
        notify("Voice effects bypassed" if self.bypassed else "Voice effects on")
        if not self.popup:
            self.refresh()

    def toggle_auto(self, item: Gtk.CheckMenuItem) -> None:
        if self.updating:
            return
        if item.get_active():
            notify(
                "Auto mode on", "Exposure and mic gain follow the room; Claude reviews the picture every few minutes."
            )
            self.cli("auto", "on", done=lambda code, out: code and notify("Auto mode needs attention", out))
        else:
            self.cli("auto", "off", done=lambda code, out: notify("Auto mode off", "Current look and levels kept."))

    def auto_once(self) -> None:
        notify("Asking Claude", "Talk normally for 4 seconds while the mic is measured.")
        self.show_output("Claude's suggestion", "auto", "once")

    def auto_reset(self) -> None:
        if self.state.get("auto"):
            self.cli("auto", "off", done=lambda code, out: self._reset_now())
        else:
            self._reset_now()

    def _reset_now(self) -> None:
        self.cli(
            "auto",
            "reset",
            done=lambda code, out: notify("Auto changes undone" if code == 0 else "Undo needs attention", out),
        )

    def mic_check(self) -> None:
        notify("Mic check", "Talk for 6 seconds (raw), then 6 seconds (processed). Playback follows.")
        self.cli("mic-check", "--raw", done=lambda code, out: notify("Mic check finished"))

    def show_output(self, title: str, *args: str) -> None:
        self.cli(*args, done=lambda code, out: self._text_window(title, out))

    def _text_window(self, title: str, text: str) -> None:
        window = Gtk.Window(title=title)
        window.set_default_size(760, 560)
        view = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            monospace=True,
            left_margin=12,
            right_margin=12,
            top_margin=12,
            bottom_margin=12,
        )
        view.get_buffer().set_text(text)
        scroller = Gtk.ScrolledWindow()
        scroller.add(view)
        window.add(scroller)
        self.windows += 1

        def closed(_w) -> None:
            self.windows -= 1
            self._maybe_quit()

        window.connect("destroy", closed)
        window.show_all()


def main() -> int:
    if "--menu" in sys.argv[1:]:
        Tray(popup=True)
        Gtk.main()
        return 0
    # One icon per session, however often autostart and the menu launch it.
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/streamstack-{os.getuid()}")
    runtime.mkdir(parents=True, exist_ok=True)
    lock = open(runtime / "streamstack-tray.lock", "w")  # noqa: SIM115 - held for the process lifetime
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    Tray()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
