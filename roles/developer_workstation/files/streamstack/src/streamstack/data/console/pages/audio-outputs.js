// pages/audio-outputs.js — THE OUTPUT STRIPS and the compact output picker.
//
//   mountOutputs(root, ctx)        the Audio page's Output section: one mixer strip per output (volume, mute, default,
//                                  the sound test with a left/right lamp pair, and the heard / not heard verdict),
//                                  a summary bar with Test all, the default-output-lost warning and the routes.
//   mountOutputPicker(root, ctx)   the Live page's one-row version: an output select and a Test button.
//
// Both return a cleanup function. The server sends the test's elapsed seconds (and the phases of the sound) only when
// the state changes, so the lamps are driven by a local clock started from `elapsed` and run only while a test plays.
// A route (the StreamStack route sink) is not a device: it has no strip and no test.

import { h, renderList } from '../core/util.js';
import { icon } from '../components/icons.js';
import { relTime } from '../core/time.js';
import { testOutput, testAllOutputs, stopOutputTest, setDefaultOutput } from '../core/ops.js';

const KIND_ICON = { hdmi: 'monitor', headphones: 'headphones', headset: 'headset', speaker: 'speaker', interface: 'audio', other: 'volume' };
const BUS_LABEL = { usb: 'USB', bluetooth: 'Bluetooth', pci: 'Built in' };
const KIND_LABEL = { hdmi: 'HDMI', headphones: 'Headphones', headset: 'Headset', speaker: 'Speakers', interface: 'Audio interface', other: 'Output' };
const SAY = { left: 'Left channel', right: 'Right channel, two beeps', chime: 'Chime, both channels' };
const REFRESH_MS = 15000;

/** The verdict of an output's last test, as the words and the tone shown for it. */
function verdict(result) {
  if (!result) return { tone: 'none', text: 'Not tested yet' };
  const when = relTime(result.at);
  switch (result.result) {
    case 'heard': return { tone: 'ok', text: `Heard OK, ${when}` };
    case 'not-heard': return { tone: 'bad', text: `Not heard, ${when}` };
    case 'failed': return { tone: 'bad', text: `Test failed: ${result.error || 'unknown error'}` };
    default: return { tone: 'ask', text: 'Did you hear it?' };
  }
}

const hearable = (d) => !d.muted && d.volume !== 0;
const unhearableWhy = (d) => (d.muted ? 'Muted: unmute it to hear the test' : d.volume === 0 ? 'Volume is at zero: raise it to hear the test' : '');

/** Which phase of the sound is audible at `t` seconds, or '' between the sounds. */
function phaseAt(phases, t) {
  return phases.find((p) => t >= p.start && t < p.end)?.id || '';
}

function strip() {
  const el = h('section', { class: 'ao-strip', role: 'group' },
    h('header', { class: 'ao-head' },
      h('span', { class: 'ao-kind' }),
      h('div', { class: 'ao-id' }, h('b', { class: 'ao-name' }), h('small', { class: 'ao-detail' })),
      h('span', { class: 'pill ao-default', 'data-state': 'info' }, icon('check'), 'Default')),
    h('div', { class: 'ao-mix' },
      h('ss-fader', { class: 'ao-vol', label: 'Volume', unit: '%', min: '0', max: '100', step: '1', decimals: '0', ticks: '0,50,100', accent: 'green' }),
      h('div', { class: 'ao-stage' },
        h('div', { class: 'ao-lamps', 'aria-hidden': 'true' },
          h('span', { class: 'ao-lamp', 'data-ch': 'left' }, 'L'),
          h('span', { class: 'ao-lamp ao-chime', 'data-ch': 'chime' }, icon('wave')),
          h('span', { class: 'ao-lamp', 'data-ch': 'right' }, 'R')),
        h('div', { class: 'ao-prog', 'aria-hidden': 'true' }, h('i')),
        h('p', { class: 'ao-say', role: 'status', 'aria-live': 'polite' }),
        h('ss-button', { class: 'ao-mute', size: 'sm', icon: 'volume' }, 'Mute'))),
    h('div', { class: 'ao-act' },
      h('ss-button', { class: 'ao-test', variant: 'primary', icon: 'play' }, 'Test'),
      h('ss-button', { class: 'ao-setdef', variant: 'ghost', size: 'sm' }, 'Set as default')),
    h('div', { class: 'ao-verdict' },
      h('span', { class: 'ao-vtext' }),
      h('span', { class: 'ao-vbtns' },
        h('ss-button', { class: 'ao-heard', size: 'sm', icon: 'check' }, 'Heard'),
        h('ss-button', { class: 'ao-nope', size: 'sm', variant: 'danger', icon: 'alert' }, "Didn't hear"))));
  return el;
}

