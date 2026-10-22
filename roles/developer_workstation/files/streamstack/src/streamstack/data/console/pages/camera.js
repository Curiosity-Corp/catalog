// pages/camera.js — GRADE THE PICTURE.
//
// Left: the picture (program or the camera you are editing) with overlays (thirds, safe areas, centre
// cross, zebra), a luma histogram and an RGB parade / waveform computed in the browser, scenes and the
// three outputs. Right: grade faders (warmth has a cool↔warm track), sharpen and LUT knobs, and the
// looks library (cards with a thumbnail or a rendered swatch, apply, save, hold-to-delete).
//
// Keys on this page: Z zebra, S safe areas, C centre cross, H scopes, V parade/waveform. G (thirds) is global.
// Everything that needs OBS is disabled with a reason and a one-press “Start studio”; the picture falls
// back to the raw camera while OBS is down, so framing still works.

import { h, renderList, signed, clamp } from '../core/util.js';
import { icon } from '../components/icons.js';
import { apiUrl } from '../core/api.js';
import { toggleVirtualcam, selectScene, setRecord, startStudio } from '../core/ops.js';
import { setUi, onUi, ui } from '../core/ui.js';
import { Scopes } from './camera-scopes.js';
import { drawSwatch, sameLook } from './look-swatch.js';

const STORE_KEY = 'streamstack.console.camera';
const MAX_NAME = 40;

const GRADE = [
  { key: 'brightness', label: 'Brightness' },
  { key: 'contrast', label: 'Contrast' },
  { key: 'gamma', label: 'Gamma' },
  { key: 'saturation', label: 'Saturation' },
  { key: 'warmth', label: 'Warmth', wide: true },
];

const OBS_REASON = {
  offline: { reason: 'OBS is not running', fix: 'Start studio' },
  'no-websocket': { reason: 'OBS is running, but its WebSocket server is off (Tools → WebSocket Server Settings)', fix: '' },
  error: { reason: 'OBS reported an error', fix: '' },
};

// Look thumbnails live for the whole session (module scope): name -> object URL.
const thumbs = new Map();

