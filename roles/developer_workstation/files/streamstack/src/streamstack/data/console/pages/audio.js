// pages/audio.js — THE CHANNEL STRIP, and the outputs.
//
// Two sections behind a switch at the top: Input (the strip below) and Output (pages/audio-outputs.js: every output
// with its volume, mute, default and a sound test).
//
// Input, left: the strip: raw microphone and processed voice meters side by side (peak hold, target band),
// the gain fader and a giant mute. Middle: tone knobs (bass, presence, treble, noise strength), the noise engine and the A/B bypass.
// Right: voice presets with an EQ-curve glyph. Below: the 60 second loudness history and the mic check
// (record raw then processed, listen back with an A/B switch).
//
// A third section, Chain (pages/audio-chain.js), holds every stage of the voice chain as live faders, the
// loudness leveller and the preset library.
//
// Keys on this page: R mic check, A switch the A/B listen clip, I, C and O the Input, Chain and Output sections.
// M (mute) and B (bypass) are global.
// The tone knobs are live while the chain runs; when it is not available, the knobs say so and stay off.

import { h, renderList, clamp } from '../core/util.js';
import { icon, iconHTML } from '../components/icons.js';
import { apiUrl } from '../core/api.js';
import { toggleMute, selectPreset, startStudio } from '../core/ops.js';
import { mmss } from '../core/time.js';
import { mountOutputs, outputBadge } from './audio-outputs.js';
import { mountChain } from './audio-chain.js';

const TARGET = [-24, -12];
const HISTORY_S = 60;
const SAMPLE_MS = 100;
const LUFS_FLOOR = -70;   // BS.1770's absolute gate: quieter than this is not a loudness
const SLOTS = HISTORY_S * 1000 / SAMPLE_MS;
const DB_FLOOR = -60;

const TONE = [
  { key: 'bass_db', label: 'Bass', unit: 'dB', hint: 'Warmth and body' },
  { key: 'presence_db', label: 'Presence', unit: 'dB', hint: 'Clarity, how forward you sound' },
  { key: 'treble_db', label: 'Treble', unit: 'dB', hint: 'Air and sparkle' },
];

// The server does not send a preset's curve, so the glyph of an inactive preset is a sketch of its character
// (matched on the name); the active preset is drawn from the real tone values.
const PROFILES = [
  [/warm/i, { bass_db: 5, presence_db: 1, treble_db: 0 }],
  [/bright/i, { bass_db: 0, presence_db: 4.5, treble_db: 4 }],
  [/deepfilter/i, null],
  [/deep/i, { bass_db: 4, presence_db: 0.5, treble_db: -1 }],
  [/minimal/i, { bass_db: 0, presence_db: 0, treble_db: 0 }],
];
const DEFAULT_PROFILE = { bass_db: 2, presence_db: 2.5, treble_db: 1.5 };
const profileFor = (name) => (PROFILES.find(([re]) => re.test(name))?.[1]) || DEFAULT_PROFILE;

