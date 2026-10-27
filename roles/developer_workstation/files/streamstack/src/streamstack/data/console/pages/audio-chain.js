// pages/audio-chain.js — THE VOICE CHAIN: every stage of StreamStack's own mic processing, live.
//
//   mountChain(root, ctx)   the Audio page's Chain section. Returns a cleanup function.
//
// Top: preset recall, the A/B bypass, reset to the preset, save as a preset, and the in / out / gain-reduction meters.
// Then the loudness card (the slow leveller to -14 / -16 / -23 LUFS) and one card per stage in signal order
// (high-pass, gate, neural denoise, de-esser, EQ, compressor, makeup gain, limiter), each with an on switch and a
// fader per parameter. Everything comes from `state.audio.chain.stages`, so a stage or parameter added on the server
// shows up here with its range, step and hint and no change to this file.
//
// The gain-reduction meter is an estimate: PipeWire does not expose a LADSPA plugin's output ports, so the server
// applies the compressor and limiter curves to the mic level (see docs/audio-chain.md).

import { h, renderList, clamp } from '../core/util.js';
import { selectPreset, toggleBypass } from '../core/ops.js';

const LEVELLER = {
  off: 'Off',
  riding: 'Riding the level',
  holding: 'On target',
  silence: 'Waiting for speech',
  muted: 'Frozen: mic muted',
  'no-signal': 'No signal',
  'at-limit': 'At the +/-12 dB limit',
  capped: 'Capped: the mic is already loud',
};
const GR_MAX_DB = 24;
const TEMPLATE = `
<div class="ch">
  <section class="st ch-top" aria-label="Voice chain" data-ref="topCard">
    <header class="st-head"><div class="st-id"><b class="st-name">Voice chain</b><small class="st-blurb">StreamStack's own mic processing, live</small></div>
      <span class="pill" data-ref="status" data-state="info"></span></header>
    <div class="ch-bar">
      <div class="ch-presets" role="group" aria-label="Chain presets" data-ref="presets"></div>
      <div class="ab ch-ab" role="group" aria-label="Chain A/B compare" data-ref="ab">
        <button type="button" data-bypass="false" aria-pressed="true"><span class="ab-k">A</span><span class="ab-t"><b>Processed</b><small>Through the chain</small></span></button>
        <button type="button" data-bypass="true" aria-pressed="false"><span class="ab-k">B</span><span class="ab-t"><b>Bypassed</b><small>Raw microphone</small></span></button>
      </div>
    </div>
    <div class="ch-tools">
      <span class="pill" data-state="info" data-ref="custom" hidden>Custom</span>
      <ss-button size="sm" variant="ghost" icon="undo" data-ref="reset" title="Back to the preset's own settings">Reset to preset</ss-button>
      <form class="ch-save" data-ref="saveForm">
        <input type="text" maxlength="48" placeholder="Save as preset" aria-label="Preset name" data-ref="saveName" autocomplete="off">
        <ss-button size="sm" icon="check" data-ref="save">Save</ss-button>
      </form>
    </div>
    <div class="ch-meters" role="group" aria-label="Chain meters">
      <ss-meter source="mic" label="In" orientation="horizontal" target="-24,-12" scale="none"></ss-meter>
      <ss-meter source="voice" label="Out" orientation="horizontal" target="-24,-12" scale="none"></ss-meter>
      <div class="gr" role="meter" aria-label="Gain reduction, estimated" aria-valuemin="0" aria-valuemax="${GR_MAX_DB}" aria-valuenow="0" data-ref="gr">
        <span class="gr-l">GR</span><span class="gr-bar"><i data-ref="grFill"></i></span><b class="gr-v" data-ref="grV">0.0 dB</b>
      </div>
    </div>
    <p class="hint ch-note" data-ref="note" role="status"></p>
  </section>

  <section class="st ch-loud" aria-label="Loudness" data-ref="loudCard">
    <header class="st-head"><div class="st-id"><b class="st-name">Loudness</b><small class="st-blurb">Slow leveller to a target, true peak under -1 dBTP</small></div></header>
    <div class="ld">
      <ss-toggle data-ref="ldOn" label="Auto level" sub="Slow gain riding to the target"></ss-toggle>
      <div class="seg" role="group" aria-label="Loudness target" data-ref="targets"></div>
      <dl class="ld-read">
        <div><dt>Short-term</dt><dd data-ref="ldLevel">&ndash;</dd></div>
        <div><dt>Gain</dt><dd data-ref="ldGain">0.0 dB</dd></div>
        <div><dt>Status</dt><dd data-ref="ldState">Off</dd></div>
      </dl>
    </div>
    <p class="hint">The leveller adds a slow gain after the chain (at most 0.5 dB a second near the target, within +/-12 dB), holds still in silence and while muted, and never touches the mic gain or phantom power.</p>
  </section>

  <div class="ch-stages" data-ref="stages" role="group" aria-label="Chain stages"></div>
</div>`;

