# ADR 0006: The Console stays a local web app with the GTK tray; no Tauri or Electron shell

Status: accepted (2026-10-27). Issue #104 (radar item 36, X4). Related: #103 (WHEP preview), radar section "Shell".

## Context

The Console is a local web server (`streamstack-console`) serving plain ES modules and Web Components, opened in
the user's browser by the launcher or by the GTK tray, plus the tray itself (`streamstack-tray`). The question the
radar raised: should a desktop shell (Tauri or Electron) wrap it, to get window chrome, autostart, global shortcuts
and a native preview surface?

What the shell would have to buy, against what we already have:

| Want | Today |
|---|---|
| A window of its own, no browser tabs | Browser app mode (`--app=URL`) or an installed PWA window; the launcher already opens the Console |
| Autostart, tray icon | GTK tray plus an XDG autostart entry (`.deb`) |
| Global shortcuts while a call has focus | The desktop GlobalShortcuts portal and MIDI path (#44), not a shell feature |
| Quality gate | Playwright drives the real Console in CI; the same pages are what people use |
| Low-latency preview | WHEP in the page (#103); a browser's WebRTC stack is the best-tested one there is |

## Options considered

1. **Keep the web Console plus tray (chosen).**
2. **Tauri 2.** Small installers (the radar's figure of 8.6 MB against 244 MB for Electron comes from one weak
   source and was not re-measured). On Linux it renders through WebKitGTK. Tauri's own Linux documentation lists
   rendering problems on some GPU and driver combinations (DMABUF renderer failures, blank windows, a slow path for
   canvas and WebGL content) and tells apps not to ship blanket overrides. WebRTC in WebKitGTK has historically needed
   GStreamer plugins and wry patches (community reports, none newer than 2026 found). A Chromium (CEF) runtime for
   Tauri was still in progress in 2026, with missing webview controls, so it is not something to depend on. Net: the
   shell would put our most latency-sensitive feature, the WHEP preview, on the least-proven WebRTC engine of the
   options.
3. **Electron.** Chromium inside, so WebRTC behaves as in the browser, and Playwright can drive it. But it doubles
   the packaging and security-update surface (a bundled Chromium to patch, one more signed artifact for the apt
   repository, #99), for chrome we can get from the browser's app mode.
4. **A native GTK or Qt window with an embedded WebView.** Same engine risk as Tauri, plus a second UI toolkit.
5. **A native preview window only** (Wayland subsurface or Vulkan surface) next to the web Console. The smallest step
   that could ever be justified; held back until the WHEP preview measurably misses its target.

## Decision

Keep the web Console as the only UI, with the GTK tray as the launcher. Make the Console an installable PWA (issue #143)
(manifest, icon, standalone display mode) and have the launcher prefer a browser app-mode window. Build the WHEP
preview (#103, MJPEG fallback) in the page. Do not adopt Tauri or Electron now, and do not spike a native preview
window unless trigger R1 fires.

## Triggers that reopen this decision

Any one is enough to open a new issue and a time-boxed spike (two days, with measurements recorded in an ADR
amendment). Triggers are written so that a person can check them without judgement.

- **R1, preview misses its target.** The WHEP preview, measured on the demo rig with `tools/` latency measurement,
  is above 1 s glass to glass at the median over 10 minutes, or drops more than 1 percent of frames, on two
  consecutive releases despite the MJPEG fallback working. Action: spike a native preview window (option 5) first,
  not a whole shell.
- **R2, browser capability gap.** A feature the product needs cannot be done from a page, with no portal or
  helper-process workaround: for example, global hotkeys that cannot be delivered through the GlobalShortcuts portal
  on a supported compositor, or capturing a window or screen without a user prompt on every start. Action: spike
  Tauri 2 first, Electron second, scoped to that one capability.
- **R3, supported browser disappears.** No Chromium-based or Firefox browser can be assumed on the supported
  distribution (for example a distribution ships only a sandboxed browser that blocks `--app` windows or local
  loopback access), so the launcher can no longer open the Console. Action: ship a minimal embedded webview.
- **R4, WebKitGTK passes our own gate.** The Playwright suite plus the WHEP latency measurement both pass inside a
  Tauri 2 window on the Arc host (WebKitGTK video and WebRTC on VA-API, no renderer overrides), measured by us. If so,
  a Tauri shell becomes an option on packaging merit alone (size, autostart, signing), and a spike is allowed.
- **R5, distribution demand.** Two or more design partners, or a store listing the product depends on, require a
  single installable desktop app that is not a browser window. Action: re-evaluate with fresh measurements.
- **R6, the page can no longer be the thin client.** Local-only work (the Rust core in the radar, GPU surfaces)
  makes the HTTP and WebSocket hop the measured bottleneck: control latency above 50 ms on the loopback for a key
  press to an applied change. Action: spike a native client over the daemon socket.

Not triggers: a wish for nicer window chrome, a competitor shipping a shell, or the installer size of a PWA, which is
zero.

## Consequences

- No new runtime, toolchain (Rust or Node packaging) or signed artifact to maintain. The apt repository ships one
  Python package.
- The tray and launcher stay supported, and Playwright against the live pages stays the quality gate.
- The preview quality now rests on the browser's WebRTC stack, so #103 carries a measured latency, and its report
  feeds R1.
- Quarterly, when the radar is re-checked, the release captain re-reads R1 to R6 and records which, if any, fired.

## Sources

- [Tauri: Linux graphics issues](https://v2.tauri.app/develop/debug/linux-graphics/) (WebKitGTK rendering problems and
  the warning against blanket overrides).
- [Tauri discussion 8426: WebRTC in WebKitGTK](https://github.com/tauri-apps/tauri/discussions/8426) (needed patches
  and GStreamer plugins).
- Radar item 36 and section "Shell", `docs/sota-radar-2026-10.md`.
- Search of 2026-10-27 found no 2026 evidence on Tauri 2 WebRTC video performance on Linux; R4 exists because that
  is unmeasured, not because it is known bad.
