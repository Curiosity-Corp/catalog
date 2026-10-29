// pages/eyeline.js — the EYE-LINE COACH, mounted by the Camera page (a popover) and, as a badge, by the Live page.
//
//   mountEyeLine(el, ctx)   ->  cleanup     the panel: switch, status, advice, calibration wizard, reminder
//   mountEyeBadge(el, ctx)  ->  cleanup     one small badge: "Camera 12° off", only while the coach has advice
//
// Everything comes from `state.eyeline` (docs/console.md) and goes out as `eyeline.enable` / `eyeline.calibrate` /
// `eyeline.calibration.clear` / `eyeline.reminder`; the only local state is the viewing distance the wizard asks for.
// The coach reads head pose from face landmarks: no picture is stored or sent (docs/auto-framing.md).
//
// Page CSS lives in eyeline.css under `@scope` for the pages that mount it; load it with ensureCss first.

import { h } from '../core/util.js';

const GAZE_TEXT = {
  lens: 'at the lens',
  screen: 'at the screen',
  down: 'down at the keyboard',
  away: 'away from the screen',
};

const STATE_TEXT = {
  off: 'Off',
  starting: 'Starting…',
  running: 'Watching',
  'no-face': 'No face in view',
  unavailable: 'Unavailable',
};

const TONE = { running: 'ok', 'no-face': 'warn', unavailable: 'fail', starting: 'info', off: '' };
const LENS_DEGREES = 6; // how far from the lens the call window may sit and still read as looking at the camera

/** "Watching · looking at the screen · Intel Arc A380 (GPU)" — whatever of it is known. */
export function statusLine(e) {
  const parts = [STATE_TEXT[e.state] ?? e.state];
  if (e.state === 'running' && e.gaze) parts.push(`looking ${GAZE_TEXT[e.gaze] ?? e.gaze}`);
  if (e.state === 'running' && e.device) parts.push(e.device);
  return parts.join(' · ');
}

/** The badge text, or null when the coach has nothing to say about the camera position. */
export function badgeText(e) {
  if (!e?.enabled || e.state !== 'running') return null;
  const a = e.advice;
  if (!a || !['lower', 'raise', 'centre'].includes(a.kind) || typeof a.degrees !== 'number') return null;
  return `Camera ${Math.round(a.degrees)}° off${a.approximate ? ' (rough)' : ''}`;
}

/** How far from the lens the call window's centre may sit, in centimetres, at a viewing distance. */
export function windowOffsetCm(viewingCm) {
  if (!(viewingCm > 0)) return 0;
  return Math.round(viewingCm * Math.tan((LENS_DEGREES * Math.PI) / 180) * 10) / 10;
}