const fmt = (v, d = 1) => (Number.isFinite(v) ? v.toFixed(d) : '–');

/** One stage card: header (name, hint, on switch) and a fader per parameter. */
function stageCard(onToggle, onFader, onFlush, onEngine) {
  const sw = h('ss-toggle', { class: 'st-on' });
  sw.addEventListener('change', (e) => onToggle(sw.dataset.stage, e.detail.checked));
  const card = h('section', { class: 'st', role: 'group' },
    h('header', { class: 'st-head' }, h('div', { class: 'st-id' }, h('b', { class: 'st-name' }), h('small', { class: 'st-blurb' })), sw),
    h('div', { class: 'st-engine ab', role: 'group', hidden: true }),
    h('div', { class: 'st-faders' }));
  card.querySelector('.st-engine').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-engine]');
    if (b && !b.disabled && b.getAttribute('aria-pressed') !== 'true') onEngine(b.dataset.engine);
  });
  card.faders = card.querySelector('.st-faders');
  card.onFader = onFader;
  card.onFlush = onFlush;
  return card;
}

function faderFor(card, key) {
  const f = h('ss-fader', { orientation: 'horizontal', ticks: 'none', 'data-key': key });
  f.addEventListener('input', (e) => card.onFader(key, e.detail.value));
  f.addEventListener('change', () => card.onFlush());
  return f;
}

