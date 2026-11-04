# Accessibility inventory and manual test checklist

Scope: the `desktop` profile with `minimal_desktop_enabled` (Openbox, LightDM,
Firefox), the GDM/GNOME display path, and the `byod-kiosk` profile (Chromium
kiosk). This is an inventory of what the role sets and a checklist for people to
test it. It is NOT a conformance claim: no assistive-technology testing has been
done, so no standard (WCAG, Section 508, EN 301 549) is claimed to be met.

## Inventory (from the role as of this change)

### Forced browser extensions and policies

| Item | Where | Accessibility note |
|---|---|---|
| Bitwarden force-installed in Firefox | `tasks/collaboration.yml` (Firefox policy) | Extension UI and autofill popups are not tested with a screen reader. |
| Bitwarden force-installed in Chrome, Edge, Brave | same file, Chromium-family policy, opt-out with `bitwarden_browser_extensions_enabled: false` | Same. |
| Firefox enterprise roots | same | None. |
| Firefox `user.js` | `files/minimal-desktop/firefox-user.js` | Sets `ui.prefersReducedMotion` to 1 and a dark content and toolbar theme. Sets no minimum font size, no forced colours, no caret browsing or zoom defaults. |

### Themes and contrast

| Item | Where | Note |
|---|---|---|
| GTK2 and GTK3 theme Arc-Dark | `tasks/minimal-desktop-gtk-theme.yml` | Dark only. No high-contrast theme is installed or offered. |
| LightDM GTK greeter Arc-Dark, fixed colours | `tasks/minimal-desktop-lightdm.yml` | The greeter shows the accessibility indicator (`~a11y`); what it offers depends on installed packages. |
| Openbox theme Onyx-Large, 14 pt bold titles | `files/minimal-desktop/rc.xml` | Larger than default; still a single dark theme. |
| rofi, dunst, tint2 | `files/minimal-desktop/` | Dark, fixed colours; notification timeouts are not reviewed against reading speed. |
| `display_scale` 1 to 4 in 0.25 steps | hardware profile | Scales `Xft.dpi` and cursor size. This is the only magnification-like setting, and it is per hardware profile, not per user. |

### Animation and motion

| Item | Where | Note |
|---|---|---|
| picom fading on, step 0.12, `inactive-opacity` 0.93 | `files/minimal-desktop/picom.conf` | Window fades are on and do NOT follow a reduced-motion preference. Opacity differences between windows can lower contrast. The compositor can be disabled per hardware profile (`compositor_enabled: false`). |
| Openbox `animateIconify: no` | `rc.xml` | Good. |
| LightDM greeter `transition-duration = 0`, type none | `minimal-desktop-lightdm.yml` | Good. |
| Firefox `ui.prefersReducedMotion = 1` | `firefox-user.js` | Good, Firefox only. |
| Kiosk `--disable-pinch`, `--overscroll-history-navigation=0` | `tasks/kiosk-session.yml` | Removes touch zoom: people who rely on pinch zoom lose it. |

### Focus behaviour

| Item | Where | Note |
|---|---|---|
| Focus on new windows, last focus kept, 200 ms delay, no raise on focus | `rc.xml`, kiosk `rc.xml` section | Click-to-focus (`followMouse` is no). Predictable for keyboard and screen-reader users, but a new window taking focus can interrupt reading. |
| Kiosk hides the cursor after 1 s idle (`unclutter`) | `kiosk-session.yml` | Hurts users who track a cursor visually. Needs a documented opt-out. |

### Not present

The role installs no screen reader (Orca), no screen magnifier, no on-screen
keyboard, no sticky-keys or slow-keys configuration and no high-contrast theme,
and sets none of the GNOME accessibility switches (`org.gnome.desktop.a11y`) on
the GNOME path. Whether Orca works on the Openbox session (AT-SPI bus, a
bridge for GTK and Firefox) is unknown.

## Known gaps (stated, not fixed)

1. No high-contrast or light theme choice.
2. Compositor fading and window opacity ignore reduced-motion.
3. No screen reader or magnifier packaged, and none tested.
4. The kiosk hides the cursor and disables pinch zoom with no opt-out.
5. Firefox sets no minimum font size or forced-colours default.
6. Forced Bitwarden extension is untested with assistive technology.

## Manual test checklist

Record for each row: date, tester, OS and package versions, pass or fail, notes.
Do not mark a row passed from reading the config.

### A. Openbox desktop (LightDM then Openbox)

1. LightDM greeter: reach the accessibility menu with the keyboard only; enable a screen reader or large text if offered.
2. Install `orca` on a test host and start it in the Openbox session; confirm it speaks the panel (tint2), the menu and a terminal.
3. Move focus with Tab and Alt+Tab through panel, menu and two windows; confirm the focus indicator is visible and spoken.
4. Open the rofi launcher, start an application with the keyboard only.
5. Notifications: trigger a notification and check the timeout is long enough to read and is announced.
6. Set `display_scale: 2`; confirm text, cursor, panel and icons stay legible and nothing is clipped.
7. With `compositor_enabled: false` versus true, check that window fades and opacity changes are absent or acceptable to a motion-sensitive tester.

### B. GNOME path (GDM)

1. With Orca enabled in Settings, log in, and confirm the screen reader starts and reads the shell.
2. High contrast, large text and reduced animation toggles work and persist after a role run.
3. Magnifier follows keyboard focus.

### C. Firefox (both profiles)

1. Orca (or NVDA on a Windows reference) reads page structure, links and form fields.
2. Bitwarden popup and autofill can be reached and operated by keyboard and read by the screen reader.
3. `ui.prefersReducedMotion` is honoured: site animations that respect `prefers-reduced-motion` are reduced.
4. Zoom to 300 percent and a 24 px minimum font: layout stays usable.
5. Forced colours or a high-contrast OS theme: pages and the browser UI remain readable.

### D. Kiosk (Chromium)

1. A screen reader reads the target page.
2. Pinch zoom, or an equivalent, is available to people who need it (or the gap is recorded).
3. The cursor remains visible when the tester needs it (or the gap is recorded).
4. Keyboard-only navigation reaches every control, and focus is visible.

## Reviewers wanted

Reviews from people who use screen readers, magnification, high-contrast or
reduced-motion settings every day are the most valuable. See the "Only you"
note on the tracking issue: inviting reviewers is a human step.
Findings go in an issue labelled `area-accessibility`; please state the assistive
technology and version used.
