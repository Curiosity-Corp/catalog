// pages/auto.js — THE AI DIRECTOR.
//
// Left: a big on/off switch with what it does, "Ask Claude now", "Undo all changes" (hold to confirm) and
// Claude's latest note as a quote. Right: the timeline of every change, newest first, grouped by minute,
// with colour-and-icon kinds (video, audio, ai, error). Relative times refresh every 15 seconds.
//
// Keys on this page: D director on/off, N ask Claude now. Undo is deliberately a hold, never a key.

import { h, renderList } from '../core/util.js';
import { icon, iconHTML } from '../components/icons.js';
import { relTime, minuteKey, parseLocal } from '../core/time.js';
import { toggleAuto } from '../core/ops.js';

const KINDS = {
  video: { icon: 'camera', label: 'Picture' },
  audio: { icon: 'audio', label: 'Sound' },
  ai: { icon: 'auto', label: 'Claude' },
  error: { icon: 'alert', label: 'Problem' },
};
const kindOf = (k) => KINDS[k] || { icon: 'dot', label: 'Change' };

/** "(0.8) Face slightly under-exposed" -> { confidence: 0.8, text: "Face slightly …" } */
export function splitConfidence(text) {
  const m = /^\s*\((\d(?:\.\d+)?)\)\s*(.*)$/s.exec(String(text || ''));
  if (!m) return { confidence: null, text: String(text || '') };
  const c = Number(m[1]);
  return Number.isFinite(c) && c >= 0 && c <= 1 ? { confidence: c, text: m[2] } : { confidence: null, text: String(text || '') };
}
const confLabel = (c) => `${c >= 0.75 ? 'Confident' : c >= 0.45 ? 'Fairly sure' : 'Unsure'} · ${Math.round(c * 100)}%`;
const prettyEvent = (e) => String(e || '').replace(/[-_]+/g, ' ').replace(/^./, (c) => c.toUpperCase());

const TEMPLATE = `
<div class="aut">
  <div class="aut-left">
    <ss-card class="aut-hero" heading="Director" icon="auto" data-ref="hero">
      <div class="hero-top">
        <button type="button" class="big-switch" role="switch" aria-checked="false" data-ref="sw" aria-describedby="swState swWhy">
          <span class="bs-track"><span class="bs-knob"></span></span>
          <span class="bs-text"><span class="bs-title">AI director</span><span class="bs-state" id="swState" data-ref="swState">Off</span></span>
          <kbd class="keycap">D</kbd>
        </button>
      </div>
      <p class="hero-why" id="swWhy">Every few minutes Claude looks at one still picture from your camera and the loudness of your voice, then nudges exposure, colour and mic gain the way a good operator would. Small steps, always undoable.</p>
      <p class="hint hero-meta" data-ref="meta" role="status"></p>
      <div class="hero-actions">
        <ss-button icon="auto" variant="primary" data-ref="now">Ask Claude now</ss-button>
        <ss-button icon="undo" variant="danger" hold="900" data-ref="undo" title="Press and hold to undo every change the director made">Undo all changes</ss-button>
      </div>
    </ss-card>

    <ss-card class="aut-note" heading="Claude's latest note" icon="quote" data-ref="noteCard">
      <div class="note-in">
        <blockquote class="quote" data-ref="quote"></blockquote>
        <div class="quote-meta" data-ref="quoteMeta"></div>
      </div>
    </ss-card>
  </div>

  <ss-card class="aut-tl" heading="What changed" icon="list" data-ref="tlCard">
    <span slot="actions" class="count" data-ref="count"></span>
    <div class="tl-scroll" data-ref="tlScroll" tabindex="0" role="region" aria-label="Timeline of changes, newest first">
      <ol class="tl" data-ref="tl"></ol>
      <div class="empty" data-ref="empty" hidden>
        <div class="empty-art" aria-hidden="true">${iconHTML('auto')}</div>
        <h2>Nothing yet</h2>
        <p>Turn the director on and every change it makes shows up here with a reason, so you always know what moved and why.</p>
        <ol class="how">
          <li><b>It looks.</b> One still image from the camera and the loudness numbers of your voice. No video and no audio ever leave this computer for the review.</li>
          <li><b>It decides.</b> Claude suggests small changes to brightness, colour or mic gain.</li>
          <li><b>You stay in charge.</b> Move any control yourself and the director respects it. Undo all changes puts everything back.</li>
        </ol>
      </div>
    </div>
  </ss-card>
</div>`;

