// pages/preflight.js — the pre-flight check, as a dialog on the Live page.
//
// state.preflight: idle | running | done, the phase while running ("listening" while the person says their name),
// the last result (score, verdict, one row per check with the reason and at most one Fix) and the last runs.
// The countdown is a local clock started when the run is first seen: the server only says when the state changes.

import { h, renderList } from '../core/util.js';
import { icon } from '../components/icons.js';
import { relTime } from '../core/time.js';
import { onUi, ui } from '../core/ui.js';

const VERDICT = {
  ready: { word: 'Ready', tone: 'ok' },
  check: { word: 'Check a few things', tone: 'warn' },
  'not-ready': { word: 'Not ready', tone: 'bad' },
};
const STATE_WORD = { ok: 'OK', warn: 'Check', fail: 'Problem', skip: 'Skipped' };

const TEMPLATE = `
<dialog class="pf" aria-labelledby="pfTitle" data-ref="dlg">
  <header class="pf-head">
    <h2 id="pfTitle">Pre-flight check</h2>
    <ss-button size="sm" variant="ghost" icon="x" data-ref="close" aria-label="Close pre-flight"></ss-button>
  </header>
  <div class="pf-body">
    <p class="pf-intro" data-ref="intro">Checks the camera, microphone, outputs, lights and network in about twenty seconds. You will be asked to say your name once.</p>
    <div class="pf-listen" data-ref="listen" hidden>
      <strong class="pf-say" data-ref="say" role="status"></strong>
      <div class="pf-bar" aria-hidden="true"><i data-ref="barFill"></i></div>
    </div>
    <div class="pf-score" data-ref="scoreBox" hidden>
      <strong class="pf-num" data-ref="score" role="status"></strong>
      <span class="pf-verdict" data-ref="verdict"></span>
      <span class="pf-when" data-ref="when"></span>
    </div>
    <ul class="pf-checks" data-ref="checks" aria-label="Pre-flight results"></ul>
    <div class="pf-hist" data-ref="hist" hidden>
      <h3>Recent runs</h3>
      <ol data-ref="histList"></ol>
    </div>
  </div>
  <footer class="pf-foot">
    <ss-button size="sm" variant="ghost" data-ref="quick">Quick check, no voice</ss-button>
    <ss-button size="sm" variant="primary" icon="pulse" data-ref="run">Run pre-flight</ss-button>
  </footer>
</dialog>`;

export function mountPreflight(host, ctx) {
  const { store, act } = ctx;
  const frag = document.createElement('template');
  frag.innerHTML = TEMPLATE;
  const dlg = frag.content.firstElementChild;
  host.append(dlg);
  const R = {};
  dlg.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  let runStart = 0;
  let timer = 0;

  const open = () => { if (!dlg.open) dlg.showModal(); };
  const close = () => { if (dlg.open) dlg.close(); };
  R.close.addEventListener('click', close);
  dlg.addEventListener('click', (e) => { if (e.target === dlg) close(); });
  R.run.addEventListener('click', () => act('preflight.run', { listen: true }));
  R.quick.addEventListener('click', () => act('preflight.run', { listen: false }));

  const tick = () => {
    const p = store.state.preflight;
    const left = Math.max(0, p.listen_s - (performance.now() - runStart) / 1000);
    R.barFill.style.transform = `scaleX(${p.listen_s ? left / p.listen_s : 0})`;
    R.say.textContent = left > 0 ? `${p.prompt}. ${Math.ceil(left)} s` : 'Thanks. Checking the rest';
  };
  const stopTimer = () => { if (timer) { clearInterval(timer); timer = 0; } };

  const renderCheck = (li, c) => {
    li.dataset.state = c.state;
    li.replaceChildren(
      h('span', { class: 'pf-dot', 'aria-hidden': 'true' }),
      h('span', { class: 'pf-text' }, h('strong', {}, c.title), h('span', { class: 'pf-detail' }, c.detail)),
      h('span', { class: 'pf-state' }, STATE_WORD[c.state] || c.state),
    );
    if (c.fix) {
      const b = h('ss-button', { size: 'sm', 'aria-label': `Fix ${c.title}: ${c.fix.label}` }, c.fix.label);
      b.addEventListener('click', () => act('preflight.fix', { check: c.id }));
      li.append(b);
    }
  };

  const render = (p) => {
    const running = p.state === 'running';
    const listening = running && p.phase === 'listening';
    if (running && !runStart) runStart = performance.now();
    if (!running) runStart = 0;
    R.listen.hidden = !listening;
    if (listening && !timer) { tick(); timer = setInterval(tick, 200); }
    if (!listening) stopTimer();
    R.intro.hidden = running || !!p.result;
    R.run.toggleAttribute('busy', running);
    R.run.toggleAttribute('disabled', running);
    R.quick.toggleAttribute('disabled', running);
    R.run.textContent = p.result ? 'Run again' : 'Run pre-flight';
    const r = p.result;
    R.scoreBox.hidden = !r;
    if (r) {
      const v = VERDICT[r.verdict] || { word: r.verdict, tone: 'warn' };
      R.scoreBox.dataset.tone = v.tone;
      R.score.textContent = `${r.score} / 100`;
      R.verdict.textContent = v.word;
      R.when.textContent = relTime(r.at);
    }
    renderList(R.checks, r ? r.checks : [], (c) => c.id, () => h('li', { class: 'pf-check' }), renderCheck);
    R.checks.classList.toggle('stale', running && !!r);
    const hist = p.history.slice(0, 8);
    R.hist.hidden = hist.length < 2;
    R.histList.replaceChildren(...hist.map((x) => h('li', { 'data-tone': (VERDICT[x.verdict] || {}).tone }, h('b', {}, String(x.score)), h('span', {}, relTime(x.at)))));
  };

  const offStore = store.subscribe((s) => s.preflight, render);
  const offUi = onUi((key) => { if (key === 'preflightOpen') open(); });
  if (ui.preflightOpen) open();
  return () => { offStore(); offUi(); stopTimer(); dlg.remove(); };
}

/** Palette entries: open the dialog and start a run (never while one is already running). */
export function preflightCommands(ctx, openDialog) {
  const { store, act } = ctx;
  const p = store.state.preflight;
  if (!p.available) return [];
  return [
    { id: 'Studio:Pre-flight check', group: 'Studio', title: 'Run the pre-flight check', icon: 'pulse', keywords: 'preflight pre-flight check ready meeting test',
      run: () => { openDialog(); if (p.state !== 'running') act('preflight.run', { listen: true }); } },
  ];
}