const TEMPLATE = `
<div class="cam">
  <div class="cam-main"><div class="cam-stack">
    <ss-card bare class="c-preview">
      <div class="pv-cell">
        <ss-preview data-ref="pv" fps="10" label="Camera picture">
          <ss-button slot="empty" variant="primary" icon="power" data-ref="pvFix" hidden>Start studio</ss-button>
          <div class="ov" aria-hidden="true">
            <canvas class="ov-zebra" data-ref="zebra" width="384" height="216"></canvas>
            <div class="ov-safe" data-ref="safe"></div>
            <div class="ov-cross" data-ref="cross"></div>
          </div>
          <div slot="badges" class="badges" data-ref="badges"></div>
          <span slot="corner" class="tc" data-ref="tc"></span>
          <div slot="footer" class="pv-tools">
            <div class="seg pv-seg" role="group" aria-label="Which picture" data-ref="view">
              <button type="button" data-view="source" aria-pressed="true"><span data-ref="viewSourceLabel">Camera</span></button>
              <button type="button" data-view="program" aria-pressed="false">Program</button>
            </div>
            <span class="spacer"></span>
            <ss-button size="sm" icon="grid" data-ref="tGrid" title="Rule-of-thirds grid (G)"><span class="lbl">Thirds</span></ss-button>
            <ss-button size="sm" icon="safe" data-ref="tSafe" title="Title and action safe areas (S)"><span class="lbl">Safe</span></ss-button>
            <ss-button size="sm" icon="cross" data-ref="tCross" title="Centre cross (C)"><span class="lbl">Centre</span></ss-button>
            <ss-button size="sm" icon="zebra" data-ref="tZebra" title="Zebra: stripes where highlights clip (Z)"><span class="lbl">Zebra</span></ss-button>
            <ss-button size="sm" icon="scope" data-ref="tScopes" title="Scopes: histogram and parade (H)"><span class="lbl">Scopes</span></ss-button>
          </div>
        </ss-preview>
      </div>
    </ss-card>

    <section class="c-scopes" aria-label="Scopes" data-ref="scBody">
      <figure class="sc-fig sc-hist">
        <canvas data-ref="hist" role="img" aria-label="Luma histogram with red, green and blue channels"></canvas>
        <div class="sc-over sc-over-l">
          <span class="pill" data-ref="stHigh" title="Share of pixels at or above 94 % brightness. Zebra shows where.">Highlights <b data-ref="stHighV">–</b></span>
          <span class="pill" data-ref="stLow" title="Share of pixels at or below 6 % brightness">Shadows <b data-ref="stLowV">–</b></span>
        </div>
        <figcaption>Histogram</figcaption>
      </figure>
      <figure class="sc-fig sc-scope">
        <canvas data-ref="scope" role="img" aria-label="Colour scope"></canvas>
        <div class="sc-over sc-over-l"><span class="sc-cap" data-ref="scopeCap">RGB parade</span></div>
        <div class="seg sc-seg" role="group" aria-label="Scope type" data-ref="mode">
          <button type="button" data-mode="parade" aria-pressed="true" title="RGB parade (V)">Parade</button>
          <button type="button" data-mode="waveform" aria-pressed="false" title="Luma waveform (V)">Waveform</button>
        </div>
      </figure>
    </section>

    <ss-card bare inline-reason class="c-bottom-card" data-ref="bottomCard"><div class="c-bottom">
      <div class="c-scenes">
        <div class="scene-row"><div class="scene-pads" data-ref="scenes" role="group" aria-label="Scenes"></div></div>
      </div>
      <div class="c-outputs">
        <div class="outs" role="group" aria-label="Outputs">
          <ss-button size="lg" data-ref="oVcam"><span class="o-l">Virtual cam</span><span class="o-s" data-ref="oVcamS">Off</span></ss-button>
          <ss-button size="lg" data-ref="oRec"><span class="o-l">Record</span><span class="o-s mono" data-ref="oRecS">Off</span></ss-button>
          <ss-button size="lg" data-ref="oStream"><span class="o-l">Stream</span><span class="o-s mono" data-ref="oStreamS">Off</span></ss-button>
        </div>
      </div>
    </div></ss-card>
  </div></div>

  <div class="cam-side">
    <ss-card inline-reason class="c-grade" heading="Grade" icon="sliders" data-ref="gradeCard">
      <ss-button slot="actions" size="sm" variant="ghost" icon="refresh" data-ref="reset">Reset</ss-button>
      <div class="src-row"><span class="eyebrow">Editing</span><div class="seg" role="group" aria-label="Camera to edit" data-ref="sources"></div></div>
      <div class="grade-grid" data-ref="grade"></div>
      <div class="fx">
        <div class="fx-block"><ss-toggle label="Sharpen" data-ref="sharpOn"></ss-toggle><ss-fader class="fx-fader" orientation="horizontal" label="Amount" min="0" max="1" step="0.01" default="0.08" ticks="none" data-ref="sharpAmt"></ss-fader></div>
        <div class="fx-block"><ss-toggle label="LUT" data-ref="lutOn"></ss-toggle><ss-fader class="fx-fader" orientation="horizontal" label="Amount" min="0" max="1" step="0.01" default="0.35" ticks="none" data-ref="lutAmt"></ss-fader></div>
      </div>
      <p class="hint grade-hint">Double-click a control to reset it. Hold Shift while dragging for fine control.</p>
    </ss-card>

    <ss-card inline-reason class="c-looks" heading="Looks" icon="camera" data-ref="looksCard">
      <ss-button slot="actions" size="sm" icon="plus" data-ref="saveOpen">Save current look</ss-button>
      <form class="save-form" data-ref="saveForm" hidden novalidate>
        <div class="save-row">
          <input class="text-input" data-ref="saveName" type="text" maxlength="40" autocomplete="off" spellcheck="false" placeholder="Name this look" aria-label="Look name" aria-describedby="saveHint">
          <ss-button variant="primary" size="sm" icon="save" data-ref="saveGo">Save</ss-button>
          <ss-button variant="ghost" size="sm" data-ref="saveCancel">Cancel</ss-button>
        </div>
        <p class="hint" id="saveHint" data-ref="saveHint"></p>
      </form>
      <div class="looks-grid" data-ref="looks" role="list" aria-label="Saved looks"></div>
    </ss-card>
  </div>
</div>`;

function readPrefs() {
  const defaults = { safe: false, cross: false, zebra: false, scopes: true, mode: 'parade', view: 'source' };
  try {
    const raw = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
    return { ...defaults, ...(raw && typeof raw === 'object' ? raw : {}) };
  } catch (err) {
    return defaults;     // storage blocked or corrupt: the page works, it just forgets
  }
}

