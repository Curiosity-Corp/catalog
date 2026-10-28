// pages/framing.js — AUTO-FRAMING control, mounted by the Camera page (panel) and the Live page (compact pad).
//
//   mountFraming(el, ctx, { compact })  ->  cleanup
//
// One segmented control (Off / Gentle / Tight / Follow), a Hold toggle and a status line: the device the face
// detector runs on and how long one detection takes. The panel adds a small map of the crop window inside the
// camera frame and says why framing is unavailable, with the fix. Everything comes from `state.framing`
// (docs/console.md) and goes out as `framing.mode` / `framing.shot` / `framing.subjects` / `framing.handoff` / `framing.hold` / `framing.reset`; there is no local state.
//
// Page CSS lives in framing.css under `@scope` for the pages that mount it; load it with ensureCss first.

import { h } from '../core/util.js';

const MODES = [
  { name: 'off', label: 'Off', blurb: 'The camera picture as it is.' },
  { name: 'gentle', label: 'Gentle', blurb: 'Slow, small corrections.' },
  { name: 'tight', label: 'Tight', blurb: 'Closer on your face.' },
  { name: 'follow', label: 'Follow', blurb: 'Stays with you when you move.' },
];

const STATE_TEXT = {
  off: 'Off',
  starting: 'Starting…',
  tracking: 'Tracking',
  held: 'Held',
  'no-face': 'No face in view',
  unavailable: 'Unavailable',
};

const TONE = { tracking: 'ok', held: 'info', 'no-face': 'warn', unavailable: 'fail', starting: 'info', off: '' };

/** "Tracking · Intel Arc A380 (GPU) · 3.4 ms" — whatever of it is known. */
export function statusLine(f) {
  const parts = [STATE_TEXT[f.state] ?? f.state];
  if (f.state !== 'off' && f.state !== 'unavailable') {
    if (f.device) parts.push(f.device);
    if (typeof f.detect_ms === 'number') parts.push(`${f.detect_ms.toFixed(1)} ms`);
    if (f.state === 'tracking' && typeof f.zoom === 'number' && f.zoom > 1.005) parts.push(`${f.zoom.toFixed(2)}×`);
  }
  return parts.join(' · ');
}

export function mountFraming(el, ctx, { compact = false } = {}) {
  const { store, act } = ctx;
  const off = [];
  const buttons = new Map();
  const seg = h('div', { class: 'fr-seg', role: 'group', 'aria-label': 'Auto-framing mode' });
  for (const m of MODES) {
    const b = h('button', { type: 'button', 'data-mode': m.name, title: m.blurb, 'aria-pressed': 'false' }, m.label);
    b.addEventListener('click', () => act('framing.mode', { mode: m.name }));
    buttons.set(m.name, b);
    seg.append(b);
  }
  const hold = h('button', { type: 'button', class: 'fr-hold', 'aria-pressed': 'false', title: 'Freeze the crop where it is' }, 'Hold');
  hold.addEventListener('click', () => act('framing.hold', { on: hold.getAttribute('aria-pressed') !== 'true' }));
  const recentre = h('button', { type: 'button', class: 'fr-hold', title: 'Forget the current crop and find you again' }, 'Recentre');
  recentre.addEventListener('click', () => act('framing.reset'));
  const shots = h('div', { class: 'fr-seg fr-shots', role: 'group', 'aria-label': 'Shot' });
  const shotButtons = new Map();
  const subjects = h('select', { class: 'fr-subjects', 'aria-label': 'Who to frame' });
  subjects.addEventListener('change', () => act('framing.subjects', { subjects: subjects.value }));
  const follow = h('button', { type: 'button', class: 'fr-hold fr-follow', 'aria-pressed': 'false', title: 'Move the shot to whoever is talking' }, 'Follow speaker');
  follow.addEventListener('click', () => act('framing.handoff', { on: follow.getAttribute('aria-pressed') !== 'true' }));
  const people = h('span', { class: 'fr-people' });
  const status = h('span', { class: 'fr-status', role: 'status' });
  const note = h('p', { class: 'fr-note', hidden: true });
  const win = h('div', { class: 'fr-map', 'aria-hidden': 'true' }, h('div', { class: 'fr-win' }));
  const row = h('div', { class: 'fr-row' }, seg, hold);
  if (compact) {
    el.append(h('div', { class: 'fr fr-compact' }, h('span', { class: 'fr-eyebrow' }, 'Framing'), row, status));
  } else {
    row.append(recentre);
    el.append(h('div', { class: 'fr' }, row, h('div', { class: 'fr-row' }, shots, subjects, follow),
      h('div', { class: 'fr-line' }, win, status, people), note));
  }
  const winEl = win.firstElementChild;

  off.push(store.subscribe((s) => s.framing, (f) => {
    if (!f) return;
    for (const [name, b] of buttons) {
      b.setAttribute('aria-pressed', String(f.mode === name));
      if (name !== 'off') b.disabled = !f.available && f.mode === 'off';
      b.title = `${MODES.find((m) => m.name === name).blurb}${b.disabled ? ' Not available on this machine.' : ''}`;
    }
    if (shotButtons.size !== (f.shots ?? []).length) {
      shotButtons.clear();
      shots.replaceChildren();
      for (const sh of f.shots ?? []) {
        const b = h('button', { type: 'button', 'data-shot': sh.name, title: sh.blurb ?? '' }, sh.label);
        b.addEventListener('click', () => act('framing.shot', { shot: sh.name }));
        shotButtons.set(sh.name, b);
        shots.append(b);
      }
    }
    for (const [name, b] of shotButtons) {
      b.setAttribute('aria-pressed', String(f.shot === name));
      b.disabled = !f.available && f.mode === 'off';
    }
    const opts = f.subject_options ?? [];
    if (subjects.options.length !== opts.length) {
      subjects.replaceChildren(...opts.map((o) => h('option', { value: o.name }, o.label)));
    }
    subjects.value = f.subjects;
    subjects.disabled = f.shot === 'desk';
    follow.setAttribute('aria-pressed', String(!!f.handoff));
    follow.disabled = f.subjects !== 'group' || f.shot === 'desk';
    people.textContent = f.mode !== 'off' && f.shot !== 'desk' && f.people > 0
      ? `${f.people} ${f.people === 1 ? 'person' : 'people'}${f.handoff && f.speaker ? ' · speaker' : ''}` : '';
    const active = f.mode !== 'off';
    hold.disabled = !active;
    recentre.disabled = !active;
    hold.setAttribute('aria-pressed', String(!!f.held));
    status.textContent = statusLine(f);
    status.dataset.state = TONE[f.state] ?? '';
    const why = f.state === 'unavailable' ? f.reason : (!f.available && f.mode === 'off' ? f.unavailable_hint : null);
    note.hidden = !why;
    note.textContent = why ?? '';
    const w = f.window;
    if (w && f.mode !== 'off') {
      const size = 1 / Math.max(w.zoom, 1);
      winEl.style.setProperty('--w', String(size));
      winEl.style.setProperty('--x', String(Math.min(Math.max(w.cx - size / 2, 0), 1 - size)));
      winEl.style.setProperty('--y', String(Math.min(Math.max(w.cy - size / 2, 0), 1 - size)));
    } else {
      winEl.style.setProperty('--w', '1');
      winEl.style.setProperty('--x', '0');
      winEl.style.setProperty('--y', '0');
    }
  }));

  return () => {
    off.forEach((fn) => fn());
    el.replaceChildren();
  };
}