export async function mount(host, ctx) {
  const { store, act, registerKeys, registerCommands } = ctx;
  await ctx.ensureCss(new URL('./common.css', import.meta.url).href);
  await ctx.ensureCss(new URL('./auto.css', import.meta.url).href);
  document.title = 'Auto · StreamStack';
  host.innerHTML = TEMPLATE;
  const R = {};
  host.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  const off = [];
  const sub = (selector, cb) => off.push(store.subscribe(selector, cb));
  let auto = store.state.auto;

  // ---------- relative times ----------
  const stamp = () => {
    for (const el of host.querySelectorAll('time[data-t]')) el.textContent = relTime(el.dataset.t);
    const q = R.quoteMeta.querySelector('time');
    if (q) q.textContent = relTime(q.dataset.t);
  };
  const clock = setInterval(stamp, 15000);
  off.push(() => clearInterval(clock));

  // ---------- hero ----------
  sub((s) => s.auto, (a) => {
    auto = a;
    R.sw.setAttribute('aria-checked', String(a.running));
    R.hero.toggleAttribute('data-on', a.running);
    R.swState.textContent = a.busy ? 'Looking at the picture…' : a.running ? 'On and watching' : 'Off';
    R.meta.textContent = `Brain: ${a.backend || 'not set'}${a.running ? '' : '. Off means nothing changes by itself.'}`;
    R.now.toggleAttribute('busy', a.busy);
    R.now.toggleAttribute('disabled', a.busy);
    R.undo.toggleAttribute('disabled', !a.can_undo);
    R.undo.setAttribute('title', a.can_undo ? 'Press and hold to undo every change the director made' : 'Nothing to undo yet');

    // note
    const note = splitConfidence(a.last_note);
    R.noteCard.toggleAttribute('data-empty', !a.last_note);
    R.quote.textContent = a.last_note ? note.text : 'No note yet. Ask Claude now to get one.';
    R.quoteMeta.replaceChildren();
    if (a.last_note) {
      if (note.confidence !== null) R.quoteMeta.append(h('span', { class: 'pill', 'data-state': note.confidence >= 0.75 ? 'ok' : note.confidence >= 0.45 ? 'info' : 'warn' }, confLabel(note.confidence)));
      if (a.last_time) R.quoteMeta.append(h('time', { datetime: a.last_time, 'data-t': a.last_time, title: parseLocal(a.last_time)?.toLocaleString() || '' }, relTime(a.last_time)));
    }

    // timeline, grouped by minute (journal is newest first)
    const groups = [];
    for (const e of a.journal || []) {
      const key = minuteKey(e.time);
      const last = groups[groups.length - 1];
      if (last && last.key === key) last.items.push(e); else groups.push({ key, time: e.time, items: [e] });
    }
    R.empty.hidden = groups.length > 0;
    R.tl.hidden = groups.length === 0;
    R.count.textContent = groups.length ? `${(a.journal || []).length} changes` : '';
    renderList(R.tl, groups, (g) => g.key, () => h('li', { class: 'grp' }, h('div', { class: 'grp-head' }, h('time')), h('ul', { class: 'rows' })), (li, g) => {
      const t = li.querySelector('.grp-head time');
      t.dataset.t = g.time;
      t.setAttribute('datetime', g.time);
      t.textContent = relTime(g.time);
      const d = parseLocal(g.time);
      t.title = d ? d.toLocaleString() : '';
      renderList(li.querySelector('.rows'), g.items.map((e, i) => ({ ...e, _k: `${e.time}|${e.event}|${e.text}|${i}` })), (e) => e._k, () => h('li', { class: 'row' },
        h('span', { class: 'row-ic' }), h('div', { class: 'row-body' }, h('div', { class: 'row-title' }, h('b'), h('span', { class: 'chip' })), h('p', { class: 'row-text' }))), (row, e) => {
        const k = kindOf(e.kind);
        row.dataset.kind = e.kind || 'other';
        const ic = row.querySelector('.row-ic');
        if (ic.dataset.i !== k.icon) { ic.dataset.i = k.icon; ic.replaceChildren(icon(k.icon)); }
        ic.setAttribute('title', k.label);
        const split = e.kind === 'ai' ? splitConfidence(e.text) : { confidence: null, text: e.text };
        row.querySelector('.row-title b').textContent = e.event && e.event !== e.kind ? `${k.label} · ${prettyEvent(e.event)}` : k.label;
        const chip = row.querySelector('.chip');
        chip.hidden = split.confidence === null;
        chip.textContent = split.confidence === null ? '' : confLabel(split.confidence);
        row.querySelector('.row-text').textContent = split.text;
      });
    });
  });

  // ---------- actions ----------
  R.sw.addEventListener('click', () => toggleAuto());
  R.now.addEventListener('click', () => act('auto.once'));
  R.undo.addEventListener('click', () => act('auto.reset'));
  const ask = () => { if (!auto.busy) act('auto.once'); };
  off.push(registerKeys([
    { key: 'd', desc: 'AI director on or off', group: 'Auto', run: () => toggleAuto() },
    { key: 'n', desc: 'Ask Claude to review the picture now', group: 'Auto', run: ask },
  ]));
  off.push(registerCommands(() => [
    { id: 'auto:toggle', group: 'Auto', title: auto.running ? 'Turn AI director off' : 'Turn AI director on', icon: 'auto', hint: 'D', keywords: 'auto ai director claude on off', run: () => toggleAuto() },
    { id: 'auto:now', group: 'Auto', title: 'Ask Claude now', icon: 'auto', hint: 'N', keywords: 'auto ai review once now claude', run: ask },
  ]));

  return () => off.forEach((fn) => fn());
}