function writePrefs(prefs) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(prefs)); } catch (err) { /* private window or blocked storage: nothing to remember */ }
}

/** One JPEG of the picture being graded, as an object URL (or null when it cannot be had). */
async function fetchThumb(source) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 4000);
  try {
    const res = await fetch(apiUrl('api/preview.jpg', { source, width: 320 }), { cache: 'no-store', signal: ctrl.signal });
    if (!res.ok) return null;
    const blob = await res.blob();
    return blob.size ? URL.createObjectURL(blob) : null;
  } catch (err) {
    console.warn('look thumbnail unavailable', err);
    return null;
  } finally {
    clearTimeout(timer);
  }
}

export async function mount(host, ctx) {
  const { store, act, actStream, flushStream, registerKeys, registerCommands } = ctx;
  await ctx.ensureCss(new URL('./common.css', import.meta.url).href);
  await ctx.ensureCss(new URL('./camera.css', import.meta.url).href);
  document.title = 'Camera · StreamStack';
  host.innerHTML = TEMPLATE;
  const R = {};
  host.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  const off = [];
  const sub = (selector, cb) => off.push(store.subscribe(selector, cb));
  const prefs = readPrefs();
  const save = () => writePrefs(prefs);

  // ---------- overlays + scopes ----------
  const scopes = new Scopes({ getImage: () => R.pv.image, hist: R.hist, scope: R.scope, zebra: R.zebra });
  scopes.onStats = (s) => {
    const fmt = (v) => (v < 0.001 ? '0%' : v < 0.1 ? `${(v * 100).toFixed(1)}%` : `${Math.round(v * 100)}%`);
    R.stHighV.textContent = s.ok ? fmt(s.clipHigh) : '–';
    R.stLowV.textContent = s.ok ? fmt(s.clipLow) : '–';
    R.stHigh.dataset.state = !s.ok ? '' : s.clipHigh > 0.03 ? 'fail' : s.clipHigh > 0.005 ? 'warn' : 'ok';
    R.stLow.dataset.state = !s.ok ? '' : s.clipLow > 0.1 ? 'warn' : 'ok';
  };
  const applyOverlays = () => {
    R.pv.toggleAttribute('grid', ui.grid);
    R.safe.hidden = !prefs.safe;
    R.cross.hidden = !prefs.cross;
    R.zebra.hidden = !prefs.zebra;
    R.tGrid.toggleAttribute('pressed', ui.grid);
    R.tSafe.toggleAttribute('pressed', prefs.safe);
    R.tCross.toggleAttribute('pressed', prefs.cross);
    R.tZebra.toggleAttribute('pressed', prefs.zebra);
    R.tScopes.toggleAttribute('pressed', prefs.scopes);
    R.scBody.hidden = !prefs.scopes;
    for (const b of R.mode.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.mode === prefs.mode));
    R.scopeCap.textContent = prefs.mode === 'parade' ? 'RGB parade' : 'Luma waveform';
    scopes.configure({ scopes: prefs.scopes, zebra: prefs.zebra, mode: prefs.mode });
  };
  const toggle = (key) => { prefs[key] = !prefs[key]; save(); applyOverlays(); };
  const setMode = (mode) => { prefs.mode = mode; save(); applyOverlays(); };
  R.tGrid.addEventListener('click', () => setUi('grid', !ui.grid));
  R.tSafe.addEventListener('click', () => toggle('safe'));
  R.tCross.addEventListener('click', () => toggle('cross'));
  R.tZebra.addEventListener('click', () => toggle('zebra'));
  R.tScopes.addEventListener('click', () => toggle('scopes'));
  R.mode.addEventListener('click', (e) => { const b = e.target.closest('button[data-mode]'); if (b) setMode(b.dataset.mode); });
  R.pv.addEventListener('dblclick', () => setUi('grid', !ui.grid));
  off.push(onUi((k) => { if (k === 'grid') applyOverlays(); }));
  scopes.start();
  off.push(() => scopes.stop());

  // ---------- which picture ----------
  R.view.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-view]');
    if (!b) return;
    prefs.view = b.dataset.view;
    save();
    syncPicture();
  });
  let syncPicture = () => {};
  sub((s) => [s.video.obs, s.video.selected_source, s.video.sources, s.video.program_scene], () => syncPicture());
  syncPicture = () => {
    const v = store.state.video;
    const obsUp = v.obs === 'connected';
    const sourceName = v.selected_source;
    let source;
    let label;
    if (!obsUp) { source = 'camera'; label = 'Raw camera'; }
    else if (prefs.view === 'program' || !sourceName) { source = 'program'; label = sourceName || 'Camera'; }
    else { source = sourceName; label = sourceName; }
    if (R.pv.getAttribute('source') !== source) R.pv.setAttribute('source', source);
    R.viewSourceLabel.textContent = label;
    const showProgram = obsUp && (prefs.view === 'program' || !sourceName);
    for (const b of R.view.querySelectorAll('button')) {
      b.setAttribute('aria-pressed', String(b.dataset.view === 'program' ? showProgram : !showProgram));
      b.disabled = !obsUp;
    }
    R.pv.setAttribute('label', showProgram ? 'Program picture, what the audience sees' : `Picture from ${label}`);
    R.pv.setAttribute('tally', showProgram && (v.streaming.active || v.virtualcam) ? 'program' : 'off');
  };

  // ---------- OBS availability ----------
  sub((s) => [s.video.obs, s.video.obs_error, s.studio.busy], ([obs, err, busy]) => {
    const r = OBS_REASON[obs];
    let reason = '';
    let fix = '';
    if (r) {
      reason = busy ? 'Starting the studio…' : (obs === 'error' && err ? err : r.reason);
      fix = busy ? '' : r.fix;
    }
    for (const card of [R.gradeCard, R.looksCard, R.bottomCard]) {
      if (reason) card.setAttribute('reason', reason); else card.removeAttribute('reason');
      if (card === R.gradeCard || card === R.looksCard) { if (fix) card.setAttribute('fix', fix); else card.removeAttribute('fix'); }
    }
    R.pvFix.hidden = !fix;
    syncPicture();
  });
  for (const card of [R.gradeCard, R.looksCard]) card.addEventListener('fix', () => startStudio());
  R.pvFix.addEventListener('click', () => startStudio());

  // ---------- badges + timecode ----------
  sub((s) => [s.video.streaming, s.video.recording, s.video.virtualcam, s.video.privacy], ([streaming, rec, vcam, privacy]) => {
    const chips = [];
    const onAir = streaming.active || vcam;
    chips.push(onAir ? { id: 'air', state: 'live', label: 'ON AIR', icon: 'broadcast', pulse: true } : { id: 'air', state: 'off', label: 'OFF AIR', icon: 'dot' });
    if (rec.active) chips.push({ id: 'rec', state: rec.paused ? 'warn' : 'live', label: rec.paused ? 'REC PAUSED' : 'REC', icon: 'record', pulse: !rec.paused });
    if (privacy) chips.push({ id: 'privacy', state: 'warn', label: 'PRIVACY', icon: 'eye-off' });
    renderList(R.badges, chips, (c) => c.id, () => h('ss-status-chip', { size: 'md' }), (el, c) => {
      el.setAttribute('state', c.state); el.setAttribute('label', c.label); el.setAttribute('icon', c.icon); el.toggleAttribute('pulse', !!c.pulse);
    });
    R.tc.textContent = streaming.active ? (streaming.timecode || '') : (rec.active ? (rec.timecode || '') : '');
    syncPicture();
  });

  // ---------- outputs (record / stream stop only on a hold) ----------
  const setOut = (btn, stateEl, on, { onText, hold, offHint }) => {
    btn.toggleAttribute('pressed', on);
    btn.setAttribute('variant', on ? 'danger' : 'default');
    btn.setAttribute('hold', on && hold ? String(hold) : '0');
    stateEl.textContent = on ? onText : 'Off';
    if (on && hold) btn.setAttribute('title', offHint);
    else btn.removeAttribute('title');
  };
  sub((s) => [s.video.virtualcam, s.video.recording, s.video.streaming], ([vcam, rec, stream]) => {
    R.oVcam.toggleAttribute('pressed', vcam);
    R.oVcam.setAttribute('variant', vcam ? 'primary' : 'default');
    R.oVcamS.textContent = vcam ? 'On' : 'Off';
    setOut(R.oRec, R.oRecS, rec.active, { onText: `${rec.paused ? 'Paused ' : ''}${rec.timecode || '00:00:00'} · hold to stop`, hold: 700, offHint: 'Press and hold to stop recording' });
    setOut(R.oStream, R.oStreamS, stream.active, { onText: `${stream.timecode || '00:00:00'} · hold to stop`, hold: 900, offHint: 'Press and hold to end the stream' });
  });
  R.oVcam.addEventListener('click', () => toggleVirtualcam());
  R.oRec.addEventListener('click', () => setRecord(!store.state.video.recording.active));
  R.oStream.addEventListener('click', () => act('video.stream', { on: !store.state.video.streaming.active }));

  // ---------- scenes ----------
  sub((s) => [s.video.scenes, s.video.program_scene], ([scenes, program]) => {
    renderList(R.scenes, scenes, (sc) => sc.name, () => {
      const pad = h('ss-pad', { compact: true });
      pad.addEventListener('click', () => selectScene(pad.dataset.key));
      return pad;
    }, (pad, sc) => {
      const i = scenes.indexOf(sc);
      pad.setAttribute('label', sc.name);
      pad.title = sc.name;
      pad.setAttribute('hotkey', i < 9 ? String(i + 1) : '');
      const live = sc.active || sc.name === program;
      pad.toggleAttribute('active', live);
      if (live) pad.setAttribute('tally', 'program'); else pad.removeAttribute('tally');
    });
  });

  // ---------- source picker ----------
  sub((s) => [s.video.sources, s.video.selected_source], ([sources, selected]) => {
    R.sources.replaceChildren();
    if (!sources.length) { R.sources.append(h('span', { class: 'src-none' }, 'No camera with the grading filter')); return; }
    for (const name of sources) {
      const b = h('button', { type: 'button', 'aria-pressed': String(name === selected) }, h('span', {}, name));
      b.addEventListener('click', () => { flushStream('video.look'); act('video.source', { name }); });
      R.sources.append(b);
    }
  });

  // ---------- grade faders ----------
  const faders = {};
  for (const g of GRADE) {
    const cell = h('div', { class: `gr${g.wide ? ' gr-wide' : ''}`, 'data-key': g.key });
    const f = h('ss-fader', {
      orientation: 'horizontal', label: g.label, step: '0.01', default: '0', bipolar: true, ticks: 'none', signed: true,
      class: `gr-fader gr-${g.key}`,
    });
    f.formatter = (v) => signed(v * 100, 0);
    f.addEventListener('input', (e) => actStream('video.look', { values: { [g.key]: clampTo(g.key, e.detail.value) } }));
    f.addEventListener('change', () => flushStream('video.look'));
    cell.append(f);
    if (g.key === 'warmth') cell.append(h('div', { class: 'warm-caps', 'aria-hidden': 'true' }, h('span', {}, icon('snow'), 'Cool'), h('span', {}, 'Neutral'), h('span', {}, 'Warm', icon('sun'))));
    faders[g.key] = f;
    R.grade.append(cell);
  }
  const limitOf = (key) => store.state.video.limits[key] || [-1, 1];
  function clampTo(key, v) { const [lo, hi] = limitOf(key); return clamp(Number(v), lo, hi); }
  sub((s) => s.video.limits, (limits) => {
    for (const g of GRADE) { const [lo, hi] = limits[g.key] || [-1, 1]; faders[g.key].setAttribute('min', lo); faders[g.key].setAttribute('max', hi); }
    for (const [el, key] of [[R.sharpAmt, 'sharpen_amount'], [R.lutAmt, 'lut_amount']]) { const [lo, hi] = limits[key] || [0, 1]; el.setAttribute('min', lo); el.setAttribute('max', hi); }
  });
  sub((s) => s.video.look, (look) => {
    for (const g of GRADE) faders[g.key].value = look[g.key] ?? 0;
    R.sharpOn.toggleAttribute('checked', !!look.sharpen_enabled);
    R.lutOn.toggleAttribute('checked', !!look.lut_enabled);
    R.sharpAmt.value = look.sharpen_amount ?? 0;
    R.lutAmt.value = look.lut_amount ?? 0;
    R.sharpAmt.toggleAttribute('disabled', !look.sharpen_enabled);
    R.lutAmt.toggleAttribute('disabled', !look.lut_enabled);
  });
  for (const el of [R.sharpAmt, R.lutAmt]) el.formatter = (v) => `${Math.round(v * 100)}%`;
  R.sharpOn.addEventListener('change', (e) => act('video.look', { values: { sharpen_enabled: e.detail.checked } }));
  R.lutOn.addEventListener('change', (e) => act('video.look', { values: { lut_enabled: e.detail.checked } }));
  for (const [el, key] of [[R.sharpAmt, 'sharpen_amount'], [R.lutAmt, 'lut_amount']]) {
    el.addEventListener('input', (e) => actStream('video.look', { values: { [key]: clampTo(key, e.detail.value) } }));
    el.addEventListener('change', () => flushStream('video.look'));
  }
  R.reset.addEventListener('click', () => act('video.look.reset'));

  // ---------- looks library ----------
  const busyLooks = new Set();
  const renderLooks = () => {
    const { looks, look: current } = store.state.video;
    renderList(R.looks, looks, (l) => l.name, (l) => {
      const card = h('div', { class: 'look', role: 'listitem' });
      const main = h('button', { type: 'button', class: 'look-main' },
        h('span', { class: 'look-art' }, h('canvas', { width: 64, height: 40, 'aria-hidden': 'true' }), h('img', { alt: '', hidden: true, decoding: 'async' }), h('span', { class: 'look-on' }, icon('check'), 'Current')),
        h('span', { class: 'look-name' }), h('span', { class: 'look-meta' }));
      main.addEventListener('click', () => applyLook(card.dataset.key));
      const del = h('ss-button', { class: 'look-del', size: 'sm', variant: 'ghost', icon: 'trash', hold: '800' });
      del.addEventListener('click', () => deleteLook(card.dataset.key));
      card.append(main, del);
      card._art = { canvas: main.querySelector('canvas'), img: main.querySelector('img'), values: null };
      return card;
    }, (card, l) => {
      const main = card.firstElementChild;
      main.querySelector('.look-name').textContent = l.name;
      main.querySelector('.look-meta').textContent = l.builtin ? 'Built-in' : 'Yours';
      main.setAttribute('aria-label', `Apply look ${l.name}`);
      const art = card._art;
      const sig = JSON.stringify(l.values);
      if (art.values !== sig) { art.values = sig; drawSwatch(art.canvas, l.values); }
      const url = thumbs.get(l.name);
      if (url) { if (art.img.getAttribute('src') !== url) art.img.src = url; art.img.hidden = false; art.canvas.hidden = true; }
      else { art.img.hidden = true; art.canvas.hidden = false; art.img.removeAttribute('src'); }
      const isCurrent = sameLook(l.values, current);
      card.toggleAttribute('data-current', isCurrent);
      card.toggleAttribute('data-busy', busyLooks.has(l.name));
      main.setAttribute('aria-pressed', String(isCurrent));
      const del = card.querySelector('.look-del');
      del.hidden = l.builtin;
      del.setAttribute('aria-label', `Hold to delete look ${l.name}`);
      del.setAttribute('title', `Hold to delete “${l.name}”`);
    });
  };
  sub((s) => [s.video.looks, s.video.look], renderLooks);

  async function applyLook(name) {
    busyLooks.add(name);
    renderLooks();
    setTimeout(() => { busyLooks.delete(name); if (R.looks.isConnected) renderLooks(); }, 1700);
    await act('video.looks.apply', { name });
  }
  async function deleteLook(name) {
    const res = await act('video.looks.delete', { name });
    if (res?.ok) {
      const url = thumbs.get(name);
      if (url) { URL.revokeObjectURL(url); thumbs.delete(name); }
      renderLooks();
    }
  }

  // save current look: inline form with validation
  const builtinNames = () => new Set(store.state.video.looks.filter((l) => l.builtin).map((l) => l.name.toLowerCase()));
  const checkName = () => {
    const raw = R.saveName.value;
    const name = raw.trim();
    let problem = '';
    let note = `${name.length}/${MAX_NAME}`;
    let state = '';
    // eslint-disable-next-line no-control-regex
    if (/[\u0000-\u001f]/.test(raw)) problem = 'Names cannot contain control characters.';
    else if (name.length > MAX_NAME) problem = `At most ${MAX_NAME} characters.`;
    else if (builtinNames().has(name.toLowerCase())) problem = `“${name}” is a built-in look. Pick another name.`;
    else if (name && store.state.video.looks.some((l) => !l.builtin && l.name === name)) { note = `Replaces your look “${name}”.`; state = 'warn'; }
    else if (!name) note = `Up to ${MAX_NAME} characters. It saves the current grade${store.state.video.selected_source ? ` of ${store.state.video.selected_source}` : ''}.`;
    R.saveHint.textContent = problem || note;
    R.saveHint.dataset.state = problem ? 'error' : state;
    R.saveName.setAttribute('aria-invalid', String(!!problem));
    R.saveGo.toggleAttribute('disabled', !!problem || !name);
    return { name, ok: !problem && !!name };
  };
  const openSave = () => {
    R.saveForm.hidden = false;
    R.saveOpen.hidden = true;
    R.saveName.value = '';
    checkName();
    R.saveName.focus();
  };
  const closeSave = () => { R.saveForm.hidden = true; R.saveOpen.hidden = false; R.saveOpen.focus(); };
  const doSave = async () => {
    const { name, ok } = checkName();
    if (!ok) { R.saveName.focus(); return; }
    R.saveGo.setAttribute('busy', '');
    flushStream('video.look');
    const source = store.state.video.selected_source || 'camera';
    const thumb = fetchThumb(source);
    const res = await act('video.looks.save', { name });
    const url = await thumb;
    R.saveGo.removeAttribute('busy');
    if (res?.ok) {
      const old = thumbs.get(name);
      if (old) URL.revokeObjectURL(old);
      if (url) thumbs.set(name, url);
      closeSave();
      renderLooks();
      R.looks.querySelector(`[data-key="${CSS.escape(name)}"]`)?.scrollIntoView({ block: 'nearest' });
    } else if (url) URL.revokeObjectURL(url);
  };
  R.saveOpen.addEventListener('click', openSave);
  R.saveCancel.addEventListener('click', closeSave);
  R.saveGo.addEventListener('click', doSave);
  R.saveName.addEventListener('input', checkName);
  R.saveForm.addEventListener('submit', (e) => { e.preventDefault(); doSave(); });
  R.saveName.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); doSave(); }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeSave(); }
  });

  // ---------- keys and commands ----------
  applyOverlays();
  syncPicture();
  off.push(registerKeys([
    { key: 'z', desc: 'Zebra: stripes where highlights clip', group: 'Camera', run: () => toggle('zebra') },
    { key: 's', desc: 'Title and action safe areas', group: 'Camera', run: () => toggle('safe') },
    { key: 'c', desc: 'Centre cross on the picture', group: 'Camera', run: () => toggle('cross') },
    { key: 'h', desc: 'Show or hide the scopes', group: 'Camera', run: () => toggle('scopes') },
    { key: 'v', desc: 'Scopes: RGB parade / luma waveform', group: 'Camera', run: () => setMode(prefs.mode === 'parade' ? 'waveform' : 'parade') },
  ]));
  off.push(registerCommands(() => [
    { id: 'cam:zebra', group: 'Camera', title: prefs.zebra ? 'Zebra off' : 'Zebra on', icon: 'zebra', hint: 'Z', keywords: 'zebra clipping highlights exposure', run: () => toggle('zebra') },
    { id: 'cam:safe', group: 'Camera', title: prefs.safe ? 'Hide safe areas' : 'Show safe areas', icon: 'safe', hint: 'S', keywords: 'safe title action area overlay', run: () => toggle('safe') },
    { id: 'cam:cross', group: 'Camera', title: prefs.cross ? 'Hide centre cross' : 'Show centre cross', icon: 'cross', hint: 'C', keywords: 'center centre cross overlay', run: () => toggle('cross') },
    { id: 'cam:scopes', group: 'Camera', title: prefs.scopes ? 'Hide scopes' : 'Show scopes', icon: 'scope', hint: 'H', keywords: 'scopes histogram parade waveform', run: () => toggle('scopes') },
    { id: 'cam:mode', group: 'Camera', title: prefs.mode === 'parade' ? 'Scope: waveform' : 'Scope: RGB parade', icon: 'scope', hint: 'V', keywords: 'parade waveform scope', run: () => setMode(prefs.mode === 'parade' ? 'waveform' : 'parade') },
    { id: 'cam:save', group: 'Camera', title: 'Save current look…', icon: 'save', keywords: 'save look preset grade', run: openSave },
  ]));

  return () => off.forEach((fn) => fn());
}
