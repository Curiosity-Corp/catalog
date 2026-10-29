// pages/auto.js — THE AI DIRECTOR.
//
// Left: a big on/off switch with what it does and what leaves this computer, "Ask Claude now", "Undo all
// changes" (hold to confirm), Claude's latest note, the contact sheet of the last burst of stills with the
// measured picture, and the voice read-out (pitch, depth, clarity, sibilance, clean). Right: persona cards
// (choose, edit, reset), the time-of-day strip, what the director can drive, what it found, and the timeline of
// every change with its reason, newest first. Relative times refresh every 15 seconds.
//
// Keys on this page: D director on/off, N ask Claude now. Undo is deliberately a hold, never a key.

import { h, renderList } from '../core/util.js';
import { apiUrl, postAction, ApiError } from '../core/api.js';
import { icon, iconHTML } from '../components/icons.js';
import { relTime, minuteKey, parseLocal } from '../core/time.js';
import { toggleAuto } from '../core/ops.js';
import { mountMeetings } from './meetings.js';

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

const SLOT_ORDER = ['night', 'dawn', 'morning', 'afternoon', 'evening'];
const slotName = (slot) => String(slot || '').replace(/^./, (c) => c.toUpperCase());
const num = (v, d = 1) => (Number.isFinite(v) ? Number(v).toFixed(d) : '–');
const KELVIN = (v) => `${Math.round(v)} K`;
const hhmm = (t) => t || '–';

/** The five voice bars: label, 0-1 score, the readable value, and which persona target (if any) applies. */
export function voiceBars(features, persona) {
  const f = features || {};
  const sc = f.scores || {};
  const t = persona?.voice?.targets || {};
  const hz = Number.isFinite(f.f0_hz) ? `${Math.round(f.f0_hz)} Hz` : '–';
  const range = Number.isFinite(f.f0_low_hz) && Number.isFinite(f.f0_high_hz) ? `${Math.round(f.f0_low_hz)} to ${Math.round(f.f0_high_hz)} Hz` : '';
  return [
    { id: 'pitch', label: 'Pitch', score: sc.pitch, value: hz, detail: [f.pitch_band, range].filter(Boolean).join(', ') },
    { id: 'depth', label: 'Depth', score: sc.depth, value: Number.isFinite(f.tilt_db_oct) ? `${num(f.tilt_db_oct)} dB/oct` : '–', detail: 'low-end warmth', target: t.depth },
    { id: 'clarity', label: 'Clarity', score: sc.clarity, value: Number.isFinite(f.clarity_db) ? `${num(f.clarity_db)} dB` : '–', detail: '2 to 4 kHz presence', target: t.clarity },
    { id: 'sibilance', label: 'Sibilance', score: sc.sibilance, value: Number.isFinite(f.sibilance_db) ? `${num(f.sibilance_db)} dB` : '–', detail: '5 to 9 kHz hiss on S sounds', target: t.sibilance },
    { id: 'clean', label: 'Clean', score: sc.clean, value: Number.isFinite(f.snr_db) ? `${num(f.snr_db, 0)} dB SNR` : '–', detail: 'speech against room noise' },
  ];
}

/** Segments of the day strip as percentages of 24 hours. */
export function stripSegments(strip) {
  return (strip || []).map((seg) => ({ slot: seg.slot, from: (seg.start / 1440) * 100, width: ((seg.end - seg.start) / 1440) * 100 }));
}

/** A persona with the editor's field values applied (the server validates; this only shapes the document). */
export function applyEdits(persona, f) {
  const out = JSON.parse(JSON.stringify(persona));
  delete out.source;
  out.name = f.name.trim();
  out.tagline = f.tagline.trim();
  out.mood = f.mood.split(',').map((w) => w.trim()).filter(Boolean);
  out.picture.face_luma = [Number(f.face_lo), Number(f.face_hi)];
  out.picture.warmth_k = Number(f.warmth_k);
  for (const k of ['depth', 'clarity', 'sibilance']) out.voice.targets[k] = [Number(f[`${k}_lo`]), Number(f[`${k}_hi`])];
  return out;
}

