// pages/lighting-auto.js — lighting automation on the Lighting page: camera follow, the daylight schedule, per-light
// settings and the "why is the light like this" status.
//
// The engine runs in the `streamstack-lights` user service (or, in the demo rig, inside the Console); this file only
// shows `state.lighting.automation` and sends `lighting.auto.*` / `lighting.light.settings` actions. Everything the
// page shows comes from the snapshot, so a change made from another window or the CLI appears here too.
//
// Keys on the page: F camera follow on / off, S schedule on / off, R resume the schedule after a manual change.

import { h, renderList } from '../core/util.js';

const SVG = 'http://www.w3.org/2000/svg';
const svg = (tag, attrs = {}) => {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
};

const pad2 = (n) => String(n).padStart(2, '0');
const clockOf = (minute) => `${pad2(Math.floor(minute / 60) % 24)}:${pad2(minute % 60)}`;
const minuteOf = (text) => {
  const m = /^(\d{1,2}):(\d{2})$/.exec(text || '');
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
};
const KELVIN_LO = 2700;
const KELVIN_HI = 6500;

/** A swatch for a colour temperature: amber at 2700 K to icy blue at 6500 K. */
export function kelvinColor(kelvin) {
  const t = Math.min(1, Math.max(0, (kelvin - KELVIN_LO) / (KELVIN_HI - KELVIN_LO)));
  const stops = [[255, 157, 77], [255, 226, 189], [255, 244, 229], [185, 211, 255]];
  const at = t * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(at));
  const f = at - i;
  const rgb = stops[i].map((c, k) => Math.round(c + (stops[i + 1][k] - c) * f));
  return `rgb(${rgb.join(',')})`;
}

const TEMPLATE_STATUS = `
<section class="la-status" role="region" aria-label="Lighting automation" data-ref="status">
  <div class="la-head">
    <span class="la-owner" data-ref="owner"></span>
    <span class="pill" data-ref="camPill"></span>
    <span class="pill" data-ref="svcPill"></span>
  </div>
  <p class="la-summary" role="status" data-ref="summary"></p>
  <p class="hint" data-ref="svcNote" hidden></p>
  <div class="la-actions">
    <ss-button size="sm" icon="power" data-ref="svcStart" hidden>Start the lights service</ss-button>
    <ss-button size="sm" variant="ghost" icon="undo" data-ref="resume" hidden>Resume the schedule <span class="keycap">R</span></ss-button>
  </div>
</section>`;

