// pages/system-network.js — the network quality card on the System page.
//
// state.network says whether the connection is good enough for a call (good, degraded, poor, offline), the round trip,
// jitter and loss to the call services (Teams, Meet, Zoom by default), the likely cause, a five minute sparkline, the
// gateway and the Wi-Fi link. The bandwidth test is an explicit button (it uses the uplink for a few seconds, so during
// a call it asks first). Targets are editable. The warning itself arrives as an ordinary toast from the server.

import { h, renderList } from '../core/util.js';
import { relTime } from '../core/time.js';
import { confirmThen } from '../core/ops.js';

const RATING = {
  unknown: { word: 'Measuring', tone: 'none' },
  good: { word: 'Good', tone: 'ok' },
  degraded: { word: 'Degraded', tone: 'warn' },
  poor: { word: 'Poor', tone: 'bad' },
  offline: { word: 'Offline', tone: 'bad' },
};
const VERDICT = { enough: 'Plenty for a 1080p call', tight: 'Just enough for a 1080p call', short: 'Not enough for a 1080p call', unknown: '' };
const SPARK_W = 300;
const SPARK_H = 48;

const num = (v, unit, digits = 0) => (Number.isFinite(v) ? `${v.toFixed(digits)} ${unit}` : '–');

/** The sparkline's path: round trips as a line, gaps (lost or offline) as breaks. Exported for the unit of the badge. */
export function sparkPath(history, w = SPARK_W, hgt = SPARK_H) {
  const known = history.filter((v) => Number.isFinite(v));
  if (!known.length) return '';
  const top = Math.max(100, ...known);
  const step = history.length > 1 ? w / (history.length - 1) : w;
  let d = '';
  let pen = false;
  history.forEach((v, i) => {
    if (!Number.isFinite(v)) { pen = false; return; }
    const x = (i * step).toFixed(1);
    const y = (hgt - 3 - (v / top) * (hgt - 6)).toFixed(1);
    d += `${pen ? 'L' : 'M'}${x} ${y}`;
    pen = true;
  });
  return d;
}

const TEMPLATE = `
<section class="net" aria-labelledby="hNet">
  <h2 class="sec" id="hNet">Network <span class="sec-note">Round trip, jitter and loss to the call services; a few kB a minute</span></h2>
  <div class="net-card">
    <div class="net-top">
      <div class="net-state">
        <span class="net-pill" data-ref="pill" role="status"></span>
        <span class="net-text" data-ref="text"></span>
      </div>
      <button type="button" class="net-switch" role="switch" aria-checked="true" aria-label="Monitor the network" data-ref="enabled"><span class="net-knob"></span></button>
    </div>
    <p class="net-cause" data-ref="cause" hidden></p>
    <div class="net-nums" data-ref="nums">
      <div><span>Round trip</span><b data-ref="rtt"></b></div>
      <div><span>Jitter</span><b data-ref="jitter"></b></div>
      <div><span>Loss</span><b data-ref="loss"></b></div>
    </div>
    <svg class="net-spark" viewBox="0 0 300 48" preserveAspectRatio="none" role="img" aria-label="Round trip over the last five minutes" data-ref="spark"><path d="" /></svg>
    <ul class="net-targets" data-ref="targets" aria-label="Call services"></ul>
    <p class="net-link hint" data-ref="link"></p>
    <div class="net-bw">
      <button type="button" class="net-btn" data-ref="bw">Test bandwidth</button>
      <span class="net-bw-text" data-ref="bwText" role="status"></span>
    </div>
    <details class="net-edit">
      <summary>Targets</summary>
      <ul class="net-tlist" data-ref="tlist" aria-label="Probe targets"></ul>
      <form class="net-add" data-ref="form">
        <input type="text" name="name" placeholder="Name" aria-label="Target name" maxlength="40" required>
        <input type="text" name="host" placeholder="Host or IP" aria-label="Target host" required>
        <input type="number" name="port" placeholder="Port" aria-label="Target port" min="1" max="65535" required>
        <select name="kind" aria-label="Probe kind"><option value="stun">STUN (UDP)</option><option value="tcp">TCP connect</option></select>
        <button type="submit" class="net-btn">Add</button>
      </form>
      <button type="button" class="net-btn net-reset" data-ref="reset">Restore the defaults</button>
    </details>
  </div>
</section>`;