export const slugify = (name) => {
  const base = String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').replace(/^[^a-z]+/, '').slice(0, 32).replace(/-+$/, '');
  return base.length >= 2 ? base : 'my-persona';
};

const FIELDS = [
  { key: 'name', label: 'Name', type: 'text', get: (p) => p.name, wide: true },
  { key: 'tagline', label: 'Tagline', type: 'text', get: (p) => p.tagline, wide: true },
  { key: 'mood', label: 'Mood words, comma separated', type: 'text', get: (p) => (p.mood || []).join(', '), wide: true },
  { key: 'face_lo', label: 'Face brightness, from', get: (p) => p.picture.face_luma[0] },
  { key: 'face_hi', label: 'Face brightness, to', get: (p) => p.picture.face_luma[1] },
  { key: 'warmth_k', label: 'Warmth (Kelvin)', get: (p) => p.picture.warmth_k },
  { key: 'depth_lo', label: 'Depth, from (0 to 1)', get: (p) => p.voice.targets.depth[0] },
  { key: 'depth_hi', label: 'Depth, to', get: (p) => p.voice.targets.depth[1] },
  { key: 'clarity_lo', label: 'Clarity, from', get: (p) => p.voice.targets.clarity[0] },
  { key: 'clarity_hi', label: 'Clarity, to', get: (p) => p.voice.targets.clarity[1] },
  { key: 'sibilance_lo', label: 'Sibilance, from', get: (p) => p.voice.targets.sibilance[0] },
  { key: 'sibilance_hi', label: 'Sibilance, to', get: (p) => p.voice.targets.sibilance[1] },
];