const OUTPUT_TEMPLATE = `
<div class="ao">
  <div class="ao-bar">
    <div class="ao-sum">
      <div class="ao-sum-t" data-ref="sumT"></div>
      <div class="ao-ready" data-ref="ready" role="img"></div>
      <div class="ao-sum-d hint" data-ref="sumD"></div>
    </div>
    <div class="ao-bar-act">
      <ss-button variant="danger" icon="stop" data-ref="stop" hidden>Stop test</ss-button>
      <ss-button variant="primary" icon="play" data-ref="testAll">Test all outputs</ss-button>
    </div>
  </div>
  <div class="ao-warn" role="alert" data-ref="warn" hidden>
    <span class="ao-warn-ic" data-ref="warnIc"></span>
    <p data-ref="warnText"></p>
    <ss-button size="sm" data-ref="dismiss">Dismiss</ss-button>
  </div>
  <div class="ao-strips" data-ref="strips" role="list" aria-label="Outputs"></div>
  <div class="ao-empty" data-ref="empty" hidden role="status"></div>
  <div class="ao-routes" data-ref="routes" role="group" aria-label="Routes"></div>
</div>`;

export function mountOutputs(root, ctx) {
  const { store, act, actStream, flushStream, registerCommands } = ctx;
  root.innerHTML = OUTPUT_TEMPLATE;
  const R = {};
  root.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  const off = [];
  const outputs = () => store.state.audio.outputs;

  R.warnIc.append(icon('alert'));
  R.testAll.addEventListener('click', () => testAllOutputs());
  R.stop.addEventListener('click', () => stopOutputTest());
  R.dismiss.addEventListener('click', () => act('audio.output_dismiss'));

  // ---------- the lamps: a local clock per playing output ----------
  const clocks = new Map();   // output name -> performance.now() at second 0 of its test
  let raf = 0;
  const paint = (el, name, now) => {
    const o = outputs();
    const t0 = clocks.get(name);
    const t = t0 === undefined ? null : (now - t0) / 1000;
    const phase = t === null ? '' : phaseAt(o.test.phases, t);
    if (el.dataset.phase !== phase) el.dataset.phase = phase;
    const frac = t === null ? 0 : Math.min(1, Math.max(0, t / (o.test.seconds || 1)));
    el.querySelector('.ao-prog i').style.setProperty('--p', frac.toFixed(3));
    const say = el.querySelector('.ao-say');
    const text = t === null ? '' : (SAY[phase] || 'Listen…');
    if (say.textContent !== text) say.textContent = text;
  };
  const frame = (now) => {
    raf = 0;
    for (const el of R.strips.children) if (el.dataset.playing === 'true') paint(el, el.dataset.key, now);
    if (clocks.size) raf = requestAnimationFrame(frame);
  };
  const syncClocks = (devices, test) => {
    const live = new Set(devices.filter((d) => d.playing).map((d) => d.name));
    for (const name of [...clocks.keys()]) if (!live.has(name)) clocks.delete(name);
    for (const name of live) if (!clocks.has(name)) clocks.set(name, performance.now() - (test.elapsed || 0) * 1000);
    if (clocks.size && !raf) raf = requestAnimationFrame(frame);
  };
  off.push(() => { cancelAnimationFrame(raf); raf = 0; });

  // ---------- strips ----------
  const wire = (el) => {
    const name = () => el.dataset.key;
    const vol = el.querySelector('.ao-vol');
    vol.addEventListener('input', (e) => actStream('audio.output_volume', { name: name(), percent: e.detail.value }));
    vol.addEventListener('change', () => flushStream('audio.output_volume'));
    el.querySelector('.ao-mute').addEventListener('click', () => {
      const d = outputs().devices.find((x) => x.name === name());
      if (d) act('audio.output_mute', { name: d.name, on: !d.muted });
    });
    el.querySelector('.ao-test').addEventListener('click', () => testOutput(name()));
    el.querySelector('.ao-setdef').addEventListener('click', () => setDefaultOutput(name()));
    el.querySelector('.ao-heard').addEventListener('click', () => act('audio.output_result', { name: name(), heard: true }));
    el.querySelector('.ao-nope').addEventListener('click', () => act('audio.output_result', { name: name(), heard: false }));
    return el;
  };
  const update = (el, d, test) => {
    const busy = test.state === 'playing';
    el.setAttribute('aria-label', d.label);
    el.dataset.default = String(d.default);
    el.dataset.playing = String(d.playing);
    el.dataset.muted = String(d.muted);
    el.querySelector('.ao-kind').replaceChildren(icon(KIND_ICON[d.kind] || 'volume'));
    el.querySelector('.ao-kind').title = KIND_LABEL[d.kind] || 'Output';
    el.querySelector('.ao-name').textContent = d.label;
    const bus = BUS_LABEL[d.bus] || '';
    el.querySelector('.ao-detail').textContent = [KIND_LABEL[d.kind], bus, d.detail].filter((x, i, a) => x && a.indexOf(x) === i).join(' · ');
    el.querySelector('.ao-default').hidden = !d.default;
    const vol = el.querySelector('.ao-vol');
    vol.toggleAttribute('disabled', d.volume === null);
    if (Number.isFinite(d.volume)) vol.value = d.volume;
    const mute = el.querySelector('.ao-mute');
    mute.toggleAttribute('pressed', d.muted);
    mute.setAttribute('icon', d.muted ? 'volume-off' : 'volume');
    mute.textContent = d.muted ? 'Muted' : 'Mute';
    mute.setAttribute('aria-label', `Mute ${d.label}`);
    const testBtn = el.querySelector('.ao-test');
    testBtn.toggleAttribute('busy', d.playing);
    testBtn.toggleAttribute('disabled', !d.playing && (busy || !hearable(d)));
    testBtn.textContent = d.playing ? 'Playing…' : 'Test';
    testBtn.setAttribute('aria-label', `Test ${d.label}`);
    testBtn.title = d.playing ? '' : (unhearableWhy(d) || 'Plays left, right, then a chime on this output only');
    const setDef = el.querySelector('.ao-setdef');
    setDef.toggleAttribute('disabled', d.default);
    setDef.textContent = d.default ? 'Default output' : 'Set as default';
    setDef.setAttribute('aria-label', d.default ? `${d.label} is the default output` : `Set ${d.label} as default`);
    const v = verdict(d.result);
    el.dataset.verdict = v.tone;
    el.querySelector('.ao-vtext').textContent = v.text;
    el.querySelector('.ao-heard').setAttribute('aria-label', `Heard the test on ${d.label}`);
    el.querySelector('.ao-nope').setAttribute('aria-label', `Did not hear the test on ${d.label}`);
    if (!d.playing) { el.querySelector('.ao-say').textContent = d.muted ? 'Muted' : ''; delete el.dataset.phase; }
  };

  const routeEl = (r) => {
    const b = h('ss-button', { size: 'sm', variant: 'ghost' }, 'Set as default');
    b.addEventListener('click', () => setDefaultOutput(b.closest('.ao-route').dataset.key));
    return h('div', { class: 'ao-route' }, h('span', { class: 'ao-kind' }, icon('wave')),
      h('div', { class: 'ao-id' }, h('b', { class: 'ao-name' }), h('small', { class: 'ao-detail' }, 'A route, not a device: sound sent here is processed, then plays on your outputs')),
      h('span', { class: 'pill ao-default', 'data-state': 'info' }, icon('check'), 'Default'), b);
  };

  off.push(store.subscribe((s) => s.audio.outputs, (o) => {
    syncClocks(o.devices, o.test);
    renderList(R.strips, o.devices, (d) => d.name, () => wire(strip()), (el, d) => update(el, d, o.test));
    for (const el of R.strips.children) el.setAttribute('role', 'listitem');
    renderList(R.routes, o.routes, (r) => r.name, routeEl, (el, r) => {
      el.dataset.default = String(r.default);
      el.querySelector('.ao-name').textContent = r.label;
      el.querySelector('.ao-default').hidden = !r.default;
      const b = el.querySelector('ss-button');
      b.toggleAttribute('disabled', r.default);
      b.setAttribute('aria-label', r.default ? `${r.label} is the default output` : `Set ${r.label} as default`);
    });
    R.routes.hidden = !o.routes.length;

    // summary
    const n = o.devices.length;
    const checked = o.devices.filter((d) => d.result?.result === 'heard').length;
    const playing = o.test.state === 'playing';
    R.sumT.textContent = n ? `${n} output${n === 1 ? '' : 's'}` : 'No outputs';
    R.sumD.textContent = o.test.all && playing
      ? `Testing ${o.test.index} of ${o.test.total}. Listen on each output in turn.`
      : n ? `${checked} of ${n} confirmed by ear${o.default_label ? `. Default: ${o.default_label}` : ''}` : '';
    R.ready.setAttribute('aria-label', `${checked} of ${n} outputs confirmed`);
    renderList(R.ready, o.devices, (d) => d.name, () => h('i'), (el, d) => { el.dataset.tone = verdict(d.result).tone; el.title = `${d.label}: ${verdict(d.result).text}`; });
    R.testAll.toggleAttribute('disabled', playing || !n);
    R.testAll.toggleAttribute('hidden', playing);
    R.stop.toggleAttribute('hidden', !playing);

    // the default output went away
    R.warn.hidden = !o.warning;
    R.warnText.textContent = o.warning?.message || '';

    // nothing to show
    R.empty.hidden = n > 0;
    R.empty.replaceChildren(h('b', {}, o.problem || 'No output devices found.'), h('span', { class: 'hint' }, 'Plug in a speaker or a headset, it will appear here.'));
  }));

  // "3 min ago" goes stale without a state change
  const refresh = setInterval(() => {
    const o = outputs();
    for (const el of R.strips.children) {
      const d = o.devices.find((x) => x.name === el.dataset.key);
      if (d) el.querySelector('.ao-vtext').textContent = verdict(d.result).text;
    }
  }, REFRESH_MS);
  off.push(() => clearInterval(refresh));

  off.push(registerCommands(() => {
    const o = outputs();
    return [
      { id: 'ao:test-all', group: 'Audio', title: 'Test all outputs', icon: 'play', keywords: 'output speakers headphones sound check test all', run: () => testAllOutputs() },
      { id: 'ao:test-default', group: 'Audio', title: 'Test the default output', icon: 'play', keywords: 'output speakers headphones sound check test default', run: () => { if (o.default) testOutput(o.default); } },
      ...o.devices.filter((d) => !d.default).map((d) => ({ id: `ao:default:${d.name}`, group: 'Audio', title: `Set default output: ${d.label}`, icon: KIND_ICON[d.kind] || 'volume', keywords: 'output speakers headphones default sink device', run: () => setDefaultOutput(d.name) })),
    ];
  }));
  return () => off.forEach((fn) => fn());
}