export async function mountNetworkPanel(host, ctx) {
  const { store, act } = ctx;
  await ctx.ensureCss(new URL('./system-network.css', import.meta.url).href);
  const frag = document.createElement('template');
  frag.innerHTML = TEMPLATE;
  const sec = frag.content.firstElementChild;
  const anchor = host.querySelector('.call') || host.querySelector('.diag') || host.querySelector('.stor') || host.querySelector('.hero');
  if (anchor) anchor.after(sec); else host.prepend(sec);
  const R = {};
  sec.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  const set = (params) => act('network.set', params, { quiet: false });

  R.enabled.addEventListener('click', () => set({ enabled: R.enabled.getAttribute('aria-checked') !== 'true' }));
  R.bw.addEventListener('click', () => {
    if (store.state.call.state === 'idle') { act('network.bandwidth', {}, { quiet: false }); return; }
    confirmThen('A call is on. The bandwidth test uses your uplink for several seconds and can hurt the call.', 'Run it anyway',
      () => act('network.bandwidth', { confirm: true }, { quiet: false }));
  });
  R.reset.addEventListener('click', () => set({ reset_targets: true }));
  R.form.addEventListener('submit', (e) => {
    e.preventDefault();
    const f = new FormData(R.form);
    const next = [...store.state.network.settings.targets, { name: f.get('name'), host: f.get('host'), port: Number(f.get('port')), kind: f.get('kind') }];
    set({ targets: next });
    R.form.reset();
  });

  const targetRow = () => {
    const rm = h('button', { type: 'button', class: 'net-x' }, 'Remove');
    rm.addEventListener('click', () => {
      const key = rm.closest('li').dataset.key;
      set({ targets: store.state.network.settings.targets.filter((t) => t.name !== key) });
    });
    return h('li', { class: 'net-trow' }, h('span', { class: 'net-tn' }), h('span', { class: 'net-th' }), rm);
  };

  const render = (n) => {
    sec.hidden = !n.available;
    if (!n.available) return;
    const r = RATING[n.rating] || RATING.unknown;
    sec.dataset.tone = n.enabled ? r.tone : 'none';
    R.pill.textContent = n.enabled ? r.word : 'Off';
    R.text.textContent = n.text;
    R.enabled.setAttribute('aria-checked', String(n.enabled));
    R.cause.hidden = !n.cause;
    R.cause.textContent = n.cause ? `Likely cause: ${n.cause}.` : '';
    R.rtt.textContent = num(n.rtt_ms, 'ms');
    R.jitter.textContent = num(n.jitter_ms, 'ms');
    R.loss.textContent = num(n.loss_pct, '%', n.loss_pct % 1 ? 1 : 0);
    R.spark.querySelector('path').setAttribute('d', sparkPath(n.history));
    R.spark.hidden = !n.history.some((v) => Number.isFinite(v));
    renderList(R.targets, n.targets, (t) => t.name, () => h('li', { class: 'net-t' }, h('i'), h('span', { class: 'net-tn' }), h('span', { class: 'net-tv' })), (li, t) => {
      li.dataset.tone = (RATING[t.rating] || RATING.unknown).tone;
      li.querySelector('.net-tn').textContent = t.name;
      li.querySelector('.net-tv').textContent = t.rating === 'offline' ? (t.error === 'dns' ? 'cannot resolve' : 'no answer')
        : t.rating === 'unknown' ? 'measuring' : `${num(t.rtt_ms, 'ms')}, ${num(t.jitter_ms, 'ms jitter')}, ${t.loss_pct}% loss`;
    });
    const bits = [];
    if (n.gateway) bits.push(`Router ${n.gateway.host}: ${n.gateway.rtt_ms === null ? 'no answer' : num(n.gateway.rtt_ms, 'ms')}`);
    if (n.wifi) bits.push(`Wi-Fi ${n.wifi.iface}: ${num(n.wifi.signal_dbm, 'dBm')}${n.wifi.rate_mbps ? `, ${n.wifi.rate_mbps} Mbit/s` : ''}${n.wifi.retry_pct !== null ? `, ${n.wifi.retry_pct}% retries` : ''}`);
    else bits.push('Wired or no Wi-Fi link');
    R.link.textContent = bits.join(' · ');

    const b = n.bandwidth;
    R.bw.toggleAttribute('disabled', !!b.blocked);
    R.bw.textContent = b.state === 'running' ? 'Measuring…' : 'Test bandwidth';
    R.bw.title = b.blocked || 'Sends data for a few seconds to measure upload and download';
    R.bwText.textContent = b.state === 'error' ? b.error
      : b.state === 'done' ? `Upload ${num(b.up_mbps, 'Mbit/s', 1)}, download ${num(b.down_mbps, 'Mbit/s', 1)}. ${VERDICT[b.verdict]} (needs about ${b.needed_up_mbps} up), ${relTime(b.at)}.`
      : `A 1080p call needs about ${b.needed_up_mbps} Mbit/s up.`;

    renderList(R.tlist, n.settings.targets, (t) => t.name, targetRow, (li, t) => {
      li.querySelector('.net-tn').textContent = t.name;
      li.querySelector('.net-th').textContent = `${t.host}:${t.port} ${t.kind === 'stun' ? 'STUN' : 'TCP'}`;
      li.querySelector('.net-x').toggleAttribute('disabled', n.settings.targets.length <= 1);
      li.querySelector('.net-x').setAttribute('aria-label', `Remove ${t.name}`);
    });
    R.form.querySelector('button').toggleAttribute('disabled', n.settings.targets.length >= n.settings.max_targets);
  };
  const off = store.subscribe((s) => s.network, render);
  return () => { off(); sec.remove(); };
}