const PICTURE_STATS = (p) => [
  ['Face brightness', p.skin ? num(p.skin.luma, 0) : 'no face found'],
  ['Frame brightness', num(p.luma_mean, 0)],
  ['Colour', `${p.cast || '–'}, warmth ${num(p.warm_index, 2)}`],
  ['Highlights clipped', `${num((p.clipped_highlights || 0) * 100)}%`],
  ['Shadows crushed', `${num((p.crushed_shadows || 0) * 100)}%`],
  ['Sharpness', num(p.sharpness, 0)],
  ['Noise', num(p.noise)],
];
const VOICE_STATS = (f) => [
  ['Loudness', Number.isFinite(f.lufs) ? `${num(f.lufs)} LUFS` : '–'],
  ['Speech peak', Number.isFinite(f.peak_db) ? `${num(f.peak_db)} dB` : '–'],
  ['Noise floor', Number.isFinite(f.noise_floor_db) ? `${num(f.noise_floor_db, 0)} dB` : '–'],
  ['Room', f.room ? `${f.room}${Number.isFinite(f.rt60_s) ? `, ${num(f.rt60_s, 2)} s echo` : ''}` : '–'],
];
const fillStats = (dl, rows) => {
  dl.replaceChildren(...rows.flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]));
};

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
      <p class="hero-why" id="swWhy">Every few minutes Claude looks at a few still pictures from your camera and reads measurements of your voice, then nudges exposure, colour and mic gain towards the persona you chose, the way a good operator would. Small steps, always undoable.</p>
      <p class="privacy" data-ref="privacy"><b>What leaves this computer.</b> The stills go to Claude, joined into one contact sheet. Your audio never does: a short clip is measured here and deleted, and only the measured numbers (pitch, loudness, clarity) are shared.</p>
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

    <ss-card class="aut-sheet" heading="What Claude saw" icon="camera" data-ref="sheetCard">
      <figure class="sheet">
        <img class="sheet-img" data-ref="sheetImg" alt="Contact sheet: the last burst of stills, side by side" hidden>
        <p class="sheet-empty" data-ref="sheetEmpty">No review yet. Ask Claude now and the stills it looks at appear here.</p>
        <figcaption class="hint" data-ref="sheetCap"></figcaption>
      </figure>
      <div class="pic" data-ref="pic" hidden>
        <div class="hist" data-ref="hist" role="img" aria-label="Exposure histogram of the picture"></div>
        <dl class="stats" data-ref="picStats"></dl>
      </div>
    </ss-card>

    <ss-card class="aut-voice" heading="How you sound" icon="audio" data-ref="voiceCard">
      <p class="hint" data-ref="voiceNote"></p>
      <ul class="bars" data-ref="bars" aria-label="Voice read-out"></ul>
      <dl class="stats" data-ref="voiceStats"></dl>
    </ss-card>
  </div>

  <div class="aut-right">
    <ss-card class="aut-personas" heading="Persona" icon="auto" data-ref="personaCard">
      <p class="hint">The look and sound Claude steers towards, and judges the mood against.</p>
      <div class="cards" role="radiogroup" aria-label="Persona" data-ref="cards"></div>
      <div class="persona-actions">
        <ss-button icon="sliders" data-ref="editOpen">Edit this persona</ss-button>
      </div>
      <form class="editor" data-ref="editor" hidden aria-label="Edit persona" novalidate>
        <div class="ed-grid" data-ref="edFields"></div>
        <p class="ed-error" data-ref="edError" role="alert" hidden></p>
        <div class="ed-actions">
          <ss-button variant="primary" data-ref="edSave">Save changes</ss-button>
          <ss-button data-ref="edCopy">Save as a new persona</ss-button>
          <ss-button variant="ghost" data-ref="edReset">Restore</ss-button>
          <ss-button variant="ghost" data-ref="edCancel">Cancel</ss-button>
        </div>
      </form>
    </ss-card>

    <ss-card class="aut-time" heading="Time of day" icon="sun" data-ref="timeCard">
      <p class="now" data-ref="nowText" role="status"></p>
      <div class="strip" data-ref="strip" role="img" aria-label="The day in slots, with now marked"></div>
      <ul class="strip-key" data-ref="stripKey"></ul>
      <p class="hint" data-ref="sunNote"></p>
      <p class="hint" data-ref="learnedNote"></p>
      <form class="place" data-ref="place" aria-label="Property location" novalidate>
        <label>Property <input name="name" maxlength="60" placeholder="Studio" autocomplete="off"></label>
        <label>Latitude <input name="latitude" inputmode="decimal" placeholder="51.48" autocomplete="off"></label>
        <label>Longitude <input name="longitude" inputmode="decimal" placeholder="-0.12" autocomplete="off"></label>
        <ss-button data-ref="placeSave">Set location</ss-button>
        <ss-button variant="ghost" data-ref="placeClear">Use time zone</ss-button>
      </form>
      <p class="hint">Sunrise and sunset are computed on this computer from the location. Nothing is looked up online.</p>
    </ss-card>

    <ss-card class="aut-finds" heading="What the director found" icon="list" data-ref="findCard">
      <ul class="finds" data-ref="finds" aria-label="Findings"></ul>
      <p class="hint" data-ref="findEmpty">Nothing measured yet.</p>
    </ss-card>

    <ss-card class="aut-acts" heading="What it can adjust" icon="sliders" data-ref="actCard">
      <ul class="acts" data-ref="acts" aria-label="Actuators"></ul>
    </ss-card>

  <ss-card class="aut-tl" heading="What changed" icon="list" data-ref="tlCard">
    <span slot="actions" class="count" data-ref="count"></span>
    <div class="tl-scroll" data-ref="tlScroll" tabindex="0" role="region" aria-label="Timeline of changes, newest first">
      <ol class="tl" data-ref="tl"></ol>
      <div class="empty" data-ref="empty" hidden>
        <div class="empty-art" aria-hidden="true">${iconHTML('auto')}</div>
        <h2>Nothing yet</h2>
        <p>Turn the director on and every change it makes shows up here with a reason, so you always know what moved and why.</p>
        <ol class="how">
          <li><b>It looks and listens.</b> A few stills from the camera, joined into one contact sheet that goes to Claude, and measurements of your voice taken here. Your audio never leaves this computer.</li>
          <li><b>It decides.</b> Claude suggests small changes to brightness, colour or mic gain, judged against your persona and the time of day.</li>
          <li><b>You stay in charge.</b> Move any control yourself and the director respects it. Undo all changes puts everything back.</li>
        </ol>
      </div>
    </div>
  </ss-card>
  </div>