/** The Output tab's badge: a warning wins, then the number of tested outputs still waiting for a verdict. */
export function outputBadge(o) {
  if (o.warning) return { text: '!', tone: 'warn', label: 'Default output lost' };
  const waiting = o.devices.filter((d) => d.result?.result === 'pending').length;
  return waiting ? { text: String(waiting), tone: 'info', label: `${waiting} to confirm` } : null;
}

export function mountOutputPicker(root, ctx) {
  const { store } = ctx;
  const sel = h('select', { class: 'text-input op-sel', 'aria-label': 'Output device' });
  const dot = h('span', { class: 'op-dot', role: 'img' });
  const btn = h('ss-button', { class: 'op-test', size: 'sm', icon: 'play', 'aria-label': 'Test the selected output' }, 'Test');
  root.classList.add('op');
  root.replaceChildren(h('span', { class: 'op-ic' }, icon('volume')), sel, dot, btn);

  sel.addEventListener('change', () => setDefaultOutput(sel.value));
  btn.addEventListener('click', () => { if (sel.value) testOutput(sel.value); });

  const off = [];
  off.push(store.subscribe((s) => s.audio.outputs, (o) => {
    const entries = [...o.devices, ...o.routes.map((r) => ({ ...r, route: true }))];
    renderList(sel, entries, (e) => e.name, (e) => h('option', { value: e.name }), (opt, e) => {
      opt.textContent = e.route ? `${e.label} (route)` : e.label;
    });
    if (o.default) sel.value = o.default;
    sel.disabled = !entries.length;
    const d = o.devices.find((x) => x.name === o.default);
    const playing = o.test.state === 'playing';
    btn.toggleAttribute('busy', !!d?.playing);
    btn.toggleAttribute('disabled', !d || playing || !hearable(d));
    btn.textContent = d?.playing ? 'Playing…' : 'Test';
    btn.title = d ? (d.playing ? '' : unhearableWhy(d) || 'Plays left, right, then a chime on the default output') : 'The route has no test: pick a device';
    const v = verdict(d?.result);
    dot.dataset.tone = d ? v.tone : 'none';
    dot.setAttribute('aria-label', d ? v.text : 'No output selected');
    dot.title = d ? v.text : '';
    root.dataset.state = o.warning ? 'warn' : '';
    root.title = o.warning ? o.warning.message : '';
    root.hidden = !entries.length && !o.problem;
  }));
  return () => off.forEach((fn) => fn());
}