const TEMPLATE_PANELS = `
<div class="la-grid">
  <ss-card heading="Camera follow" icon="camera" class="la-follow" data-ref="followCard">
    <ss-toggle slot="actions" label="Follow the camera" tone="amber" data-ref="followOn"></ss-toggle>
    <div class="la-body" role="group" aria-label="Camera follow">
      <p class="hint">The lights switch on when something uses the camera, and off afterwards, but only the lights StreamStack switched on.
        It works with the Console closed. A change you make by hand wins until the next camera session.</p>
      <div class="la-fields">
        <label class="la-field"><span>Switch off after (seconds)</span><input class="text-input" type="number" inputmode="numeric" data-ref="grace" aria-label="Grace period in seconds"></label>
        <label class="la-field"><span>Wait before switching on (seconds)</span><input class="text-input" type="number" inputmode="numeric" data-ref="startDelay" aria-label="Start delay in seconds"></label>
      </div>
      <ss-toggle label="Switch the lights off afterwards" sub="Off: they stay on after the call, at the schedule" data-ref="turnOff"></ss-toggle>
      <fieldset class="la-sources" data-ref="sources"><legend>What counts as "the camera is on"</legend></fieldset>
      <label class="la-field wide"><span>Ignore these apps (comma separated, e.g. Slack, obs-studio)</span><input class="text-input" type="text" maxlength="400" data-ref="ignore" aria-label="Apps to ignore" placeholder="none"></label>
      <label class="la-field wide"><span>Also watch these video devices (comma separated)</span><input class="text-input" type="text" maxlength="400" data-ref="devices" aria-label="Extra video devices" placeholder="/dev/video0"></label>
      <p class="hint" data-ref="watching"></p>
      <div class="la-demo" data-ref="demoCam" hidden>
        <span class="la-demo-l">Demo rig</span>
        <ss-button size="sm" icon="camera" data-ref="demoOpen">Teams opens the camera</ss-button>
        <ss-button size="sm" variant="ghost" icon="x" data-ref="demoClose">Camera released</ss-button>
      </div>
    </div>
  </ss-card>

  <ss-card heading="Daylight schedule" icon="sun" class="la-schedule" data-ref="scheduleCard">
    <ss-toggle slot="actions" label="Follow the schedule" tone="amber" data-ref="scheduleOn"></ss-toggle>
    <div class="la-body" role="group" aria-label="Daylight schedule">
      <p class="hint" data-ref="sunLine"></p>
      <div class="seg" role="group" aria-label="Schedule type" data-tone="amber" data-ref="modes"></div>
      <figure class="la-curve" data-ref="curveBox">
        <svg viewBox="0 0 720 190" role="img" aria-label="Brightness and colour temperature over 24 hours" data-ref="curve"></svg>
        <figcaption class="hint" data-ref="curveNote"></figcaption>
      </figure>
      <div class="la-levels" data-ref="levels" role="group" aria-label="Schedule levels"></div>
      <div class="la-fields">
        <label class="la-field"><span>Fade time (seconds)</span><input class="text-input" type="number" inputmode="numeric" data-ref="fade" aria-label="Fade time in seconds"></label>
        <label class="la-field"><span>Keep my manual change for (minutes, 0 = next call)</span><input class="text-input" type="number" inputmode="numeric" data-ref="hold" aria-label="Minutes a manual change is kept"></label>
        <label class="la-field"><span>Fill light level (% of the key)</span><input class="text-input" type="number" inputmode="numeric" data-ref="fill" aria-label="Fill light percent of the key"></label>
      </div>
      <div class="la-actions">
        <ss-button size="sm" variant="ghost" icon="refresh" data-ref="resetSchedule">Reset the schedule</ss-button>
        <a class="la-link" href="#/auto">Property and location are set on the Auto page</a>
      </div>
      <div class="la-demo" data-ref="demoDirector" hidden>
        <span class="la-demo-l">Demo rig</span>
        <ss-toggle label="Pretend the AI Director drives the lights" data-ref="demoDirectorOn"></ss-toggle>
      </div>
    </div>
  </ss-card>
</div>

<ss-card heading="Light settings" icon="sliders" class="la-lights" data-ref="lightsCard">
  <div class="la-light-rows" data-ref="lightRows" role="group" aria-label="Light settings"></div>
  <p class="hint">A name and a role per light. The key light follows the schedule in full, the fill light at the fill level, a manual light is never touched.
    The limits keep a light inside a range, whatever the schedule asks for.</p>
</ss-card>

<ss-card heading="What happened" icon="list" class="la-events" data-ref="eventsCard">
  <ol class="la-log" data-ref="events" aria-label="Automation log"></ol>
  <p class="hint" data-ref="eventsEmpty">Nothing yet. Camera sessions and manual changes are listed here.</p>
</ss-card>`;