export function mountEyeLine(el, ctx) {
  const { store, act } = ctx;
  const off = [];

  const power = h('button', { type: 'button', class: 'el-switch', role: 'switch', 'aria-checked': 'false', 'aria-label': 'Eye-line coach' },
    h('span', { class: 'el-knob' }));
  power.addEventListener('click', () => act('eyeline.enable', { on: power.getAttribute('aria-checked') !== 'true' }));
  const status = h('span', { class: 'el-status', role: 'status' });
  const advice = h('p', { class: 'el-advice' });
  const shares = h('p', { class: 'el-shares' });
  const note = h('p', { class: 'el-note', hidden: true });

  // ---- the wizard: look at the lens, see the thirds grid, size the window distance ----
  const calibrate = h('button', { type: 'button', class: 'el-btn' }, 'Calibrate');
  calibrate.addEventListener('click', () => act('eyeline.calibrate'));
  const clear = h('button', { type: 'button', class: 'el-btn el-btn-quiet' }, 'Clear');
  clear.addEventListener('click', () => act('eyeline.calibration.clear'));
  const progress = h('progress', { class: 'el-progress', max: '1', value: '0', 'aria-label': 'Calibration progress', hidden: true });
  const calText = h('p', { class: 'el-step-text' });
  const marker = h('div', { class: 'el-eyeline', hidden: true });
  const grid = h('div', { class: 'el-grid', role: 'img', 'aria-label': 'Thirds grid with your eye line' },
    h('div', { class: 'el-target' }), marker);
  const gridText = h('p', { class: 'el-step-text' });
  const distance = h('input', { type: 'number', class: 'el-input', min: '20', max: '300', step: '5', value: '60', 'aria-label': 'Viewing distance in centimetres' });
  const reach = h('p', { class: 'el-step-text' });
  const showReach = () => {
    const cm = Number(distance.value);
    reach.textContent = cm > 0
      ? `Keep the centre of the call window within ${windowOffsetCm(cm)} cm of the lens: that is ${LENS_DEGREES}° at ${cm} cm.`
      : 'Enter how far your eyes are from the screen.';
  };
  distance.addEventListener('input', showReach);
  showReach();
  const wizard = h('details', { class: 'el-wizard' },
    h('summary', {}, 'Camera position'),
    h('ol', { class: 'el-steps' },
      h('li', {}, h('strong', {}, 'Look at the lens'), h('div', { class: 'el-row' }, calibrate, clear, progress), calText),
      h('li', {}, h('strong', {}, 'Line up your eyes'), grid, gridText),
      h('li', {}, h('strong', {}, 'Place the call window'),
        h('label', { class: 'el-field' }, 'Distance to the screen (cm) ', distance), reach)));

  // ---- the reminder ----
  const remindOn = h('input', { type: 'checkbox', 'aria-label': 'Remind me when I look away during a call' });
  remindOn.addEventListener('change', () => act('eyeline.reminder', { on: remindOn.checked }));
  const minutes = h('input', { type: 'number', class: 'el-input el-minutes', min: '1', max: '30', step: '1', value: '3', 'aria-label': 'Minutes before the reminder' });
  minutes.addEventListener('change', () => {
    const n = Number(minutes.value);
    if (Number.isInteger(n) && n >= 1 && n <= 30) act('eyeline.reminder', { on: remindOn.checked, minutes: n });
  });
  const remind = h('label', { class: 'el-remind' }, remindOn, ' Remind me after ', minutes, ' minutes of looking away during a call');

  el.append(h('div', { class: 'el' },
    h('div', { class: 'el-row' }, power, h('span', { class: 'el-title' }, 'Eye-line coach'), status),
    advice, shares, note, wizard, remind,
    h('p', { class: 'el-privacy' }, 'Off until you turn it on. It reads where your head points, not a picture: nothing is stored or sent.')));

  off.push(store.subscribe((s) => s.eyeline, (e) => {
    if (!e) return;
    power.setAttribute('aria-checked', String(e.enabled));
    power.disabled = !e.available && !e.enabled;
    power.title = power.disabled ? 'Not available on this machine.' : '';
    status.textContent = statusLine(e);
    status.dataset.state = TONE[e.state] ?? '';
    const running = e.enabled && e.state === 'running';
    advice.textContent = running ? (e.advice?.text ?? '') : '';
    advice.dataset.kind = running ? (e.advice?.kind ?? '') : '';
    advice.hidden = !running || !e.advice?.text;
    const s = e.shares ?? {};
    shares.textContent = running
      ? `Last 30 seconds: lens ${Math.round((s.lens ?? 0) * 100)}% · screen ${Math.round((s.screen ?? 0) * 100)}% · keyboard ${Math.round((s.down ?? 0) * 100)}% · away ${Math.round((s.away ?? 0) * 100)}%`
      : '';
    shares.hidden = !running;
    const why = e.state === 'unavailable' ? e.reason : (!e.available && !e.enabled ? e.unavailable_hint : null);
    note.hidden = !why;
    note.textContent = why ?? '';

    calibrate.disabled = !e.enabled || e.calibrating;
    clear.disabled = !e.calibrated;
    progress.hidden = !e.calibrating;
    progress.value = e.calibration_progress ?? 0;
    calText.textContent = e.calibrating
      ? 'Keep looking at the lens…'
      : e.calibrated
        ? `Calibrated: your lens pose is ${e.baseline.pitch}° pitch, ${e.baseline.yaw}° yaw. Angles are measured from it.`
        : e.enabled ? 'Look straight into the lens and press Calibrate, so the angles are exact.' : 'Turn the coach on, then calibrate.';
    if (!e.calibrated && e.enabled) wizard.open = true;
    const line = running ? e.eye_line : null;
    marker.hidden = line == null;
    if (line != null) marker.style.setProperty('--y', String(Math.min(Math.max(line, 0), 1)));
    grid.querySelector('.el-target').style.setProperty('--y', String(e.eye_line_target ?? 1 / 3));
    gridText.textContent = e.eye_line_hint ?? 'Your eye line appears here once the coach has seen you.';
    remindOn.checked = !!e.reminder?.on;
    if (document.activeElement !== minutes) minutes.value = String(e.reminder?.minutes ?? 3);
    minutes.disabled = !e.reminder?.on;
  }));

  return () => {
    off.forEach((fn) => fn());
    el.replaceChildren();
  };
}

export function mountEyeBadge(el, ctx) {
  const badge = h('span', { class: 'el-badge', role: 'status', hidden: true });
  el.append(badge);
  const off = ctx.store.subscribe((s) => s.eyeline, (e) => {
    const text = badgeText(e);
    badge.hidden = text == null;
    badge.textContent = text ?? '';
    badge.title = text == null ? '' : e.advice.text;
  });
  return () => {
    off();
    el.replaceChildren();
  };
}