const TEMPLATE = `
<div class="au-page">
<div class="au-tabs">
  <div class="seg" role="group" aria-label="Audio section" data-ref="sections">
    <button type="button" data-section="input" aria-pressed="true">${iconHTML('mic')}Input</button>
    <button type="button" data-section="chain" aria-pressed="false">${iconHTML('sliders')}Chain</button>
    <button type="button" data-section="output" aria-pressed="false">${iconHTML('volume')}Output<span class="au-badge" data-ref="badge" hidden></span></button>
  </div>
</div>
<section class="au-sec" data-section="input" aria-label="Input">
<div class="au">
  <ss-card class="au-strip" heading="Channel" icon="mic" data-ref="micCard">
    <span slot="actions" class="dev" data-ref="micName"></span>
    <div class="strip-body">
      <ss-fader data-ref="gain" label="Gain" unit="dB" step="0.5" default="0" signed decimals="1" accent="green"></ss-fader>
      <div class="strip-meters">
        <ss-meter source="mic" label="Raw mic" target="-24,-12"></ss-meter>
        <ss-meter source="voice" label="Voice" target="-24,-12" scale="none"></ss-meter>
      </div>
      <ss-pad class="strip-mute" big solid tone="red" icon="mic" label="Mute mic" sub="Mic is live" hotkey="M" data-ref="mute"></ss-pad>
    </div>
    <p class="hint strip-note">The shaded band is where speech should sit: <b>-24 to -12 dBFS</b>. Raw mic is before effects, Voice is what the call hears.</p>
  </ss-card>

  <ss-card class="au-tone" heading="Tone" icon="sliders" data-ref="toneCard">
    <div slot="actions" class="tone-actions">
      <span class="pill" data-state="info" data-ref="custom" hidden>Custom</span>
      <ss-button size="sm" variant="ghost" icon="undo" data-ref="toneReset" title="Back to the preset's own sound">Reset to preset</ss-button>
    </div>
    <div class="knobs" data-ref="knobs"></div>
    <p class="hint tone-note" data-ref="toneNote" role="status"></p>
    <div class="ab dn-eng" role="group" aria-label="Noise removal engine" data-ref="engines"></div>
    <p class="hint dn-note" data-ref="dnNote" role="status" hidden></p>
    <div class="ab" role="group" aria-label="Effects A/B compare" data-ref="ab">
      <button type="button" data-bypass="false" aria-pressed="true"><span class="ab-k">A</span><span class="ab-t"><b>Processed</b><small>What the call hears</small></span></button>
      <button type="button" data-bypass="true" aria-pressed="false"><span class="ab-k">B</span><span class="ab-t"><b>Bypassed</b><small>Raw microphone</small></span></button>
    </div>
  </ss-card>

  <ss-card class="au-voice" heading="Voice preset" icon="wave" data-ref="voiceCard">
    <div class="presets" data-ref="presets" role="group" aria-label="Voice presets"></div>
  </ss-card>

  <ss-card class="au-hist" heading="Loudness, last 60 s" icon="pulse">
    <div class="legend" slot="actions" aria-hidden="true">
      <span class="lg lg-mic">Mic</span><span class="lg lg-voice">Voice</span><span class="lg lg-band">Target</span>
    </div>
    <div class="lufs" role="group" aria-label="Loudness in LUFS">
      <div class="lufs-row" data-who="mic"><span class="lufs-who">Mic</span>
        <span class="lufs-v" title="Momentary loudness, 400 ms (BS.1770 K-weighted)"><small>M</small><b data-ref="micM">&ndash;</b></span>
        <span class="lufs-v" title="Short-term loudness, 3 s"><small>S</small><b data-ref="micS">&ndash;</b></span></div>
      <div class="lufs-row" data-who="voice"><span class="lufs-who">Voice</span>
        <span class="lufs-v" title="Momentary loudness, 400 ms (BS.1770 K-weighted)"><small>M</small><b data-ref="voiceM">&ndash;</b></span>
        <span class="lufs-v" title="Short-term loudness, 3 s"><small>S</small><b data-ref="voiceS">&ndash;</b></span></div>
      <span class="lufs-unit">LUFS</span>
    </div>
    <div class="hist-wrap" data-ref="histWrap">
      <canvas data-ref="hist" role="img" aria-label="Scrolling loudness history for the last 60 seconds. Mic and voice, average and peak, with the target band from minus 24 to minus 12 decibels."></canvas>
    </div>
  </ss-card>

  <ss-card class="au-check" heading="Mic check" icon="headphones" data-ref="checkCard">
    <div class="chk">
      <div class="chk-top">
        <button type="button" class="rec" data-ref="rec" aria-describedby="recHint">
          <svg class="ring" viewBox="0 0 100 100" aria-hidden="true"><circle class="ring-bg" cx="50" cy="50" r="44"/><circle class="ring-fg" data-ref="ringFg" cx="50" cy="50" r="44" pathLength="100"/></svg>
          <span class="rec-ic" data-ref="recIc"></span>
          <span class="rec-t" data-ref="recT">Record</span>
        </button>
        <div class="chk-text">
          <p class="chk-step" data-ref="step" role="status" aria-live="polite">Record a few seconds, then hear yourself raw and processed.</p>
          <p class="hint" id="recHint" data-ref="recHint">Speak naturally. Key R.</p>
        </div>
      </div>
      <div class="chk-play" data-ref="play" hidden>
        <div class="seg" role="group" aria-label="Which recording to hear" data-ref="clipSeg">
          <button type="button" data-clip="raw" aria-pressed="true">A · Raw mic</button>
          <button type="button" data-clip="processed" aria-pressed="false">B · Processed</button>
        </div>
        <div class="players">
          <audio controls preload="auto" data-ref="aRaw" aria-label="Raw microphone recording"></audio>
          <audio controls preload="auto" data-ref="aProc" aria-label="Processed voice recording" hidden></audio>
        </div>
      </div>
    </div>
  </ss-card>
</div>
</section>
<section class="au-sec" data-section="chain" aria-label="Chain" data-ref="chainSec" hidden></section>
<section class="au-sec" data-section="output" aria-label="Output" data-ref="outSec" hidden></section>
</div>`;

