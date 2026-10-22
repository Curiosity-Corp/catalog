// pages/system.js — HEALTH, SERVICES, TOOLS AND CALL SETUP.
//
// A health hero with Re-check; the checks as cards (Video, Audio, Apps) each with the one-line fix hint;
// big service tiles (Studio start/stop, voice chain, Canon bridge, Auto); a tools grid that opens the
// real apps; and a Teams / Zoom checklist with a copy button per row (mirrors `streamstack call-settings`).
//
// Keys on this page: K re-check. Stopping the studio is only ever a press-and-hold.

import { h, renderList } from '../core/util.js';
import { icon } from '../components/icons.js';
import { startStudio, stopStudio } from '../core/ops.js';
import { relTime } from '../core/time.js';

const GROUPS = [
  { id: 'video', title: 'Video', icon: 'camera' },
  { id: 'audio', title: 'Audio', icon: 'audio' },
  { id: 'apps', title: 'Apps', icon: 'system' },
];
const STATE = {
  ok: { icon: 'check', word: 'Good' },
  warn: { icon: 'alert', word: 'Check' },
  fail: { icon: 'x', word: 'Fix' },
};
const TOOLS = [
  { name: 'obs', title: 'OBS Studio', blurb: 'Scenes, filters, recording', icon: 'scenes' },
  { name: 'easyeffects', title: 'Easy Effects', blurb: 'The voice chain', icon: 'wave' },
  { name: 'qpwgraph', title: 'qpwgraph', blurb: 'Audio patchbay', icon: 'patch' },
  { name: 'pavucontrol', title: 'pavucontrol', blurb: 'Volume and devices', icon: 'speaker' },
  { name: 'qv4l2', title: 'qv4l2', blurb: 'Camera controls', icon: 'camera' },
];

const MIC_NAME = 'Easy Effects Source';
const callSteps = (cam) => ({
  teams: {
    label: 'Microsoft Teams',
    intro: 'Start the studio first: browsers only list the loopback camera while it has a picture.',
    where: 'Settings > Devices',
    rows: [
      { k: 'Camera', v: cam, copy: cam },
      { k: 'Microphone', v: MIC_NAME, copy: MIC_NAME },
      { k: 'Speaker', v: 'Headphones' },
      { k: 'Noise suppression', v: 'Low', note: 'Easy Effects already removes noise; High or Auto fights it.' },
      { k: 'Background effects', v: 'None, no blur', note: 'Do framing and grading here, not in Teams.' },
    ],
    test: 'Teams: "Make a test call"',
  },
  zoom: {
    label: 'Zoom',
    intro: 'Desktop client or the web client both work.',
    where: 'Settings > Video and Settings > Audio',
    rows: [
      { k: 'Camera', v: cam, copy: cam },
      { k: 'Microphone', v: MIC_NAME, copy: MIC_NAME },
      { k: 'HD', v: 'On' },
      { k: 'Mirror my video', v: 'Off when text or signs are in shot' },
      { k: 'Adjust for low light, Touch up appearance', v: 'Off', note: 'OBS handles it.' },
      { k: 'Automatically adjust microphone volume', v: 'Off', note: 'StreamStack auto mode levels the mic.' },
      { k: 'Suppress background noise', v: 'Low' },
      { k: 'Original sound for musicians', v: 'On, with echo cancellation', note: 'Echo cancellation can stay off if you wear headphones.' },
    ],
    test: 'Zoom: zoom.us/test',
  },
});

const TEMPLATE = `
<div class="sys">
  <section class="hero" data-ref="hero" data-state="ok" aria-labelledby="heroTitle">
    <div class="hero-badge" data-ref="heroIc" aria-hidden="true"></div>
    <div class="hero-text">
      <h1 id="heroTitle" data-ref="heroTitle">Checking…</h1>
      <p class="hero-sub" data-ref="heroSub" role="status"></p>
    </div>
    <ss-button icon="refresh" data-ref="recheck" title="Run every check again (K)">Re-check</ss-button>
    <dl class="stats" data-ref="stats">
      <div class="stat stat-cpu"><dt>CPU</dt><dd><b data-ref="cpuV">–</b><canvas data-ref="spark" width="120" height="36" role="img" aria-label="CPU use over the last minute"></canvas></dd></div>
      <div class="stat"><dt>Load</dt><dd data-ref="loadV">–</dd></div>
      <div class="stat"><dt>Cores</dt><dd data-ref="coresV">–</dd></div>
      <div class="stat"><dt>Version</dt><dd data-ref="verV">–</dd></div>
    </dl>
  </section>

  <section aria-labelledby="hServices">
    <h2 class="sec" id="hServices">Services</h2>
    <div class="tiles" data-ref="services"></div>
  </section>

  <section aria-labelledby="hChecks">
    <h2 class="sec" id="hChecks">Checks <span class="sec-note" data-ref="checkNote"></span></h2>
    <div class="check-cols" data-ref="checks"></div>
  </section>

  <div class="two">
    <section aria-labelledby="hTools">
      <h2 class="sec" id="hTools">Tools</h2>
      <div class="tools" data-ref="tools"></div>
    </section>
    <ss-card class="call" heading="Call setup" icon="headphones">
      <div slot="actions" class="seg" role="group" aria-label="Which call app" data-ref="callSeg">
        <button type="button" data-app="teams" aria-pressed="true">Teams</button>
        <button type="button" data-app="zoom" aria-pressed="false">Zoom</button>
      </div>
      <p class="hint" data-ref="callIntro"></p>
      <p class="where" data-ref="callWhere"></p>
      <ul class="steps" data-ref="steps"></ul>
      <div class="both">
        <h3>Both</h3>
        <ul>
          <li>Do a test call first. <span data-ref="callTest"></span></li>
          <li>Headphones stop echo. Speak at arm's length with the mic just off-axis.</li>
          <li>Run <code>streamstack mic-check</code>, or use Mic check on the Audio page, to hear your processed voice before anyone else does.</li>
        </ul>
      </div>
    </ss-card>
  </div>
</div>`;

