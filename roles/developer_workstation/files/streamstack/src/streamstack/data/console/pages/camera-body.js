// pages/camera-body.js — the Canon body on the Camera page: ISO, shutter, aperture, exposure compensation, white
// balance (and Kelvin), Picture Style, live-view autofocus and focus nudges, read back from the camera.
//
// It adds a “Canon body” tab next to Looks in the right column and does nothing else to the page. Everything shown
// comes from state.video.canon (the bridge's own reading of the camera): choices, read-only flags and ranges are
// the camera's, never assumed here. A control the body refuses explains why (for example “turn the mode dial to M”).
// Changes go through video.canon_set / video.canon_action; the value on screen is whatever the camera reports back.

import { h } from '../core/util.js';
import { icon } from '../components/icons.js';

const ORDER = ['iso', 'shutter', 'aperture', 'exposure_comp', 'whitebalance', 'picturestyle'];
const FOCUS_STEPS = [-3, -2, -1, 1, 2, 3];
const STATE_PILL = {
  running: { state: 'ok', text: 'Live' },
  paused: { state: 'info', text: 'Changing a setting…' },
  idle: { state: 'info', text: 'Idle' },
  starting: { state: 'info', text: 'Starting' },
  stalled: { state: 'warn', text: 'Waiting for the camera' },
  offline: { state: 'fail', text: 'Camera offline' },
  stopped: { state: 'fail', text: 'Bridge stopped' },
};

const TEMPLATE = `
<ss-card class="c-body" heading="Canon body" icon="camera" inline-reason data-ref="card">
  <span slot="actions" class="pill" data-ref="pill"></span>
  <div class="cb-scroll">
    <p class="cb-info" data-ref="info"></p>
    <div class="cb-grid" data-ref="grid" role="group" aria-label="Camera settings"></div>
    <div class="cb-kelvin" data-ref="kelvinBox" hidden>
      <ss-fader orientation="horizontal" label="Kelvin" min="2500" max="10000" step="100" default="5600" unit="K" decimals="0" ticks="none" data-ref="kelvin"></ss-fader>
    </div>
    <div class="cb-focus" role="group" aria-label="Focus">
      <span class="eyebrow">Focus</span>
      <ss-button size="sm" icon="cross" data-ref="af" title="Autofocus once (the picture can blank for a moment on some bodies)">Autofocus</ss-button>
      <ss-button size="sm" variant="ghost" data-ref="afCancel" title="Stop an autofocus in progress">Stop</ss-button>
      <span class="cb-nudge" data-ref="nudge" role="group" aria-label="Nudge focus nearer or farther"></span>
    </div>
    <p class="hint cb-session" data-ref="session"></p>
    <p class="hint cb-hint" data-ref="hint" data-state="warn" hidden></p>
  </div>
</ss-card>`;

function prettyOption(id, value) {
  if (id === 'exposure_comp' && value !== undefined && /^[+-]?\d/.test(value) && Number(value) > 0 && !value.startsWith('+')) return `+${value}`;
  return value;
}

