// pages/stream-adapt.js — "Bandwidth" card on the Stream page: automatic downshift when the uplink cannot carry the
// stream (docs/stream.md "Bandwidth adaptation").
//
// It shows the ladder of the plan chosen on the Presets page (step 0 is the plan, every later step sends less), which
// step is on air, why it stepped down, when it will try to recover, and a switch to turn the automation off. The
// person's chosen settings are never changed by it: the Picture card still shows what was chosen, this card says what
// is being sent instead. On the demo rig two buttons pretend the uplink loses packets, so the whole loop can be seen.
//
// Data: `state.stream.adapt` and `state.stream.effective`. Actions: `stream.set {adapt}` and (demo) `stream.simulate`.

import { h } from '../core/util.js';

const TEMPLATE = `
<ss-card class="st-card st-adapt" heading="Bandwidth" icon="wave" data-ref="card">
  <ss-toggle slot="actions" label="Adapt to the link" tone="green" data-ref="on"></ss-toggle>
  <p class="st-adapt-line" role="status" data-ref="line"></p>
  <p class="hint" data-ref="why"></p>
  <ol class="st-ladder" aria-label="Quality steps, best first" data-ref="ladder"></ol>
  <div class="st-label" data-ref="histLabel" hidden>What it did</div>
  <ul class="st-adapt-hist" aria-label="Recent shifts" data-ref="history"></ul>
  <div class="st-adapt-demo" data-ref="demo" hidden>
    <span class="st-adapt-demo-l">Demo rig</span>
    <ss-button size="sm" icon="alert" data-ref="loss">Simulate 12% packet loss</ss-button>
    <ss-button size="sm" variant="ghost" icon="check" data-ref="clean">Uplink is clean again</ss-button>
  </div>
</ss-card>`;

const secs = (s) => (s >= 90 ? `${Math.round(s / 60)} min` : `${Math.round(s)} s`);

export async function mountAdaptPanel(host, ctx) {
  const { store, act } = ctx;
  await ctx.ensureCss(new URL('./stream-adapt.css', import.meta.url).href);
  const frag = document.createElement('template');
  frag.innerHTML = TEMPLATE;
  const card = frag.content.firstElementChild;
  const anchor = host.querySelector('[data-ref="picCard"]');
  if (anchor) anchor.after(card); else host.append(card);
  const R = {};
  card.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  R.card = card;
  const S = () => store.state.stream;
  let pending = false;

  R.on.addEventListener('change', async (ev) => {
    const checked = ev.detail?.checked ?? R.on.checked;
    pending = true;
    await act('stream.set', { adapt: checked });
    pending = false;
    render();
  });
  R.loss.addEventListener('click', () => act('stream.simulate', { loss_percent: 12 }));
  R.clean.addEventListener('click', () => act('stream.simulate', { loss_percent: 0 }));

  function render() {
    const s = S();
    const a = s.adapt;
    if (!a) { card.hidden = true; return; }
    card.hidden = false;
    R.demo.hidden = s.probe?.ffmpeg !== 'demo';
    if (!pending) R.on.checked = !!a.enabled;
    R.on.toggleAttribute('disabled', !a.available);
    const streaming = s.destinations.some((d) => d.active && d.kind !== 'record');
    if (!a.available) {
      R.line.textContent = 'Not set up yet.';
      R.why.textContent = 'Choose a platform and a bandwidth tier on the Presets page. The stream then steps down by itself when the uplink cannot carry it, and back up when it can.';
      R.ladder.replaceChildren();
      R.histLabel.hidden = true;
      R.history.replaceChildren();
      card.setAttribute('tone', '');
      return;
    }
    const cur = a.current;
    card.setAttribute('tone', a.degraded && a.enabled ? 'warn' : '');
    if (!a.enabled) R.line.textContent = 'Off: the stream sends exactly what you chose, whatever the link does.';
    else if (a.degraded) R.line.textContent = `Sending ${cur.label}, step ${a.step + 1} of ${a.steps.length}. ${a.verdict?.reason ? `The link: ${a.verdict.reason}.` : ''}`.trim();
    else R.line.textContent = streaming ? `Sending your plan, ${cur.label}. The link looks fine.` : `Ready: your plan is ${cur.label}.`;
    if (!a.enabled) R.why.textContent = '';
    else if (a.degraded) R.why.textContent = streaming ? `It tries the next step up after ${secs(a.recover_in_s)} of a steady link.` : 'It starts again from your plan when the stream starts.';
    else R.why.textContent = 'If the link starts losing packets or the send queue grows, it steps down one rung at a time, and back up when the link is steady.';
    R.ladder.replaceChildren(...a.steps.map((step, i) => h('li', {
      'data-state': i === a.step ? 'current' : i < a.step ? 'above' : 'below',
      'aria-current': i === a.step ? 'step' : null,
    }, h('span', { class: 'st-ladder-n' }, String(i + 1)), h('span', { class: 'st-ladder-l' }, step.label), i === 0 ? h('span', { class: 'st-ladder-tag' }, 'your plan') : null)));
    R.histLabel.hidden = a.history.length === 0;
    R.history.replaceChildren(...[...a.history].reverse().slice(0, 5).map((e) => h('li', { 'data-dir': e.direction },
      h('span', { class: 'st-hist-dir' }, e.direction === 'down' ? '▼' : '▲'),
      h('span', {}, `${e.from} → ${e.to}`),
      h('span', { class: 'st-hist-why' }, e.reason))));
  }

  const off = store.subscribe((s) => s.stream.adapt, render);
  const off2 = store.subscribe((s) => [s.stream.destinations.map((d) => d.active), s.stream.probe?.ffmpeg], render);
  render();
  return () => { off(); off2(); card.remove(); };
}
