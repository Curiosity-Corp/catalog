// pages/slots.js — TIME-OF-DAY PROFILES, mounted by the Camera page (a popover over the picture).
//
//   mountSlots(el, ctx)  ->  cleanup
//
// One profile per part of the day (night, dawn, morning, midday, afternoon, evening): a look, a scene, a voice preset,
// a lighting preset, the camera's exposure compensation and background blur. A strip shows the day split into those
// slots with a marker for now; a mode switch says whether profiles apply by themselves, only suggest, or stay off.
// Everything comes from `state.slots` (docs/slots.md) and goes out as `slots.*` actions; the only local state is the
// open select a person is working in, which is never overwritten while it has focus.
//
// Page CSS lives in slots.css under `@scope` for the Camera page; load it with ensureCss first.

import { h, renderList } from '../core/util.js';

const MODE_LABEL = { off: 'Off', suggest: 'Suggest', auto: 'Automatic' };
const EXPOSURE_NONE = '';

/** "05:30" from minutes after midnight. */
export function clock(minute) {
  const m = Math.max(0, Math.min(1440, Math.round(minute)));
  return `${String(Math.floor(m / 60) % 24).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
}

/** The profile a row's controls describe (parts left at "keep as it is" are left out). */
export function profileOf(row) {
  const profile = {};
  for (const part of ['look', 'scene', 'voice', 'lighting']) {
    const v = row[part].value;
    if (v) profile[part] = v;
  }
  const canon = {};
  if (row.exposure.value !== EXPOSURE_NONE) canon.exposure_comp = row.exposure.value;
  if (Object.keys(canon).length) profile.canon = canon;
  if (row.blur.value === 'on') profile.blur = { on: true };
  else if (row.blur.value === 'off') profile.blur = { on: false };
  return profile;
}

function select(label, part) {
  const s = h('select', { 'aria-label': label, 'data-part': part });
  s.append(h('option', { value: '' }, 'Keep as it is'));
  return s;
}

function fill(sel, names, current) {
  const wanted = ['', ...names, ...(current && !names.includes(current) ? [current] : [])];
  const have = Array.from(sel.options).map((o) => o.value);
  if (wanted.length !== have.length || wanted.some((v, i) => v !== have[i])) {
    sel.replaceChildren(
      ...wanted.map((v) => h('option', { value: v }, v === '' ? 'Keep as it is' : v)),
    );
  }
  if (document.activeElement !== sel) sel.value = current || '';
}

export function mountSlots(el, ctx) {
  const { store, act } = ctx;
  const off = [];
  const root = h('div', { class: 'sl', 'data-ref': 'slots' });
  const modes = h('div', { class: 'sl-seg', role: 'group', 'aria-label': 'When profiles apply' });
  const modeButtons = new Map();
  for (const m of ['off', 'suggest', 'auto']) {
    const b = h('button', { type: 'button', 'data-mode': m, 'aria-pressed': 'false' }, MODE_LABEL[m]);
    b.addEventListener('click', () => act('slots.mode', { mode: m }));
    modeButtons.set(m, b);
    modes.append(b);
  }
  const status = h('p', { class: 'sl-status', role: 'status', 'data-state': 'off' });
  const strip = h('div', { class: 'sl-strip', role: 'img', 'aria-label': 'The day in slots' });
  const marker = h('span', { class: 'sl-now', 'aria-hidden': 'true' });
  const stripWrap = h('div', { class: 'sl-strip-wrap' }, strip, marker);
  const rows = h('div', { class: 'sl-rows' });

  const applyBtn = h('button', { type: 'button', class: 'sl-btn', 'data-act': 'apply' }, 'Apply now');
  const undoBtn = h('button', { type: 'button', class: 'sl-btn', 'data-act': 'undo' }, 'Undo');
  const resumeBtn = h('button', { type: 'button', class: 'sl-btn', 'data-act': 'resume', hidden: true }, 'Hand back to profiles');
  const dismissBtn = h('button', { type: 'button', class: 'sl-btn', 'data-act': 'dismiss', hidden: true }, 'Dismiss');
  const starterBtn = h('button', { type: 'button', class: 'sl-btn', 'data-act': 'starter' }, 'Fill empty slots');
  const fade = h('input', { type: 'number', min: '0', max: '30', step: '1', 'aria-label': 'Fade seconds', 'data-ref': 'fade' });
  const events = h('ol', { class: 'sl-events', 'aria-label': 'What profiles did' });
  const director = h('p', { class: 'sl-note', hidden: true }, 'The AI Director is running: a profile gives it a baseline instead of moving the picture.');

  applyBtn.addEventListener('click', () => act('slots.apply', {}));
  undoBtn.addEventListener('click', () => act('slots.undo'));
  resumeBtn.addEventListener('click', () => act('slots.resume'));
  dismissBtn.addEventListener('click', () => act('slots.dismiss'));
  starterBtn.addEventListener('click', () => act('slots.starter'));
  fade.addEventListener('change', () => {
    const seconds = Number(fade.value);
    if (Number.isFinite(seconds)) act('slots.fade', { seconds });
  });

  root.append(
    h('div', { class: 'sl-head' }, modes, status),
    stripWrap,
    rows,
    h('div', { class: 'sl-foot' },
      applyBtn, undoBtn, resumeBtn, dismissBtn, starterBtn,
      h('label', { class: 'sl-fade' }, 'Lights fade over ', fade, ' s')),
    director,
    events,
  );
  el.append(root);

  const refs = new Map();
  const buildRow = (slot) => {
    const row = {
      look: select('Look', 'look'),
      scene: select('Scene', 'scene'),
      voice: select('Voice preset', 'voice'),
      lighting: select('Lights', 'lighting'),
      exposure: select('Exposure compensation', 'exposure'),
      blur: select('Background blur', 'blur'),
    };
    row.blur.replaceChildren(
      h('option', { value: '' }, 'Keep as it is'),
      h('option', { value: 'on' }, 'Blur on'),
      h('option', { value: 'off' }, 'Blur off'),
    );
    const label = h('span', { class: 'sl-name' }, slot.label);
    const save = h('button', { type: 'button', class: 'sl-mini', 'data-act': 'save', title: 'Store what is on now as this profile' }, 'Use current');
    const clear = h('button', { type: 'button', class: 'sl-mini', 'data-act': 'clear', title: 'Forget this profile' }, 'Clear');
    const now = h('button', { type: 'button', class: 'sl-mini', 'data-act': 'apply-slot', title: 'Apply this profile now' }, 'Apply');
    const send = () => act('slots.profile', { slot: slot.id, profile: profileOf(row) });
    for (const control of Object.values(row)) control.addEventListener('change', send);
    save.addEventListener('click', () => act('slots.save_current', { slot: slot.id }));
    clear.addEventListener('click', () => act('slots.clear', { slot: slot.id }));
    now.addEventListener('click', () => act('slots.apply', { slot: slot.id }));
    const node = h('div', { class: 'sl-row', 'data-slot': slot.id },
      label,
      h('div', { class: 'sl-fields' }, ...Object.values(row)),
      h('div', { class: 'sl-row-acts' }, now, save, clear));
    refs.set(slot.id, { ...row, label, node, clear, now });
    return node;
  };

  const sync = (s) => {
    root.dataset.available = String(s.available);
    for (const [m, b] of modeButtons) b.setAttribute('aria-pressed', String(s.mode === m));
    status.textContent = s.status.summary;
    status.dataset.state = s.status.state;
    undoBtn.disabled = !s.status.can_undo;
    resumeBtn.hidden = !s.status.held;
    dismissBtn.hidden = !s.status.suggestion;
    applyBtn.textContent = s.status.suggestion ? `Apply ${s.status.suggestion.label.toLowerCase()} profile` : 'Apply now';
    applyBtn.disabled = !s.available;
    starterBtn.disabled = !s.available;
    if (document.activeElement !== fade) fade.value = String(s.fade_seconds);
    director.hidden = !s.director.on;

    strip.replaceChildren(...s.strip.map((seg) => {
      const part = h('span', { class: 'sl-seg-part', 'data-slot': seg.slot, title: `${s.slots.find((x) => x.id === seg.slot)?.label ?? seg.slot} ${clock(seg.start)}–${clock(seg.end)}` });
      part.style.setProperty('--w', String(seg.end - seg.start));
      part.toggleAttribute('data-current', seg.slot === s.current.slot);
      return part;
    }));
    marker.style.setProperty('--at', String(s.current.minute / 1440));
    marker.title = `Now ${s.current.clock}`;

    renderList(rows, s.slots, (x) => x.id, buildRow, (node, slot) => {
      const r = refs.get(slot.id);
      const p = slot.profile || {};
      r.node.toggleAttribute('data-current', slot.id === s.current.slot);
      r.node.toggleAttribute('data-empty', slot.empty);
      r.label.textContent = slot.id === s.current.slot ? `${slot.label} (now)` : slot.label;
      fill(r.look, s.options.looks, p.look);
      fill(r.scene, s.options.scenes, p.scene);
      fill(r.voice, s.options.voices, p.voice);
      fill(r.lighting, s.options.lighting, p.lighting);
      fill(r.exposure, s.options.canon.present ? s.options.canon.exposure_choices : [], p.canon?.exposure_comp);
      r.exposure.disabled = !s.options.canon.present || s.options.canon.exposure_readonly;
      r.blur.disabled = !s.options.blur;
      if (document.activeElement !== r.blur) r.blur.value = p.blur ? (p.blur.on ? 'on' : 'off') : '';
      r.clear.disabled = slot.empty;
      r.now.disabled = slot.empty;
    });

    events.replaceChildren(...s.events.slice(0, 8).map((e) => h('li', { 'data-level': e.level }, h('time', {}, e.time), ' ', e.text)));
  };

  off.push(store.subscribe((st) => st.slots, sync));
  return () => { for (const f of off) f(); root.remove(); };
}