export function mountChain(root, ctx) {
  const { store, act, actStream, flushStream, registerCommands } = ctx;
  root.innerHTML = TEMPLATE;
  const R = {};
  root.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  const off = [];
  const sub = (selector, cb) => off.push(store.subscribe(selector, cb));
  const chain = () => store.state.audio.chain;

  // ---------- top bar: presets, A/B, reset, save ----------
  R.presets.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-key]');
    if (b && !b.disabled) selectPreset(b.dataset.key);
  });
  R.ab.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-bypass]');
    if (b && !b.disabled && (b.dataset.bypass === 'true') !== chain().bypass) toggleBypass();
  });
  R.reset.addEventListener('click', () => act('audio.chain_reset'));
  R.saveForm.addEventListener('submit', (e) => e.preventDefault());
  const save = () => {
    const name = R.saveName.value.trim();
    if (!name) { R.saveName.focus(); return; }
    act('audio.preset_save', { name }).then((res) => { if (res) R.saveName.value = ''; });
  };
  R.save.addEventListener('click', save);
  R.saveName.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); save(); } });

  sub((s) => s.audio.chain, (c) => {
    R.ab.querySelectorAll('button').forEach((b) => {
      b.setAttribute('aria-pressed', String((b.dataset.bypass === 'true') === c.bypass));
      b.disabled = !c.available;
    });
    R.ab.dataset.bypass = String(c.bypass);
    R.custom.hidden = !c.custom;
    R.reset.toggleAttribute('disabled', !c.custom);
    R.topCard.toggleAttribute('data-warn', c.bypass);
    renderList(R.presets, c.presets, (p) => p.name, (p) => {
      const b = h('button', { type: 'button', class: 'ch-preset' });
      b.dataset.key = p.name;
      return b;
    }, (b, p) => {
      const active = p.name === c.preset;
      b.textContent = p.label.replace(/^Voice (?=\S)/, '');
      b.setAttribute('aria-pressed', String(active));
      b.disabled = !c.available || !!p.unavailable;
      b.title = p.unavailable || p.blurb || '';
    });
    const status = !c.available ? { text: 'Plugins missing', state: 'warn' }
      : !c.running ? { text: 'Not running', state: 'warn' }
      : c.bypass ? { text: 'Bypassed', state: 'warn' }
      : { text: `${c.description || 'StreamStack Mic'} · ${Math.round((c.rate || 48000) / 1000)} kHz`, state: 'ok' };
    R.status.textContent = status.text;
    R.status.dataset.state = status.state;
    R.note.textContent = c.error || (c.missing?.length ? `Missing: ${c.missing.join(', ')}. Install them and the chain starts.` : '');
    R.note.dataset.state = R.note.textContent ? 'warn' : '';
  });

  // ---------- gain reduction ----------
  let latest = null;
  off.push(store.onMeters((frame) => { latest = frame; }));
  const grTick = setInterval(() => {
    const gr = clamp(latest?.voice?.gr_db ?? 0, 0, GR_MAX_DB);
    R.grFill.style.width = `${(gr / GR_MAX_DB * 100).toFixed(1)}%`;
    R.grV.textContent = `${gr.toFixed(1)} dB`;
    R.gr.setAttribute('aria-valuenow', gr.toFixed(1));
  }, 100);
  off.push(() => clearInterval(grTick));

  // ---------- loudness ----------
  R.ldOn.addEventListener('change', (e) => act('audio.loudness', { on: e.detail.checked }));
  R.targets.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-target]');
    if (b) act('audio.loudness', { target_lufs: Number(b.dataset.target) });
  });
  sub((s) => s.audio.chain.loudness, (l) => {
    R.ldOn.checked = l.on;
    renderList(R.targets, l.targets, (t) => String(t), (t) => {
      const b = h('button', { type: 'button' });
      b.dataset.target = String(t);
      return b;
    }, (b, t) => {
      b.textContent = `${t} LUFS`;
      b.setAttribute('aria-pressed', String(t === l.target_lufs));
    });
    R.ldLevel.textContent = Number.isFinite(l.level_lufs) ? `${fmt(l.level_lufs)} LUFS` : '–';
    R.ldGain.textContent = `${l.auto_db > 0 ? '+' : ''}${fmt(l.auto_db)} dB`;
    R.ldState.textContent = LEVELLER[l.state] || l.state;
    R.loudCard.dataset.state = l.state;
  });

  // ---------- stages ----------
  const onToggle = (stage, on) => act('audio.chain', { params: { [`${stage}_on`]: on } });
  const onFader = (key, value) => actStream('audio.chain', { params: { [key]: value } });
  const onFlush = () => flushStream('audio.chain');
  const onEngine = (engine) => act('audio.denoise', { engine });
  sub((s) => s.audio.chain.stages, (stages) => {
    renderList(R.stages, stages, (st) => st.id, () => stageCard(onToggle, onFader, onFlush, onEngine), (card, st) => {
      card.dataset.stage = st.id;
      card.dataset.on = String(st.on);
      card.setAttribute('aria-label', st.label);
      card.querySelector('.st-name').textContent = st.label;
      card.querySelector('.st-blurb').textContent = st.blurb;
      const sw = card.querySelector('.st-on');
      sw.dataset.stage = st.id;
      sw.setAttribute('label', st.label);
      sw.checked = st.on;
      const wanted = new Set(st.params.map((p) => p.key));
      for (const f of [...card.faders.children]) if (!wanted.has(f.dataset.key)) f.remove();
      for (const p of st.params) {
        let f = card.faders.querySelector(`[data-key="${p.key}"]`);
        if (!f) { f = faderFor(card, p.key); card.faders.append(f); }
        f.setAttribute('label', p.label);
        f.setAttribute('unit', p.unit);
        f.setAttribute('min', p.low);
        f.setAttribute('max', p.high);
        f.setAttribute('step', p.step);
        f.setAttribute('default', p.default);
        f.toggleAttribute('signed', p.low < 0 && p.high > 0);
        if (p.hint) f.setAttribute('title', p.hint); else f.removeAttribute('title');
        f.toggleAttribute('disabled', !st.on);
        f.value = p.value;
      }
    });
  });
  // the neural denoise stage also chooses the engine
  const paintEngines = (dn) => {
    const card = R.stages.querySelector('[data-stage="denoise"]');
    if (!card) return;
    const box = card.querySelector('.st-engine');
    box.hidden = false;
    box.setAttribute('aria-label', 'Neural denoise engine');
    renderList(box, dn.engines, (en) => en.id, () => h('button', { type: 'button' },
      h('span', { class: 'ab-k' }), h('span', { class: 'ab-t' }, h('b'), h('small'))), (b, en) => {
      b.dataset.engine = en.id;
      b.querySelector('.ab-k').textContent = dn.engines.indexOf(en) === 0 ? 'A' : 'B';
      b.querySelector('b').textContent = en.label;
      b.querySelector('small').textContent = `about ${en.delay_ms} ms`;
      b.setAttribute('aria-pressed', String(en.id === dn.engine));
      b.setAttribute('aria-label', `${en.label} engine, about ${en.delay_ms} milliseconds of delay${en.available ? '' : '. Not installed'}`);
      b.disabled = !en.available;
      if (en.reason) b.title = en.reason; else b.removeAttribute('title');
    });
  };
  sub((s) => s.audio.denoise, paintEngines);
  sub((s) => s.audio.chain.stages, () => paintEngines(store.state.audio.denoise));

  off.push(registerCommands(() => [
    { id: 'ch:reset', group: 'Audio', title: 'Voice chain: reset to the preset', icon: 'undo', keywords: 'chain reset preset stages', run: () => act('audio.chain_reset') },
    { id: 'ch:level-on', group: 'Audio', title: 'Loudness leveller on', icon: 'pulse', keywords: 'loudness lufs auto level leveller on', run: () => act('audio.loudness', { on: true }) },
    { id: 'ch:level-off', group: 'Audio', title: 'Loudness leveller off', icon: 'pulse', keywords: 'loudness lufs auto level leveller off', run: () => act('audio.loudness', { on: false }) },
    ...[-14, -16, -23].map((t) => ({ id: `ch:lufs${t}`, group: 'Audio', title: `Loudness target ${t} LUFS`, icon: 'pulse', keywords: `loudness lufs target ${t}`, run: () => act('audio.loudness', { target_lufs: t }) })),
  ]));

  return () => off.forEach((fn) => fn());
}