export async function mountBody(side, ctx) {
  const { store, act, toast } = ctx;
  await ctx.ensureCss(new URL('./camera-body.css', import.meta.url).href);
  const tabs = h('div', { class: 'side-tabs seg', role: 'group', 'aria-label': 'Right column', hidden: true },
    h('button', { type: 'button', 'data-tab': 'looks', 'aria-pressed': 'true' }, 'Looks'),
    h('button', { type: 'button', 'data-tab': 'body', 'aria-pressed': 'false' }, 'Canon body'));
  const frag = document.createElement('template');
  frag.innerHTML = TEMPLATE;
  const card = frag.content.firstElementChild;
  side.insertBefore(tabs, side.querySelector('.c-looks'));
  side.append(card);
  side.dataset.tab = 'looks';
  const R = {};
  card.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  R.card = card;
  const off = [];
  const rows = new Map();     // id -> { row, select, prev, next, busy }
  const showTab = (name) => {
    side.dataset.tab = name;
    for (const b of tabs.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.tab === name));
  };
  tabs.addEventListener('click', (e) => { const b = e.target.closest('button[data-tab]'); if (b) showTab(b.dataset.tab); });

  const canon = () => store.state.video.canon;
  const control = (id) => canon().controls[id];

  async function change(id, value) {
    const r = rows.get(id);
    if (r) { r.busy = true; r.row.dataset.busy = ''; }
    try {
      const res = await act('video.canon_set', { id, value }, { quiet: true });
      if (res) {
        // show the request until the next snapshot reports what the camera really did
        // (held until a snapshot that began after the camera was told, not for a fixed 600 ms: #157)
        const path = `video.canon.controls.${id}.value`;
        store.settle([path], store.patch({ [path]: String(value) }, { hold: Infinity }), { seq: res.seq, bySeq: true });
        if (res.message) toast(res.message, { level: 'warn', timeout: 5000, id: `canon:${id}` });
      }
    } finally {
      if (r) { r.busy = false; delete r.row.dataset.busy; }
      render();       // snap back to what the camera reports (the next snapshot follows within a second)
    }
  }

  function buildRow(id) {
    const select = h('select', { class: 'cb-select', 'aria-label': id });
    const prev = h('button', { type: 'button', class: 'cb-step', 'aria-label': 'Previous value' }, icon('chevron'));
    const next = h('button', { type: 'button', class: 'cb-step', 'aria-label': 'Next value' }, icon('chevron'));
    prev.classList.add('cb-prev');
    const label = h('label', { class: 'cb-label' });
    const row = h('div', { class: 'cb-row', 'data-id': id }, label, h('div', { class: 'cb-ctl' }, prev, select, next));
    const step = (dir) => {
      const c = control(id);
      if (!c || c.readonly) return;
      const i = c.choices.indexOf(c.value);
      const j = Math.min(c.choices.length - 1, Math.max(0, i + dir));
      if (j !== i && c.choices[j] !== undefined) { select.value = c.choices[j]; change(id, c.choices[j]); }
    };
    prev.addEventListener('click', () => step(-1));
    next.addEventListener('click', () => step(1));
    select.addEventListener('change', () => change(id, select.value));
    return { row, select, prev, next, label, busy: false };
  }

  function render() {
    const c = canon();
    const gone = c.state === 'disabled';
    tabs.hidden = gone;
    if (gone && side.dataset.tab === 'body') showTab('looks');
    const pill = STATE_PILL[c.state] || { state: '', text: c.state };
    R.pill.dataset.state = pill.state;
    R.pill.textContent = pill.text;
    const reason = c.state === 'offline' || c.state === 'stopped' ? (c.message || 'The Canon bridge is not running') : '';
    if (reason) R.card.setAttribute('reason', reason); else R.card.removeAttribute('reason');

    const model = control('model')?.value;
    const lens = control('lens')?.value;
    const battery = control('battery')?.value;
    const dial = control('mode')?.value;
    R.info.textContent = [model, lens, dial && `Mode: ${dial}`, battery && `Battery ${battery}`].filter(Boolean).join('  ·  ') || '';

    for (const id of ORDER) {
      const ctl = control(id);
      let r = rows.get(id);
      if (!ctl) { if (r) { r.row.remove(); rows.delete(id); } continue; }
      if (!r) { r = buildRow(id); rows.set(id, r); }
      if (!r.row.isConnected) R.grid.append(r.row);
      r.label.textContent = ctl.label;
      r.select.setAttribute('aria-label', ctl.label);
      const wanted = ctl.choices.map(String);
      if (!ctl.choices.length && ctl.value !== null) wanted.push(String(ctl.value));
      const have = [...r.select.options].map((o) => o.value);
      if (wanted.join('\n') !== have.join('\n')) {
        r.select.replaceChildren(...wanted.map((v) => h('option', { value: v }, prettyOption(id, v))));
      }
      if (!r.busy && document.activeElement !== r.select) r.select.value = String(ctl.value);
      const locked = ctl.readonly || c.state !== 'running' || r.busy;
      r.select.disabled = locked;
      r.prev.disabled = r.next.disabled = locked;
      r.row.toggleAttribute('data-readonly', !!ctl.readonly);
      r.row.title = ctl.readonly && ctl.reason ? ctl.reason : '';
      let why = r.row.querySelector('.cb-why');
      if (ctl.readonly && ctl.reason) {
        if (!why) { why = h('span', { class: 'cb-why' }); r.row.append(why); }
        why.textContent = ctl.reason;
      } else if (why) why.remove();
    }

    const k = control('kelvin');
    R.kelvinBox.hidden = !k;
    if (k) {
      const lock = k.readonly || c.state !== 'running';
      R.kelvin.toggleAttribute('disabled', lock);
      const usingKelvin = control('whitebalance')?.value === 'Color Temperature';
      R.kelvinBox.dataset.active = usingKelvin ? 'true' : 'false';
      if (!R.kelvin.matches(':active')) R.kelvin.value = Number(k.value) || 5600;
      R.kelvin.title = usingKelvin ? '' : 'Moving this switches white balance to Color Temperature';
    }

    const live = c.state === 'running' || c.state === 'paused';
    for (const b of [R.af, R.afCancel]) b.toggleAttribute('disabled', !live);
    for (const b of R.nudge.querySelectorAll('button')) b.disabled = !live;
    R.session.textContent = c.session === 'shared'
      ? 'Changes apply instantly; the picture keeps running.'
      : c.session === 'pause'
        ? 'This setup pauses the picture for about a second per change. Install the python gphoto2 package for instant changes.'
        : '';
    const hints = [control('liveviewsize')?.hint].filter(Boolean);
    const input = c.input;
    const small = input && input.height < 720;
    const resting = c.state === 'idle';
    R.hint.hidden = !(resting || (small && hints.length));
    R.hint.textContent = resting ? (c.message || 'Live view is off while no app uses the camera.') : (small && hints.length ? `Live view is ${input.width}×${input.height}. ${hints[0]}` : '');
  }

  // ---------- focus ----------
  for (const step of FOCUS_STEPS) {
    const label = `${step < 0 ? 'Nearer' : 'Farther'} ${Math.abs(step)}`;
    const b = h('button', { type: 'button', class: 'cb-nb', title: `Focus ${label.toLowerCase()}`, 'aria-label': `Focus ${label.toLowerCase()}` }, step < 0 ? '‹'.repeat(Math.abs(step)) : '›'.repeat(step));
    b.addEventListener('click', () => act('video.canon_action', { name: 'focus', step }, { quiet: true }));
    R.nudge.append(b);
  }
  R.af.addEventListener('click', () => act('video.canon_action', { name: 'autofocus' }, { quiet: true }));
  R.afCancel.addEventListener('click', () => act('video.canon_action', { name: 'af_cancel' }, { quiet: true }));
  R.kelvin.addEventListener('change', () => change('kelvin', Math.round(R.kelvin.value)));

  off.push(store.subscribe((s) => [s.video.canon.state, s.video.canon.message, s.video.canon.session, s.video.canon.controls, s.video.canon.input], render));
  render();
  return () => { off.forEach((fn) => fn()); tabs.remove(); card.remove(); delete side.dataset.tab; };
}