/** A smooth EQ-curve glyph for three band gains (dB), as an SVG string. */
function eqGlyph({ bass_db: b, presence_db: p, treble_db: t }) {
  const W = 72;
  const H = 30;
  const y = (db) => H / 2 - clamp(db, -8, 8) / 8 * (H / 2 - 3);
  const pts = [[0, b], [W * 0.2, b], [W * 0.52, p], [W * 0.84, t], [W, t]].map(([x, db]) => [x, y(db)]);
  let d = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
  for (let i = 1; i < pts.length; i++) {
    const [x0, y0] = pts[i - 1];
    const [x1, y1] = pts[i];
    const cx = (x0 + x1) / 2;
    d += ` C${cx.toFixed(1)},${y0.toFixed(1)} ${cx.toFixed(1)},${y1.toFixed(1)} ${x1.toFixed(1)},${y1.toFixed(1)}`;
  }
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><line class="eq-zero" x1="0" y1="${H / 2}" x2="${W}" y2="${H / 2}"/><path class="eq-fill" d="${d} L${W},${H / 2} L0,${H / 2} Z"/><path class="eq-line" d="${d}"/></svg>`;
}

export async function mount(host, ctx) {
  const { store, act, actStream, flushStream, registerKeys, registerCommands } = ctx;
  await ctx.ensureCss(new URL('./common.css', import.meta.url).href);
  await ctx.ensureCss(new URL('./audio.css', import.meta.url).href);
  await ctx.ensureCss(new URL('./audio-outputs.css', import.meta.url).href);
  await ctx.ensureCss(new URL('./audio-chain.css', import.meta.url).href);
  document.title = 'Audio · StreamStack';
  host.innerHTML = TEMPLATE;
  const R = {};
  host.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  const off = [];
  const sub = (selector, cb) => off.push(store.subscribe(selector, cb));

  // ---------- sections: Input (the strip) or Output ----------
  const showSection = (which) => {
    for (const b of R.sections.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.section === which));
    for (const el of host.querySelectorAll('.au-sec')) el.hidden = el.dataset.section !== which;
    if (which === 'input') window.dispatchEvent(new Event('resize'));
  };
  R.sections.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-section]');
    if (b) showSection(b.dataset.section);
  });
  off.push(mountChain(R.chainSec, ctx));
  off.push(mountOutputs(R.outSec, ctx));
  sub((s) => s.audio.outputs, (o) => {
    const badge = outputBadge(o);
    R.badge.hidden = !badge;
    if (badge) { R.badge.textContent = badge.text; R.badge.dataset.tone = badge.tone; R.badge.title = badge.label; }
    R.sections.querySelector('[data-section="output"]').setAttribute('aria-label', badge ? `Output, ${badge.label}` : 'Output');
  });

  // ---------- strip: gain, mute, mic presence ----------
  sub((s) => s.audio.gain_range, ([lo, hi]) => {
    R.gain.setAttribute('min', lo);
    R.gain.setAttribute('max', hi);
    const ticks = [];
    for (let v = Math.ceil(lo / 10) * 10; v <= hi; v += 10) ticks.push(v);
    R.gain.setAttribute('ticks', ticks.join(','));
  });
  sub((s) => s.audio.mic.gain_db, (v) => { R.gain.value = v; });
  R.gain.addEventListener('input', (e) => actStream('audio.gain', { db: e.detail.value }));
  R.gain.addEventListener('change', () => flushStream('audio.gain'));
  sub((s) => s.audio.mic, (mic) => {
    R.micName.textContent = mic.present ? (mic.description || mic.name || '') : '';
    if (mic.present) R.micCard.removeAttribute('reason'); else R.micCard.setAttribute('reason', 'No microphone found. Plug it in, it will appear here.');
    R.mute.toggleAttribute('active', mic.muted);
    R.mute.setAttribute('icon', mic.muted ? 'mic-off' : 'mic');
    R.mute.setAttribute('label', mic.muted ? 'MUTED' : 'Mute mic');
    R.mute.setAttribute('sub', mic.muted ? 'Nobody can hear you. Press M to unmute' : 'Mic is live');
    R.gain.toggleAttribute('disabled', !mic.present);
  });
  R.mute.addEventListener('click', () => toggleMute());

  // ---------- tone knobs ----------
  const knobs = {};
  for (const t of TONE) {
    const k = h('ss-knob', { label: t.label, unit: t.unit, step: '0.5', default: '0', signed: true, bipolar: true, decimals: '1', size: 'lg', title: t.hint, 'data-tone': t.key });
    k.addEventListener('input', (e) => actStream('audio.tone', { [t.key]: e.detail.value }));
    k.addEventListener('change', () => flushStream('audio.tone'));
    knobs[t.key] = k;
    R.knobs.append(k);
  }
  // Strength is the noise engine's tone knob: how much of the noise it may take out (lower = more natural)
  R.atten = h('ss-knob', { label: 'Attenuation limit', unit: 'dB', step: '1', default: '100', decimals: '0', size: 'lg', title: 'How much noise the denoiser may remove. Lower leaves more of the room in and sounds more natural.', 'data-denoise': 'atten_limit_db' });
  R.knobs.append(R.atten);
  sub((s) => s.audio.tone_ranges, (ranges) => {
    for (const t of TONE) {
      const [lo, hi] = ranges?.[t.key] || [-12, 12];
      knobs[t.key].setAttribute('min', lo);
      knobs[t.key].setAttribute('max', hi);
    }
  });
  sub((s) => s.audio.tone, (tone) => {
    for (const t of TONE) if (Number.isFinite(tone[t.key])) knobs[t.key].value = tone[t.key];
    R.custom.hidden = !tone.custom;
    R.toneReset.toggleAttribute('disabled', !tone.custom || !tone.supported);
    for (const t of TONE) knobs[t.key].toggleAttribute('disabled', !tone.supported);
    R.toneNote.textContent = !tone.supported
      ? 'Live tone is not available right now (the voice chain is not available). The presets still work.'
      : (tone.custom ? 'You changed the tone. Pick a preset or reset to go back to its own sound.' : 'Drag a knob, double-click to reset it. Shift for fine steps.');
    R.toneNote.dataset.state = tone.supported ? '' : 'warn';
    R.toneCard.toggleAttribute('data-custom', !!tone.custom);
  });
  R.toneReset.addEventListener('click', () => act('audio.preset', { name: store.state.audio.chain.preset }));

  // ---------- noise removal: RNNoise or DeepFilterNet ----------
  // The A/B pad swaps the engine inside the chosen preset (a live preset, like the tone), so tone and preset stay.
  R.engines.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-engine]');
    if (b && !b.disabled && b.getAttribute('aria-pressed') !== 'true') act('audio.denoise', { engine: b.dataset.engine });
  });
  R.atten.addEventListener('input', (e) => actStream('audio.denoise', { atten_limit_db: e.detail.value }));
  R.atten.addEventListener('change', () => flushStream('audio.denoise'));
  sub((s) => s.audio.denoise, (dn) => {
    R.knobs.dataset.engine = dn.engine;
    renderList(R.engines, dn.engines, (en) => en.id, (en) => {
      const b = h('button', { type: 'button', 'data-engine': '' },
        h('span', { class: 'ab-k' }), h('span', { class: 'ab-t' }, h('b'), h('small')));
      return b;
    }, (b, en) => {
      b.dataset.engine = en.id;
      b.querySelector('.ab-k').textContent = dn.engines.indexOf(en) === 0 ? 'A' : 'B';
      b.querySelector('b').textContent = en.label;
      b.querySelector('small').textContent = `${en.effect}, about ${en.delay_ms} ms`;
      b.setAttribute('aria-pressed', String(en.id === dn.engine));
      b.setAttribute('aria-label', `${en.label}, ${en.effect}, about ${en.delay_ms} milliseconds of delay${en.available ? '' : '. Not installed'}`);
      b.disabled = !dn.supported || !en.available;
      if (en.reason) b.title = en.reason; else b.removeAttribute('title');
    });
    const [lo, hi] = dn.atten_range || [0, 100];
    R.atten.setAttribute('min', lo);
    R.atten.setAttribute('max', hi);
    if (Number.isFinite(dn.atten_limit_db)) R.atten.value = dn.atten_limit_db;
    const deep = dn.engine === 'deepfilternet';
    R.atten.toggleAttribute('disabled', !dn.supported || !deep);
    const missing = dn.engines.find((en) => !en.available);
    // only a problem is worth a line: the delay of each engine is on its button
    R.dnNote.textContent = missing ? missing.reason : '';
    R.dnNote.hidden = !missing;
    R.dnNote.dataset.state = missing ? 'warn' : '';
  });

  // ---------- A/B bypass ----------
  R.ab.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-bypass]');
    if (b && !b.disabled) act('audio.bypass', { on: b.dataset.bypass === 'true' });
  });
  const setBypassUi = (bypass, available) => {
    for (const b of R.ab.querySelectorAll('button')) {
      b.setAttribute('aria-pressed', String((b.dataset.bypass === 'true') === bypass));
      b.disabled = !available;
    }
    R.ab.dataset.bypass = String(bypass);
  };

  // ---------- presets ----------
  const glyphKey = new WeakMap();
  sub((s) => [s.audio.chain, s.audio.tone], ([chain, tone]) => {
    setBypassUi(chain.bypass, chain.available);
    renderList(R.presets, chain.presets, (p) => p.name, (p) => {
      const b = h('button', { type: 'button', class: 'preset' },
        h('span', { class: 'p-glyph' }), h('span', { class: 'p-name' }), h('span', { class: 'p-blurb' }));
      b.addEventListener('click', () => selectPreset(b.dataset.key));
      return b;
    }, (b, p) => {
      const active = p.name === chain.preset;
      b.querySelector('.p-name').textContent = p.label.replace(/^Voice (?=\S)/, '');
      b.querySelector('.p-blurb').textContent = p.blurb || '';
      b.setAttribute('aria-pressed', String(active));
      b.disabled = !chain.available || !!p.unavailable;
      if (p.unavailable) b.title = p.unavailable; else b.removeAttribute('title');
      b.toggleAttribute('data-active', active);
      b.setAttribute('aria-label', `${p.label}. ${p.blurb || ''}`);
      const values = active && !tone.custom && tone.supported ? tone : profileFor(p.name);
      const sig = [values.bass_db, values.presence_db, values.treble_db].map((v) => Number(v).toFixed(1)).join();
      if (glyphKey.get(b) !== sig) { glyphKey.set(b, sig); b.querySelector('.p-glyph').innerHTML = eqGlyph(values); }
    });
    if (!chain.available) { R.voiceCard.setAttribute('reason', 'The voice chain plugins are not installed'); R.voiceCard.removeAttribute('fix'); }
    else if (!chain.running) { R.voiceCard.setAttribute('reason', 'The voice chain is not running'); R.voiceCard.setAttribute('fix', 'Start studio'); }
    else { R.voiceCard.removeAttribute('reason'); R.voiceCard.removeAttribute('fix'); }
  });
  R.voiceCard.addEventListener('fix', () => startStudio());

  // ---------- loudness history (60 s, 10 samples/s, newest on the right) ----------
  const series = { micRms: new Float32Array(SLOTS).fill(-90), micPeak: new Float32Array(SLOTS).fill(-90), voiceRms: new Float32Array(SLOTS).fill(-90), voicePeak: new Float32Array(SLOTS).fill(-90) };
  let head = 0;
  let latest = null;
  off.push(store.onMeters((frame) => { latest = frame; }));
  const push = () => {
    const f = latest;
    series.micRms[head] = f?.mic?.rms_db ?? -90;
    series.micPeak[head] = f?.mic?.peak_db ?? -90;
    series.voiceRms[head] = f?.voice?.rms_db ?? -90;
    series.voicePeak[head] = f?.voice?.peak_db ?? -90;
    head = (head + 1) % SLOTS;
  };
  const tick = setInterval(push, SAMPLE_MS);
  off.push(() => clearInterval(tick));

  // LUFS readouts: 4 updates a second is as fast as a number can be read
  const lufsText = (v) => (Number.isFinite(v) && v > LUFS_FLOOR ? v.toFixed(1).replace('-', '\u2212') : '\u2013');
  const showLufs = () => {
    R.micM.textContent = lufsText(latest?.mic?.lufs_m);
    R.micS.textContent = lufsText(latest?.mic?.lufs_s);
    R.voiceM.textContent = lufsText(latest?.voice?.lufs_m);
    R.voiceS.textContent = lufsText(latest?.voice?.lufs_s);
  };
  const lufsTick = setInterval(showLufs, 250);
  off.push(() => clearInterval(lufsTick));

  const g = R.hist.getContext('2d');
  let cssW = 0;
  let cssH = 0;
  let colors = null;
  const readColors = () => {
    const cs = getComputedStyle(R.hist);
    const v = (n) => cs.getPropertyValue(n).trim();
    colors = { accent: v('--accent'), green: v('--green'), text2: v('--text-2'), text3: v('--text-3'), line: v('--line'), amber: v('--amber') };
  };
  const resize = () => {
    const r = R.histWrap.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    cssW = Math.max(1, Math.round(r.width));
    cssH = Math.max(1, Math.round(r.height));
    R.hist.width = Math.round(cssW * dpr);
    R.hist.height = Math.round(cssH * dpr);
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    readColors();
  };
  const ro = new ResizeObserver(resize);
  ro.observe(R.histWrap);
  off.push(() => ro.disconnect());
  const onTheme = () => readColors();
  window.addEventListener('ss:theme', onTheme);
  off.push(() => window.removeEventListener('ss:theme', onTheme));

  const yOf = (db) => cssH - ((clamp(db, DB_FLOOR, 0) - DB_FLOOR) / -DB_FLOOR) * cssH;
  const trace = (arr, color, width, fill) => {
    g.beginPath();
    for (let i = 0; i < SLOTS; i++) {
      const x = (i / (SLOTS - 1)) * cssW;
      const yy = yOf(arr[(head + i) % SLOTS]);
      if (i === 0) g.moveTo(x, yy); else g.lineTo(x, yy);
    }
    g.strokeStyle = color;
    g.lineWidth = width;
    g.lineJoin = 'round';
    g.stroke();
    if (fill) {
      g.lineTo(cssW, cssH);
      g.lineTo(0, cssH);
      g.closePath();
      g.globalAlpha = 0.16;
      g.fillStyle = color;
      g.fill();
      g.globalAlpha = 1;
    }
  };
  const draw = () => {
    if (!colors || cssW < 2) return;
    g.clearRect(0, 0, cssW, cssH);
    // target band
    g.fillStyle = colors.green;
    g.globalAlpha = 0.13;
    g.fillRect(0, yOf(TARGET[1]), cssW, yOf(TARGET[0]) - yOf(TARGET[1]));
    g.globalAlpha = 1;
    // gridlines every 12 dB with labels
    g.font = '10px ui-monospace, monospace';
    g.textBaseline = 'middle';
    for (const db of [0, -12, -24, -36, -48]) {
      const yy = Math.round(yOf(db)) + 0.5;
      g.strokeStyle = colors.line;
      g.lineWidth = 1;
      g.beginPath(); g.moveTo(0, yy); g.lineTo(cssW, yy); g.stroke();
      g.fillStyle = colors.text3;
      g.fillText(String(db), 4, Math.min(cssH - 6, Math.max(7, yy + (db === 0 ? 7 : -7))));
    }
    g.globalAlpha = 0.45;
    trace(series.micPeak, colors.text2, 1, false);
    trace(series.voicePeak, colors.accent, 1, false);
    g.globalAlpha = 1;
    trace(series.micRms, colors.text2, 1.5, true);
    trace(series.voiceRms, colors.accent, 2, true);
  };
  let raf = 0;
  let lastDraw = 0;
  const loop = (now) => {
    raf = requestAnimationFrame(loop);
    if (document.hidden || now - lastDraw < 66) return;
    lastDraw = now;
    draw();
  };
  raf = requestAnimationFrame(loop);
  off.push(() => cancelAnimationFrame(raf));

  // ---------- mic check ----------
  let startedAt = 0;
  let prevState = '';
  let progressTimer = 0;
  const clipUrl = (clip) => (clip ? apiUrl(clip.url) : '');
  const findClip = (clips, kind) => clips.find((c) => (kind === 'raw') === /raw/i.test(c.label)) || null;
  let listenAs = 'raw';

  const setRing = (p) => R.ringFg.style.setProperty('--p', String(clamp(p, 0, 1)));
  const progress = () => {
    const mc = store.state.audio.mic_check;
    const total = 2 * (mc.seconds || 6);
    const el = (performance.now() - startedAt) / 1000;
    setRing(Math.min(0.985, el / total));
    const left = Math.max(0, Math.ceil(total - el));
    R.recT.textContent = mmss(left);
  };
  const stopProgress = () => { clearInterval(progressTimer); progressTimer = 0; };
  const showClip = (which, { keepTime = false } = {}) => {
    listenAs = which;
    const from = which === 'raw' ? R.aProc : R.aRaw;
    const to = which === 'raw' ? R.aRaw : R.aProc;
    const wasPlaying = !from.paused;
    const t = from.currentTime;
    from.pause();
    from.hidden = true;
    to.hidden = false;
    if (keepTime && Number.isFinite(t)) { try { to.currentTime = t; } catch (err) { console.warn('clip seek failed', err); } }
    if (wasPlaying) to.play().catch((err) => console.warn('clip playback blocked', err));
    for (const b of R.clipSeg.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.clip === which));
  };
  R.clipSeg.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-clip]');
    if (b) showClip(b.dataset.clip, { keepTime: true });
  });

  const canRecord = () => {
    const s = store.state.audio;
    return s.mic.present && s.chain.available && s.chain.running && !['recording-raw', 'recording-processed'].includes(s.mic_check.state);
  };
  const startCheck = () => {
    if (!canRecord()) return;
    R.aRaw.pause(); R.aProc.pause();
    act('audio.mic_check', { seconds: store.state.audio.mic_check.seconds || 6 });
  };
  R.rec.addEventListener('click', startCheck);

  sub((s) => [s.audio.mic_check, s.audio.mic.present, s.audio.chain.running, s.audio.chain.available], ([mc, present, running, available]) => {
    const recording = mc.state === 'recording-raw' || mc.state === 'recording-processed';
    if (mc.state !== prevState) {
      if (mc.state === 'recording-raw') startedAt = performance.now();
      else if (mc.state === 'recording-processed' && prevState !== 'recording-raw') startedAt = performance.now() - (mc.seconds || 6) * 1000;
      else if (mc.state === 'recording-raw' || recording) { /* progress continues */ }
      prevState = mc.state;
    }
    if (recording && !progressTimer) { progress(); progressTimer = setInterval(progress, 100); }
    if (!recording) stopProgress();
    R.rec.dataset.state = mc.state;
    R.rec.setAttribute('aria-pressed', String(recording));
    R.rec.disabled = !recording && !(present && available && running);
    R.rec.setAttribute('aria-label', recording ? `Recording, ${R.recT.textContent} left` : 'Start the mic check');
    R.recIc.replaceChildren(icon(recording ? 'pulse' : mc.state === 'done' ? 'undo' : 'mic'));
    if (!recording) {
      setRing(mc.state === 'done' ? 1 : 0);
      R.recT.textContent = mc.state === 'done' ? 'Again' : 'Record';
    }
    const secs = mc.seconds || 6;
    R.step.dataset.state = mc.state;
    R.step.textContent = ({
      idle: `Record ${secs} s of your voice raw, then ${secs} s through the effects, and hear both.`,
      'recording-raw': `Step 1 of 2: recording the raw microphone. Keep talking.`,
      'recording-processed': 'Step 2 of 2: recording through the voice effects. Keep talking.',
      done: 'Done. Switch between A and B to hear what the effects do.',
      error: `The mic check failed: ${mc.error || 'unknown error'}`,
    })[mc.state] || '';
    R.recHint.textContent = !present ? 'No microphone found.' : !available ? 'The voice chain plugins are not installed.' : !running ? 'Start the studio first so the voice chain runs.' : 'Speak naturally. Key R.';
    R.recHint.dataset.state = (!present || !available || !running) ? 'warn' : '';
    const raw = findClip(mc.clips || [], 'raw');
    const proc = findClip(mc.clips || [], 'processed');
    const showPlay = mc.state === 'done' && raw && proc;
    R.play.hidden = !showPlay;
    if (showPlay) {
      const rawUrl = clipUrl(raw);
      const procUrl = clipUrl(proc);
      if (R.aRaw.dataset.src !== rawUrl) { R.aRaw.dataset.src = rawUrl; R.aRaw.src = rawUrl; }
      if (R.aProc.dataset.src !== procUrl) { R.aProc.dataset.src = procUrl; R.aProc.src = procUrl; showClip('raw'); }
    }
  });
  off.push(stopProgress);
  for (const a of [R.aRaw, R.aProc]) {
    a.addEventListener('error', () => { R.step.dataset.state = 'error'; R.step.textContent = 'The recording could not be played. Record again.'; });
  }

  // ---------- keys and commands ----------
  const flipClip = () => { if (!R.play.hidden) showClip(listenAs === 'raw' ? 'processed' : 'raw', { keepTime: true }); };
  off.push(registerKeys([
    { key: 'r', desc: 'Mic check: record, then listen raw and processed', group: 'Audio', run: startCheck },
    { key: 'a', desc: 'Mic check: switch between raw and processed playback', group: 'Audio', run: flipClip },
    { key: 'i', desc: 'Audio: input section', group: 'Audio', run: () => showSection('input') },
    { key: 'c', desc: 'Audio: voice chain section', group: 'Audio', run: () => showSection('chain') },
    { key: 'o', desc: 'Audio: output section', group: 'Audio', run: () => showSection('output') },
  ]));
  off.push(registerCommands(() => [
    { id: 'au:input', group: 'Audio', title: 'Audio: input section', icon: 'mic', hint: 'I', keywords: 'audio input microphone section', run: () => showSection('input') },
    { id: 'au:chain', group: 'Audio', title: 'Audio: voice chain section', icon: 'sliders', hint: 'C', keywords: 'audio chain stages faders compressor gate eq loudness section', run: () => showSection('chain') },
    { id: 'au:output', group: 'Audio', title: 'Audio: output section', icon: 'volume', hint: 'O', keywords: 'audio output speakers headphones section', run: () => showSection('output') },
    { id: 'au:check', group: 'Audio', title: 'Start mic check', icon: 'headphones', hint: 'R', keywords: 'mic check record listen test microphone', run: startCheck },
    { id: 'au:reset-tone', group: 'Audio', title: 'Reset tone to the preset', icon: 'undo', keywords: 'tone reset preset bass treble presence', run: () => act('audio.preset', { name: store.state.audio.chain.preset }) },
    { id: 'au:dn-rnnoise', group: 'Audio', title: 'Noise removal: RNNoise (lowest delay)', icon: 'wave', keywords: 'denoise noise removal rnnoise noise reduction latency', run: () => act('audio.denoise', { engine: 'rnnoise' }) },
    { id: 'au:dn-deepfilternet', group: 'Audio', title: 'Noise removal: DeepFilterNet (deep noise remover)', icon: 'wave', keywords: 'denoise noise removal deepfilternet deep noise remover', run: () => act('audio.denoise', { engine: 'deepfilternet' }) },
    { id: 'au:gain0', group: 'Audio', title: 'Mic gain to 0 dB', icon: 'sliders', keywords: 'gain reset zero microphone level', run: () => act('audio.gain', { db: 0 }) },
  ]));

  return () => off.forEach((fn) => fn());
}