</div>`;

export async function mount(host, ctx) {
  const { store, act, registerKeys, registerCommands } = ctx;
  await ctx.ensureCss(new URL('./common.css', import.meta.url).href);
  await ctx.ensureCss(new URL('./auto.css', import.meta.url).href);
  await ctx.ensureCss(new URL('./meetings.css', import.meta.url).href);
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
        h('span', { class: 'row-ic' }), h('div', { class: 'row-body' }, h('div', { class: 'row-title' }, h('b'), h('span', { class: 'chip' })), h('p', { class: 'row-text' }), h('p', { class: 'row-reason' }))), (row, e) => {
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
        const ch = e.event === 'change' ? e.change : null;
        row.querySelector('.row-text').textContent = ch ? `${prettyEvent(ch.key)}: ${ch.before} to ${ch.after}${ch.actuator ? ` on ${ch.target || ch.actuator}` : ''}` : split.text;
        const why = row.querySelector('.row-reason');
        why.hidden = !e.reason;
        why.textContent = e.reason ? `Why: ${e.reason}` : '';
        row.querySelector('.row-title b').textContent = ch ? `${k.label} · ${ch.source === 'claude' ? 'Claude changed' : 'Director changed'}` : row.querySelector('.row-title b').textContent;
      });
    });
  });


  // ---------- director: persona, picture, voice, time of day ----------
  const directorOf = () => (store.state.auto || {}).director || {};
  const personaOf = (d) => (d.personas || []).find((p) => p.id === d.persona);
  let editing = false;

  const renderCards = (d) => {
    renderList(R.cards, d.personas || [], (p) => p.id, () => h('button', { type: 'button', class: 'pcard', role: 'radio' },
      h('span', { class: 'pc-swatch' }), h('span', { class: 'pc-name' }), h('span', { class: 'pc-tag' }), h('span', { class: 'pc-mood' }), h('span', { class: 'pc-badge' })), (card, p) => {
      const on = p.id === d.persona;
      card.setAttribute('aria-checked', String(on));
      card.setAttribute('aria-label', `${p.name}, ${p.tagline}`);
      card.dataset.id = p.id;
      const [a, b] = p.swatch || ['#444', '#888'];
      card.querySelector('.pc-swatch').style.setProperty('--c1', a);
      card.querySelector('.pc-swatch').style.setProperty('--c2', b);
      card.querySelector('.pc-name').textContent = p.name;
      card.querySelector('.pc-tag').textContent = p.tagline;
      card.querySelector('.pc-mood').textContent = (p.mood || []).slice(0, 4).join(' · ');
      const badge = card.querySelector('.pc-badge');
      badge.textContent = p.source === 'edited' ? 'Edited' : p.source === 'custom' ? 'Yours' : '';
      badge.hidden = !badge.textContent;
    });
  };

  const renderTime = (d) => {
    const t = d.time;
    if (!t) return;
    const sun = t.sun;
    R.nowText.textContent = `Now: ${sun.label}${sun.trend === 'rising' ? ', daylight rising' : sun.trend === 'falling' ? ', daylight falling' : ''}`;
    R.nowText.dataset.slot = sun.slot;
    const segs = stripSegments(t.strip);
    R.strip.replaceChildren(...segs.map((g) => {
      const el = h('span', { class: 'seg', 'data-slot': g.slot, title: slotName(g.slot) });
      el.style.setProperty('--from', `${g.from}%`);
      el.style.setProperty('--w', `${g.width}%`);
      if (d.learned && d.learned[g.slot]) el.dataset.learned = '';
      return el;
    }), (() => {
      const mark = h('span', { class: 'now-mark', 'aria-hidden': 'true' });
      mark.style.setProperty('--at', `${(t.minute / 1440) * 100}%`);
      return mark;
    })());
    R.stripKey.replaceChildren(...SLOT_ORDER.map((slot) => h('li', { 'data-slot': slot, 'data-current': slot === sun.slot ? '' : null },
      h('i', { class: 'dot' }), slotName(slot), d.learned && d.learned[slot] ? h('span', { class: 'learned-tag', title: `Learned from ${d.learned[slot].n} kept settings` }, 'learned') : null)));
    const times = sun.times || {};
    R.sunNote.textContent = `Sunrise ${hhmm(times.sunrise)}, sunset ${hhmm(times.sunset)} for ${t.location.name}${t.location.source === 'timezone' ? ' (from the time zone)' : ''}. ${sun.note || ''}`;
    const cur = d.learned && d.learned[sun.slot];
    R.learnedNote.textContent = cur
      ? `Learned for ${sun.slot_label.toLowerCase()}: ${Object.entries(cur.look || {}).map(([k, v]) => `${k} ${v >= 0 ? '+' : ''}${num(v, 2)}`).join(', ')} (from ${cur.n} kept settings).`
      : `Nothing learned for ${sun.slot_label.toLowerCase()} yet. Settings you keep become this persona's starting point for this part of the day.`;
    const f = R.place.elements;
    if (document.activeElement?.closest?.('form') !== R.place) {
      f.name.value = t.location.source === 'timezone' ? '' : t.location.name;
      f.latitude.value = t.location.source === 'timezone' ? '' : String(t.location.latitude);
      f.longitude.value = t.location.source === 'timezone' ? '' : String(t.location.longitude);
    }
  };

  const renderSheet = (d) => {
    const report = d.report || {};
    const seen = report.observation || {};
    if (d.sheet) {
      const src = apiUrl('api/director/sheet.jpg', { v: d.sheet });
      if (R.sheetImg.getAttribute('src') !== src) R.sheetImg.setAttribute('src', src);
    }
    R.sheetImg.hidden = !d.sheet;
    R.sheetEmpty.hidden = !!d.sheet;
    R.sheetCap.textContent = seen.frames
      ? `${seen.frames} stills${seen.spacing ? `, ${num(seen.spacing)} s apart` : ''} from ${seen.source || 'the camera'}, sent to Claude as one picture${report.time ? ` · ${relTime(report.time)}` : ''}.`
      : '';
    const pic = seen.picture;
    R.pic.hidden = !pic;
    if (!pic) return;
    const bins = pic.histogram || [];
    const top = Math.max(0.0001, ...bins);
    R.hist.replaceChildren(...bins.map((v, i) => {
      const bar = h('i', { class: 'hb', 'data-clip': i === 0 || i === bins.length - 1 ? '' : null });
      bar.style.setProperty('--v', `${Math.max(2, (v / top) * 100)}%`);
      return bar;
    }));
    fillStats(R.picStats, PICTURE_STATS(pic));
  };

  const renderVoice = (d) => {
    const audio = (d.report || {}).audio || {};
    const f = audio.features;
    R.voiceNote.textContent = f && f.enough_speech !== false
      ? `Measured on this computer from a ${num(f.seconds, 0)} second clip, then the clip was deleted. Only these numbers are shared with Claude. The shaded band is what ${personaOf(d)?.name || 'the persona'} aims for.`
      : (audio.reason || 'No voice measured yet. It is measured while the director is on and you are speaking. Audio never leaves this computer.');
    const bars = f ? voiceBars(f, personaOf(d)) : [];
    renderList(R.bars, bars, (b) => b.id, () => h('li', { class: 'bar-row' },
      h('span', { class: 'bar-label' }), h('span', { class: 'bar', role: 'meter', 'aria-valuemin': '0', 'aria-valuemax': '1' }, h('i', { class: 'band' }), h('i', { class: 'fill' })), h('span', { class: 'bar-value' })), (row, b) => {
      const meter = row.querySelector('.bar');
      const score = Number.isFinite(b.score) ? Math.min(1, Math.max(0, b.score)) : 0;
      meter.setAttribute('aria-label', b.label);
      meter.setAttribute('aria-valuenow', score.toFixed(2));
      meter.setAttribute('aria-valuetext', `${b.value}${b.detail ? `, ${b.detail}` : ''}`);
      meter.querySelector('.fill').style.setProperty('--v', `${score * 100}%`);
      const band = meter.querySelector('.band');
      band.hidden = !b.target;
      if (b.target) {
        band.style.setProperty('--from', `${b.target[0] * 100}%`);
        band.style.setProperty('--w', `${(b.target[1] - b.target[0]) * 100}%`);
      }
      row.querySelector('.bar-label').textContent = b.label;
      row.querySelector('.bar-value').textContent = b.value;
      row.title = b.detail;
    });
    R.bars.hidden = !f;
    R.voiceStats.hidden = !f;
    if (f) fillStats(R.voiceStats, VOICE_STATS(f));
  };

  const renderFindings = (d) => {
    const list = (d.report || {}).findings || [];
    R.findEmpty.hidden = list.length > 0;
    renderList(R.finds, list, (f) => f.id, () => h('li', { class: 'find' }, h('span', { class: 'pill' }), h('span', { class: 'find-text' })), (li, f) => {
      li.dataset.state = f.state;
      const pill = li.querySelector('.pill');
      pill.dataset.state = f.state === 'ok' ? 'ok' : f.state === 'warn' ? 'warn' : 'info';
      pill.textContent = f.state === 'ok' ? 'Good' : f.state === 'warn' ? 'Watch' : 'Note';
      li.querySelector('.find-text').textContent = f.measure ? `${f.text} (${f.measure})` : f.text;
    });
  };

  const renderActuators = (d) => {
    const list = (d.report || {}).actuators || [];
    renderList(R.acts, list, (a) => a.id, () => h('li', { class: 'act' }, h('b'), h('span', { class: 'pill' }), h('span', { class: 'act-why' })), (li, a) => {
      li.querySelector('b').textContent = a.label;
      const pill = li.querySelector('.pill');
      pill.dataset.state = a.available ? 'ok' : 'info';
      pill.textContent = a.available ? 'Ready' : a.kind === 'planned' ? 'Planned' : 'Unavailable';
      li.querySelector('.act-why').textContent = a.reason || '';
    });
    R.actCard.hidden = list.length === 0;
  };

  const renderEditor = (d) => {
    const p = personaOf(d);
    R.editOpen.toggleAttribute('disabled', !p);
    R.editor.hidden = !editing || !p;
    if (R.editor.hidden || R.editor.dataset.for === p.id) return;
    R.editor.dataset.for = p.id;
    R.edFields.replaceChildren(...FIELDS.map((fd) => h('label', { class: fd.wide ? 'wide' : '' }, fd.label,
      h('input', { name: fd.key, type: fd.type || 'number', step: 'any', value: fd.get(p), autocomplete: 'off' }))));
    R.edReset.textContent = p.source === 'custom' ? 'Delete this persona' : 'Restore the original';
    R.edReset.toggleAttribute('disabled', p.source === 'shipped');
    R.edError.hidden = true;
  };

  sub((s) => s.auto.director, (d) => {
    d = d || {};
    const broken = !!d.error;
    R.personaCard.toggleAttribute('data-error', broken);
    if (broken) { R.nowText.textContent = d.error; return; }
    if (!d.personas) return;
    renderCards(d);
    renderTime(d);
    renderSheet(d);
    renderVoice(d);
    renderFindings(d);
    renderActuators(d);
    renderEditor(d);
  });

  R.cards.addEventListener('click', (e) => {
    const card = e.target.closest('.pcard');
    if (card && card.dataset.id !== directorOf().persona) act('auto.persona', { id: card.dataset.id });
  });
  R.cards.addEventListener('keydown', (e) => {
    const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
    if (!(e.key in keys)) return;
    const all = [...R.cards.querySelectorAll('.pcard')];
    const next = all[(all.indexOf(document.activeElement) + keys[e.key] + all.length) % all.length];
    if (!next) return;
    e.preventDefault();
    next.focus();
    next.click();
  });

  const formValues = () => Object.fromEntries([...R.editor.elements].filter((el) => el.name).map((el) => [el.name, el.value]));
  const saveEdits = async (asNew) => {
    const d = directorOf();
    const p = personaOf(d);
    if (!p) return;
    const values = formValues();
    const doc = applyEdits(p, values);
    if (asNew) {
      const taken = new Set((d.personas || []).map((x) => x.id));
      let id = slugify(doc.name);
      if (taken.has(id)) id = slugify(`${doc.name}-2`);
      for (let n = 3; taken.has(id) && n < 50; n += 1) id = slugify(`${doc.name}-${n}`);
      doc.id = id;
    }
    R.edSave.setAttribute('busy', '');
    try {
      await postAction('auto.persona_save', { persona: doc });
    } catch (err) {
      R.edSave.removeAttribute('busy');
      R.edError.textContent = err instanceof ApiError ? err.message : `That persona could not be saved: ${err}`;
      R.edError.hidden = false;
      return;
    }
    R.edSave.removeAttribute('busy');
    if (asNew) await act('auto.persona', { id: doc.id });
    editing = false;
    delete R.editor.dataset.for;
    renderEditor(directorOf());
  };
  R.editOpen.addEventListener('click', () => { editing = true; delete R.editor.dataset.for; renderEditor(directorOf()); R.editor.querySelector('input')?.focus(); });
  R.edCancel.addEventListener('click', () => { editing = false; renderEditor(directorOf()); });
  R.edSave.addEventListener('click', () => saveEdits(false));
  R.edCopy.addEventListener('click', () => saveEdits(true));
  R.editor.addEventListener('submit', (e) => { e.preventDefault(); saveEdits(false); });
  R.editor.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.tagName === 'INPUT') { e.preventDefault(); saveEdits(false); } });
  R.edReset.addEventListener('click', async () => {
    const p = personaOf(directorOf());
    if (!p) return;
    const res = await act('auto.persona_reset', { id: p.id });
    if (res?.ok) { editing = false; delete R.editor.dataset.for; renderEditor(directorOf()); }
  });

  const placeValues = () => {
    const f = R.place.elements;
    return { name: f.name.value.trim() || 'This property', latitude: f.latitude.value.trim(), longitude: f.longitude.value.trim() };
  };
  const savePlace = () => {
    const v = placeValues();
    act('auto.location', { name: v.name, latitude: Number(v.latitude), longitude: Number(v.longitude) });
  };
  R.placeSave.addEventListener('click', savePlace);
  R.place.addEventListener('submit', (e) => { e.preventDefault(); savePlace(); });
  R.place.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.tagName === 'INPUT') { e.preventDefault(); savePlace(); } });
  R.placeClear.addEventListener('click', () => act('auto.location', { clear: true }));

  // ---------- actions ----------
  R.sw.addEventListener('click', () => toggleAuto());
  R.now.addEventListener('click', () => act('auto.once'));
  R.undo.addEventListener('click', () => act('auto.reset'));
  const ask = () => { if (!auto.busy) act('auto.once'); };
  off.push(mountMeetings(host.querySelector('.aut-right'), ctx)); // calendar-aware setup: its own card at the top of the right column
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