export function mountAutomation(statusHost, panelsHost, ctx) {
  const { store, act, registerKeys, registerCommands } = ctx;
  statusHost.innerHTML = TEMPLATE_STATUS;
  panelsHost.innerHTML = TEMPLATE_PANELS;
  const R = {};
  for (const root of [statusHost, panelsHost]) root.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  const off = [];
  const sub = (selector, cb) => off.push(store.subscribe(selector, cb));
  const auto = () => store.state.lighting.automation;
  const settings = () => auto().settings;

  // ---------- helpers ----------
  /** Wire a number input: shows the stored value unless being edited, sends on change, reverts when refused. */
  const numberField = (input, getValue, send, limit) => {
    input.addEventListener('change', async () => {
      const value = Number(input.value);
      if (input.value.trim() === '' || !Number.isFinite(value)) { input.value = getValue(); return; }
      if ((await send(value)) === null) input.value = getValue(); // refused: show what is stored again
    });
    return (value, limits) => {
      const [lo, hi, step] = limits ?? limit() ?? [];
      if (lo !== undefined) { input.min = lo; input.max = hi; input.step = step; }
      if (document.activeElement !== input) input.value = value;
    };
  };
  const followSend = (patch) => act('lighting.auto.follow', patch);
  const scheduleSend = (patch) => act('lighting.auto.schedule', patch);
  const limit = (key) => () => auto().limits[key];

  const setters = {};
  setters.grace = numberField(R.grace, () => settings().follow.grace_seconds, (v) => followSend({ grace_seconds: v }), limit('grace_seconds'));
  setters.startDelay = numberField(R.startDelay, () => settings().follow.start_delay_seconds, (v) => followSend({ start_delay_seconds: v }), limit('start_delay_seconds'));
  setters.fade = numberField(R.fade, () => settings().schedule.fade_seconds, (v) => scheduleSend({ fade_seconds: v }), limit('fade_seconds'));
  setters.hold = numberField(R.hold, () => settings().schedule.hold_minutes, (v) => scheduleSend({ hold_minutes: v }), limit('hold_minutes'));
  setters.fill = numberField(R.fill, () => settings().schedule.fill_percent, (v) => scheduleSend({ fill_percent: v }), limit('fill_percent'));

  const listField = (input, key) => input.addEventListener('change', async () => {
    const items = input.value.split(',').map((x) => x.trim()).filter(Boolean);
    if ((await followSend({ [key]: items })) === null) input.value = settings().follow[key].join(', ');
  });
  listField(R.ignore, 'ignore');
  listField(R.devices, 'devices');

  R.followOn.addEventListener('change', (e) => followSend({ enabled: e.detail.checked }));
  R.turnOff.addEventListener('change', (e) => followSend({ turn_off: e.detail.checked }));
  R.scheduleOn.addEventListener('change', (e) => scheduleSend({ enabled: e.detail.checked }));
  R.resume.addEventListener('click', () => act('lighting.auto.resume'));
  R.resetSchedule.addEventListener('click', () => act('lighting.auto.defaults', { what: 'schedule' }));
  R.svcStart.addEventListener('click', () => act('lighting.service.enable'));
  R.demoOpen.addEventListener('click', () => act('lighting.demo.camera', { on: true, app: 'Teams' }));
  R.demoClose.addEventListener('click', () => act('lighting.demo.camera', { on: false }));
  R.demoDirectorOn.addEventListener('change', (e) => act('lighting.demo.director', { on: e.detail.checked }));

  // ---------- status banner ----------
  sub((s) => s.lighting.automation, (a) => {
    const st = a.status;
    statusHost.hidden = !a.available;
    R.owner.textContent = st.owner_label;
    R.status.dataset.owner = st.owner;
    R.summary.textContent = st.summary;
    const cam = st.camera;
    R.camPill.dataset.state = cam.in_use ? 'ok' : '';
    R.camPill.replaceChildren(h('span', {}, cam.in_use ? 'Camera in use' : 'Camera idle'));
    R.camPill.title = cam.reason || 'Nothing is using the camera';
    const svc = a.service;
    const svcText = { running: 'Lights service running', console: 'Running in the Console (demo)', stopped: 'Lights service stopped', missing: 'Lights service not installed' }[svc.state] || svc.state;
    R.svcPill.dataset.state = svc.state === 'running' || svc.state === 'console' ? 'ok' : 'warn';
    R.svcPill.replaceChildren(h('span', {}, svcText));
    R.svcNote.hidden = !svc.message;
    R.svcNote.textContent = svc.message || '';
    R.svcStart.hidden = !(svc.state === 'stopped' || svc.state === 'missing');
    const held = Object.values(st.lights || {}).some((l) => l.held);
    R.resume.hidden = !held;
  });

  // ---------- camera follow ----------
  sub((s) => [s.lighting.automation.settings.follow, s.lighting.automation.sources, s.lighting.automation.limits], ([f, sources]) => {
    R.followOn.checked = f.enabled;
    R.turnOff.checked = f.turn_off;
    setters.grace(f.grace_seconds);
    setters.startDelay(f.start_delay_seconds);
    renderList(R.sources, sources, (src) => src.id, (src) => {
      const toggle = h('ss-toggle', { label: src.label });
      toggle.addEventListener('change', (e) => followSend({ [src.id]: e.detail.checked }));
      return toggle;
    }, (toggle, src) => { toggle.checked = !!f[src.id]; toggle.toggleAttribute('disabled', !f.enabled); });
    if (document.activeElement !== R.ignore) R.ignore.value = f.ignore.join(', ');
    if (document.activeElement !== R.devices) R.devices.value = f.devices.join(', ');
    for (const el of [R.grace, R.startDelay, R.ignore, R.devices]) el.toggleAttribute('disabled', !f.enabled);
    R.turnOff.toggleAttribute('disabled', !f.enabled);
  });
  sub((s) => s.lighting.automation.status.camera, (cam) => {
    const bits = cam.apps.map((c) => `${c.app} (${c.process}, ${c.device})`);
    if (cam.obs.length) bits.push(`OBS ${cam.obs.join(', ')}`);
    R.watching.textContent = bits.length ? `Using the camera now: ${bits.join('; ')}.` : 'Nothing is using the camera right now.';
  });
  sub((s) => s.lighting.automation.demo, (demo) => { R.demoCam.hidden = !demo; R.demoDirector.hidden = !demo; });
  sub((s) => s.lighting.automation.status.owner, (owner) => { R.demoDirectorOn.checked = owner === 'director'; });

  // ---------- schedule ----------
  let selectedHour = 12;
  const renderModes = () => {
    const a = auto();
    renderList(R.modes, a.modes, (m) => m.id, (m) => {
      const b = h('button', { type: 'button', 'aria-pressed': 'false' }, m.id === 'sun' ? 'Follow the sun' : '24 hour table');
      b.addEventListener('click', () => scheduleSend({ mode: b.dataset.key }));
      return b;
    }, (b, m) => { b.setAttribute('aria-pressed', String(settings().schedule.mode === m.id)); b.title = m.note; });
  };

  const drawCurve = () => {
    const a = auto();
    const svgEl = R.curve;
    svgEl.replaceChildren();
    const W = 720; const top = 10; const plotH = 120; const stripY = 142; const stripH = 14; const left = 34; const right = 10;
    const plotW = W - left - right;
    const n = a.curve.brightness.length;
    if (!n) return;
    const xOf = (minute) => left + (minute / 1440) * plotW;
    const yOf = (b) => top + plotH - (b / 100) * plotH;
    for (const b of [0, 25, 50, 75, 100]) {
      svgEl.append(svg('line', { x1: left, x2: W - right, y1: yOf(b), y2: yOf(b), class: 'grid' }));
      const t = svg('text', { x: left - 6, y: yOf(b) + 3, class: 'tick', 'text-anchor': 'end' });
      t.textContent = `${b}%`;
      svgEl.append(t);
    }
    for (let hr = 0; hr <= 24; hr += 3) {
      const x = xOf(hr * 60);
      svgEl.append(svg('line', { x1: x, x2: x, y1: top, y2: stripY + stripH, class: 'grid' }));
      const t = svg('text', { x, y: stripY + stripH + 14, class: 'tick', 'text-anchor': 'middle' });
      t.textContent = pad2(hr % 24);
      svgEl.append(t);
    }
    // night shading outside sunrise..sunset
    const rise = minuteOf(a.sun.sunrise); const set = minuteOf(a.sun.sunset);
    if (rise !== null && set !== null && rise < set) {
      svgEl.append(svg('rect', { x: xOf(0), y: top, width: xOf(rise) - xOf(0), height: plotH, class: 'night' }));
      svgEl.append(svg('rect', { x: xOf(set), y: top, width: xOf(1440) - xOf(set), height: plotH, class: 'night' }));
    }
    // colour strip
    a.curve.kelvin.forEach((k, i) => {
      const minute = i * a.curve.step;
      svgEl.append(svg('rect', { x: xOf(minute), y: stripY, width: (a.curve.step / 1440) * plotW + 0.6, height: stripH, fill: kelvinColor(k) }));
    });
    // brightness line
    const pts = a.curve.brightness.map((b, i) => `${xOf(i * a.curve.step).toFixed(1)},${yOf(b).toFixed(1)}`);
    svgEl.append(svg('polyline', { points: pts.join(' '), class: 'line', fill: 'none' }));
    // clock table handles
    if (settings().schedule.mode === 'clock') {
      a.hours.forEach((lv, hr) => {
        const dot = svg('circle', { cx: xOf(hr * 60 + 30), cy: yOf(lv.brightness), r: hr === selectedHour ? 6 : 4, class: hr === selectedHour ? 'dot sel' : 'dot', tabindex: 0, role: 'button', 'aria-label': `Edit ${pad2(hr)}:00` });
        const pick = () => { selectedHour = hr; drawCurve(); renderLevels(); };
        dot.addEventListener('click', pick);
        dot.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } });
        svgEl.append(dot);
      });
    }
    for (const [name, minute] of [['sunrise', rise], ['sunset', set]]) {
      if (minute === null) continue;
      svgEl.append(svg('line', { x1: xOf(minute), x2: xOf(minute), y1: top, y2: stripY + stripH, class: 'sun' }));
      const t = svg('text', { x: xOf(minute) + 3, y: top + 10, class: 'tick' });
      t.textContent = name === 'sunrise' ? `sunrise ${a.sun.sunrise}` : `sunset ${a.sun.sunset}`;
      svgEl.append(t);
    }
    const nowX = xOf(a.now.minute);
    svgEl.append(svg('line', { x1: nowX, x2: nowX, y1: top, y2: stripY + stripH, class: 'now' }));
    svgEl.append(svg('circle', { cx: nowX, cy: yOf(a.now.brightness), r: 4, class: 'nowdot' }));
    R.curveNote.textContent = `Now ${clockOf(a.now.minute)}: ${a.now.slot_label}, ${Math.round(a.now.brightness)}% at ${Math.round(a.now.kelvin)} K.`;
  };

  const levelRow = (label, brightness, kelvin, onBrightness, onKelvin, key) => {
    const bIn = h('input', { class: 'text-input', type: 'number', min: 1, max: 100, step: 1, value: Math.round(brightness), 'aria-label': `${label} brightness in percent` });
    const kIn = h('input', { class: 'text-input', type: 'number', min: KELVIN_LO, max: KELVIN_HI, step: 100, value: Math.round(kelvin), 'aria-label': `${label} colour temperature in Kelvin` });
    const commit = async (input, send) => {
      if (input.value === '') { renderLevels(true); return; }
      if ((await send(Number(input.value))) === null) renderLevels(true); // refused: draw the stored values again
    };
    bIn.addEventListener('change', () => commit(bIn, onBrightness));
    kIn.addEventListener('change', () => commit(kIn, onKelvin));
    const swatch = h('span', { class: 'la-swatch', 'aria-hidden': 'true' });
    swatch.style.setProperty('--sw', kelvinColor(kelvin));
    return h('div', { class: 'la-level', 'data-key': key }, swatch, h('span', { class: 'la-level-l' }, label), h('label', {}, bIn, h('span', {}, '%')), h('label', {}, kIn, h('span', {}, 'K')));
  };

  const renderLevels = (force = false) => {
    const a = auto();
    const sched = settings().schedule;
    const focused = R.levels.contains(document.activeElement);
    if (focused && !force) return; // never rebuild under the cursor
    R.levels.replaceChildren();
    if (sched.mode === 'sun') {
      for (const slot of a.slots) {
        const lv = sched.slots[slot.id];
        const row = levelRow(slot.label, lv.brightness, lv.kelvin,
          (v) => act('lighting.auto.slot', { slot: slot.id, brightness: v }),
          (v) => act('lighting.auto.slot', { slot: slot.id, kelvin: v }), slot.id);
        row.title = slot.note;
        row.classList.toggle('is-now', a.now.slot === slot.id);
        R.levels.append(row);
      }
    } else if (a.hours.length === 24) {
      const picker = h('select', { class: 'text-input', 'aria-label': 'Hour to edit' },
        ...Array.from({ length: 24 }, (_, hr) => h('option', { value: hr }, `${pad2(hr)}:00`)));
      picker.value = String(selectedHour);
      picker.addEventListener('change', () => { selectedHour = Number(picker.value); drawCurve(); renderLevels(); });
      const lv = a.hours[selectedHour];
      R.levels.append(h('div', { class: 'la-hourpick' }, h('span', {}, 'Hour'), picker),
        levelRow(`${pad2(selectedHour)}:00`, lv.brightness, lv.kelvin,
          (v) => act('lighting.auto.hour', { hour: selectedHour, brightness: v }),
          (v) => act('lighting.auto.hour', { hour: selectedHour, kelvin: v }), 'hour'));
    }
  };

  sub((s) => s.lighting.automation, (a) => {
    const sc = a.settings.schedule;
    R.scheduleOn.checked = sc.enabled;
    setters.fade(sc.fade_seconds);
    setters.hold(sc.hold_minutes);
    setters.fill(sc.fill_percent);
    const loc = a.location;
    const sun = a.sun || {};
    R.sunLine.textContent = sun.sunrise
      ? `Sunrise ${sun.sunrise}, sunset ${sun.sunset}${loc.name ? ` at ${loc.name}` : ''}${loc.source === 'timezone' ? ' (place taken from this computer\'s time zone)' : ''}.`
      : 'The sun times could not be worked out for this place.';
    renderModes();
    drawCurve();
    renderLevels();
    R.scheduleCard.toggleAttribute('data-off', !sc.enabled);
  });

  // ---------- per-light settings ----------
  const roleSelect = () => h('select', { class: 'text-input', 'aria-label': 'Role' });
  const lights = () => store.state.lighting.lights;
  const create = (l) => {
    const name = h('input', { class: 'text-input', type: 'text', maxlength: 40, 'aria-label': 'Name' });
    const role = roleSelect();
    for (const r of auto().roles) role.append(h('option', { value: r.id, title: r.note }, r.id === 'key' ? 'Key light' : r.id === 'fill' ? 'Fill light' : 'Manual only'));
    const mkNum = (label, min, max, step) => h('input', { class: 'text-input', type: 'number', min, max, step, 'aria-label': label });
    const minB = mkNum('Dimmest allowed in percent', 1, 100, 1);
    const maxB = mkNum('Brightest allowed in percent', 1, 100, 1);
    const minK = mkNum('Warmest allowed in Kelvin', KELVIN_LO, KELVIN_HI, 100);
    const maxK = mkNum('Coolest allowed in Kelvin', KELVIN_LO, KELVIN_HI, 100);
    const send = async (patch) => {
      if ((await act('lighting.light.settings', { id: row.dataset.key, ...patch })) === null) {
        const light = lights().find((x) => x.id === row.dataset.key);
        if (light) update(row, light); // refused: show what is stored again
      }
    };
    name.addEventListener('change', () => send({ name: name.value.trim() }));
    role.addEventListener('change', () => send({ role: role.value }));
    minB.addEventListener('change', () => send({ min_brightness: Number(minB.value) }));
    maxB.addEventListener('change', () => send({ max_brightness: Number(maxB.value) }));
    minK.addEventListener('change', () => send({ min_kelvin: Number(minK.value) }));
    maxK.addEventListener('change', () => send({ max_kelvin: Number(maxK.value) }));
    const row = h('div', { class: 'la-light', role: 'group' },
      h('label', { class: 'la-field' }, h('span', {}, 'Name'), name),
      h('label', { class: 'la-field' }, h('span', {}, 'Role'), role),
      h('div', { class: 'la-range' }, h('span', {}, 'Brightness limits'), minB, h('span', {}, 'to'), maxB, h('span', {}, '%')),
      h('div', { class: 'la-range' }, h('span', {}, 'Temperature limits'), minK, h('span', {}, 'to'), maxK, h('span', {}, 'K')),
      h('p', { class: 'hint la-state' }));
    row.refs = { name, role, minB, maxB, minK, maxK };
    return row;
  };
  const update = (row, l) => {
    const cfg = settings().lights[l.id] || {};
    const { name, role, minB, maxB, minK, maxK } = row.refs;
    row.setAttribute('aria-label', `${l.default_name} settings`);
    const set = (input, value) => { if (document.activeElement !== input) input.value = value; };
    set(name, cfg.name ?? '');
    name.placeholder = l.default_name;
    role.value = cfg.role ?? 'key';
    set(minB, cfg.min_brightness ?? 1);
    set(maxB, cfg.max_brightness ?? 100);
    set(minK, cfg.min_kelvin ?? KELVIN_LO);
    set(maxK, cfg.max_kelvin ?? KELVIN_HI);
    const run = auto().status.lights[l.id];
    row.querySelector('.la-state').textContent = run && run.state ? run.state : '';
  };
  sub((s) => [s.lighting.lights, s.lighting.automation.settings.lights, s.lighting.automation.status.lights], ([lights]) => {
    R.lightsCard.hidden = lights.length === 0;
    renderList(R.lightRows, lights, (l) => l.id, create, update);
  });

  // ---------- log ----------
  sub((s) => s.lighting.automation.status.events, (events) => {
    R.eventsEmpty.hidden = events.length > 0;
    renderList(R.events, events.map((e, i) => ({ ...e, id: `${e.ts}-${e.kind}-${i}` })), (e) => e.id,
      () => h('li', {}, h('time', {}), h('span', {})),
      (li, e) => { li.querySelector('time').textContent = e.time; li.querySelector('span').textContent = e.text; li.dataset.kind = e.kind; });
  });

  // ---------- keys and commands ----------
  const toggleFollow = () => followSend({ enabled: !settings().follow.enabled });
  const toggleSchedule = () => scheduleSend({ enabled: !settings().schedule.enabled });
  const resume = () => act('lighting.auto.resume');
  off.push(registerKeys([
    { key: 'f', desc: 'Camera follow on / off', group: 'Lighting', run: toggleFollow },
    { key: 's', desc: 'Daylight schedule on / off', group: 'Lighting', run: toggleSchedule },
    { key: 'r', desc: 'Resume the schedule after a manual change', group: 'Lighting', run: resume },
  ]));
  off.push(registerCommands(() => [
    { id: 'Lighting:follow', group: 'Lighting', title: settings().follow.enabled ? 'Stop lights following the camera' : 'Lights follow the camera', icon: 'camera', hint: 'F', keywords: 'lights camera follow automatic call', run: toggleFollow },
    { id: 'Lighting:schedule', group: 'Lighting', title: settings().schedule.enabled ? 'Turn the daylight schedule off' : 'Turn the daylight schedule on', icon: 'sun', hint: 'S', keywords: 'lights daylight schedule time of day sunrise', run: toggleSchedule },
    { id: 'Lighting:resume', group: 'Lighting', title: 'Resume the lighting schedule', icon: 'undo', hint: 'R', keywords: 'lights schedule resume manual hold', run: resume },
  ]));
  return () => off.forEach((fn) => (typeof fn === 'function' ? fn() : undefined));
}