async function copyText(text) {
  if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return; }
  const ta = h('textarea', { 'aria-hidden': 'true', readonly: '' });
  ta.value = text;
  document.body.append(ta);
  ta.select();
  const ok = document.execCommand('copy');
  ta.remove();
  if (!ok) throw new Error('copy command was refused');
}

export async function mount(host, ctx) {
  const { store, act, toast, navigate, registerKeys, registerCommands } = ctx;
  await ctx.ensureCss(new URL('./common.css', import.meta.url).href);
  await ctx.ensureCss(new URL('./system.css', import.meta.url).href);
  document.title = 'System · StreamStack';
  host.innerHTML = TEMPLATE;
  const R = {};
  host.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  const off = [];
  const sub = (selector, cb) => off.push(store.subscribe(selector, cb));
  let callApp = 'teams';

  // ---------- health hero ----------
  const recheck = () => act('health.refresh');
  R.recheck.addEventListener('click', recheck);
  sub((s) => s.health, (hl) => {
    const bad = hl.checks.filter((c) => c.state === 'fail').length;
    const warn = hl.checks.filter((c) => c.state === 'warn').length;
    const state = hl.summary || (bad ? 'fail' : warn ? 'warn' : 'ok');
    R.hero.dataset.state = hl.checks.length ? state : 'info';
    R.heroIc.replaceChildren(icon(!hl.checks.length ? 'loader' : state === 'ok' ? 'check' : state === 'warn' ? 'alert' : 'x'));
    R.heroTitle.textContent = !hl.checks.length ? 'Checking…' : state === 'ok' ? 'Everything is ready' : state === 'warn' ? `${warn} thing${warn === 1 ? '' : 's'} to check` : `${bad} thing${bad === 1 ? '' : 's'} to fix`;
    const parts = [];
    if (bad && state !== 'ok') parts.push(`${bad} to fix`);
    if (warn) parts.push(`${warn} to check`);
    R.heroSub.textContent = hl.checks.length ? `${parts.length ? `${parts.join(', ')}. ` : 'All checks passed. '}${hl.updated ? `Checked ${relTime(hl.updated)}.` : ''}` : 'Running the first checks.';
    R.checkNote.textContent = hl.checks.length ? `${hl.checks.length - bad - warn} of ${hl.checks.length} good` : '';

    R.checks.replaceChildren(...GROUPS.map((gr) => {
      const items = hl.checks.filter((c) => c.group === gr.id);
      const worst = items.some((c) => c.state === 'fail') ? 'fail' : items.some((c) => c.state === 'warn') ? 'warn' : 'ok';
      return h('section', { class: 'cgrp', 'aria-label': gr.title },
        h('header', { class: 'cgrp-head' }, icon(gr.icon), h('b', {}, gr.title), h('span', { class: 'pill', 'data-state': worst }, worst === 'ok' ? 'All good' : worst === 'warn' ? 'Check' : 'Fix')),
        h('ul', { class: 'cards' }, items.map((c) => {
          const st = STATE[c.state] || STATE.warn;
          return h('li', { class: 'ck', 'data-state': c.state },
            h('span', { class: 'ck-ic', title: st.word }, icon(st.icon)),
            h('div', { class: 'ck-body' },
              h('div', { class: 'ck-title' }, h('span', { class: 'sr-only' }, `${st.word}: `), c.title),
              c.detail ? h('div', { class: 'ck-detail' }, c.detail) : null,
              c.hint && c.state !== 'ok' ? h('div', { class: 'ck-hint' }, icon('info'), c.hint) : null));
        })),
        items.length ? null : h('p', { class: 'hint' }, 'Nothing reported yet.'));
    }));
  });

  // ---------- stats + sparkline ----------
  const cpu = [];
  const g = R.spark.getContext('2d');
  const drawSpark = () => {
    const cs = getComputedStyle(R.spark);
    const color = cs.getPropertyValue('--accent').trim();
    const w = R.spark.width;
    const hh = R.spark.height;
    g.clearRect(0, 0, w, hh);
    R.spark.hidden = cpu.length < 2;
    if (cpu.length < 2) return;
    g.beginPath();
    cpu.forEach((v, i) => { const x = (i / 29) * w; const y = hh - 2 - (Math.min(100, v) / 100) * (hh - 4); if (i === 0) g.moveTo(x, y); else g.lineTo(x, y); });
    g.strokeStyle = color; g.lineWidth = 2; g.lineJoin = 'round'; g.stroke();
  };
  sub((s) => s.system, (sys) => {
    if (Number.isFinite(sys.cpu_percent)) { cpu.push(sys.cpu_percent); if (cpu.length > 30) cpu.shift(); }
    R.cpuV.textContent = Number.isFinite(sys.cpu_percent) ? `${Math.round(sys.cpu_percent)}%` : '–';
    R.stats.querySelector('.stat-cpu').dataset.level = sys.cpu_percent >= 90 ? 'fail' : sys.cpu_percent >= 75 ? 'warn' : 'ok';
    R.loadV.textContent = Array.isArray(sys.load) ? sys.load.map((v) => v.toFixed(2)).join('  ') : '–';
    R.coresV.textContent = sys.cores ?? '–';
    drawSpark();
  });
  window.addEventListener('ss:theme', drawSpark);
  off.push(() => window.removeEventListener('ss:theme', drawSpark));
  sub((s) => s.version, (v) => { R.verV.textContent = v || '–'; });

  // ---------- services ----------
  const tile = (id) => {
    const el = h('div', { class: 'tile', role: 'group' },
      h('div', { class: 'tile-head' }, h('span', { class: 'tile-dot', 'aria-hidden': 'true' }), h('b', { class: 'tile-name' })),
      h('div', { class: 'tile-state' }), h('p', { class: 'tile-note' }), h('div', { class: 'tile-act' }));
    el.dataset.id = id;
    return el;
  };
  const SERVICES = ['studio', 'voice', 'bridge', 'auto'];
  for (const id of SERVICES) R.services.append(tile(id));
  const svc = (id) => R.services.querySelector(`[data-id="${id}"]`);
  const setTile = (id, { name, on, state, note, busy, action }) => {
    const el = svc(id);
    el.querySelector('.tile-name').textContent = name;
    el.dataset.on = String(on);
    el.dataset.busy = String(!!busy);
    el.setAttribute('aria-label', `${name}: ${state}`);
    el.querySelector('.tile-state').textContent = state;
    el.querySelector('.tile-note').textContent = note;
    const slot = el.querySelector('.tile-act');
    let btn = slot.firstElementChild;
    if (!action) { slot.replaceChildren(); return; }
    if (!btn || btn.dataset.kind !== action.kind) {
      btn = h('ss-button', { 'data-kind': action.kind, size: 'md', block: true });
      btn.addEventListener('click', () => btn._run?.());
      slot.replaceChildren(btn);
    }
    btn._run = action.run;
    btn.setAttribute('variant', action.variant || 'default');
    btn.setAttribute('icon', action.icon);
    btn.setAttribute('hold', action.hold ? String(action.hold) : '0');
    btn.toggleAttribute('busy', !!busy);
    btn.toggleAttribute('disabled', !!busy);
    btn.textContent = action.label;
    btn.setAttribute('title', action.hold ? `Press and hold to ${action.label.toLowerCase()}` : action.label);
  };
  sub((s) => [s.studio, s.auto.running, s.auto.busy], ([st, autoOn, autoBusy]) => {
    setTile('studio', {
      name: 'Studio', on: st.running, busy: st.busy,
      state: st.busy ? (st.running ? 'Stopping…' : 'Starting…') : st.running ? 'Running' : 'Stopped',
      note: st.running ? `Program camera "${st.program_label || 'StreamStack Camera'}" is ready for calls.` : 'OBS, the camera bridge and the voice chain are off.',
      action: st.running ? { kind: 'stop', label: 'Stop studio', icon: 'stop', variant: 'danger', hold: 900, run: stopStudio } : { kind: 'start', label: 'Start studio', icon: 'power', variant: 'primary', run: startStudio },
    });
    setTile('voice', {
      name: 'Voice chain', on: !!st.voice_chain, state: st.voice_chain ? 'Running' : 'Off',
      note: st.voice_chain ? `Apps hear "${MIC_NAME}".` : 'Calls would hear the raw microphone.',
      action: { kind: 'go-audio', label: 'Open Audio', icon: 'audio', run: () => navigate('audio') },
    });
    setTile('bridge', {
      name: `${st.camera_label || 'Camera'} bridge`, on: !!st.bridge, state: st.bridge ? 'Running' : 'Off',
      note: st.bridge ? 'The camera feeds OBS through a loopback device.' : 'Starts with the studio.',
      action: { kind: 'go-camera', label: 'Open Camera', icon: 'camera', run: () => navigate('camera') },
    });
    setTile('auto', {
      name: 'AI director', on: autoOn, busy: false, state: autoBusy ? 'Looking…' : autoOn ? 'On' : 'Off',
      note: autoOn ? 'Watching picture and voice, adjusting gently.' : 'Nothing changes by itself.',
      action: { kind: 'go-auto', label: 'Open Auto', icon: 'auto', run: () => navigate('auto') },
    });
  });

  // ---------- tools ----------
  for (const t of TOOLS) {
    const b = h('button', { type: 'button', class: 'app', 'data-name': t.name, 'aria-label': `Open ${t.title}. ${t.blurb}` },
      h('span', { class: 'app-ic' }, icon(t.icon)), h('span', { class: 'app-t' }, h('b', {}, t.title), h('small', {}, t.blurb)), h('span', { class: 'app-go', 'aria-hidden': 'true' }, icon('external')));
    b.addEventListener('click', async () => {
      b.dataset.busy = 'true';
      try { await act('open.app', { name: t.name }); } finally { setTimeout(() => { b.dataset.busy = 'false'; }, 1200); }
    });
    R.tools.append(b);
  }
  sub((s) => s.health.checks, (checks) => {
    const missing = (name) => checks.find((c) => c.id === `app-${name}` && c.state !== 'ok');
    for (const b of R.tools.children) {
      const m = missing(b.dataset.name);
      b.toggleAttribute('data-missing', !!m);
      b.title = m ? `${m.title}: ${m.detail || 'not installed'}` : '';
    }
  });

  // ---------- call setup ----------
  const renderCall = () => {
    const cam = store.state.studio.program_label || 'StreamStack Camera';
    const c = callSteps(cam)[callApp];
    for (const b of R.callSeg.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.app === callApp));
    R.callIntro.textContent = c.intro;
    R.callWhere.textContent = c.where;
    R.callTest.textContent = c.test + '.';
    R.steps.replaceChildren(...c.rows.map((r) => {
      const li = h('li', { class: 'step' },
        h('span', { class: 'step-k' }, r.k),
        h('span', { class: 'step-v' }, h('b', {}, r.v), r.note ? h('small', {}, r.note) : null));
      if (r.copy) {
        const btn = h('button', { type: 'button', class: 'copy', 'aria-label': `Copy ${r.copy}`, title: `Copy "${r.copy}"` }, icon('copy'), h('span', {}, 'Copy'));
        btn.addEventListener('click', async () => {
          try {
            await copyText(r.copy);
            btn.dataset.done = 'true';
            btn.querySelector('span').textContent = 'Copied';
            setTimeout(() => { btn.dataset.done = 'false'; btn.querySelector('span').textContent = 'Copy'; }, 1500);
          } catch (err) {
            toast(`Could not copy: ${err.message}`, { level: 'error' });
          }
        });
        li.append(btn);
      }
      return li;
    }));
  };
  R.callSeg.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-app]');
    if (b) { callApp = b.dataset.app; renderCall(); }
  });
  sub((s) => s.studio.program_label, renderCall);

  off.push(registerKeys([{ key: 'k', desc: 'Re-check health', group: 'System', run: recheck }]));
  off.push(registerCommands(() => [
    { id: 'sys:recheck', group: 'System', title: 'Re-check health', icon: 'refresh', hint: 'K', keywords: 'health check refresh doctor system', run: recheck },
    ...TOOLS.map((t) => ({ id: `sys:open-${t.name}`, group: 'System', title: `Open ${t.title}`, icon: t.icon, keywords: `open launch app tool ${t.name} ${t.blurb}`, run: () => act('open.app', { name: t.name }) })),
    { id: 'sys:teams', group: 'System', title: 'Call setup: Teams', icon: 'headphones', keywords: 'teams call settings checklist', run: () => { callApp = 'teams'; renderCall(); navigate('system'); } },
    { id: 'sys:zoom', group: 'System', title: 'Call setup: Zoom', icon: 'headphones', keywords: 'zoom call settings checklist', run: () => { callApp = 'zoom'; renderCall(); navigate('system'); } },
  ]));

  return () => off.forEach((fn) => fn());
}
