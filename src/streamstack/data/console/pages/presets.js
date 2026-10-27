// pages/presets.js — THE PRESETS PAGE: profiles, platform and bandwidth tier, and the looks library.
//
// Top: the profile gallery. One press applies a whole setup (look, platform, tier, lights, voice, framing) through
// the same actions every other page uses; the result shows part by part, so a part that could not be set is named.
// Your own profiles are saved from the current rig, exported as JSON, imported from JSON and deleted by holding.
// Middle: platform and tier pickers with the resolved stream plan. The plan goes to `stream.set`, a pending
// contract: the choice is saved, and the page says "pending" on a rig without the stream backend.
// Bottom: the looks library with thumbnails rendered from one test portrait, filtered by category.
//
// Data: `state.presets` (selection, profiles, last apply, stream status) and `state.video.looks` come over the live
// connection; the platform and tier catalog (static) is fetched once from api/presets/catalog.

import { h, renderList } from '../core/util.js';
import { getJson } from '../core/api.js';
import { toast } from '../core/toast.js';
import { sameLook, drawSwatch } from './look-swatch.js';

const MAX_IMPORT_BYTES = 60 * 1024;
const STATUS_WORDS = { ok: 'Applied', pending: 'Pending', failed: 'Not applied' };
const VERIFIED_WORDS = { official: 'Official source', community: 'Community figures', uncertain: 'Uncertain' };
const VERIFIED_STATE = { official: 'ok', community: 'info', uncertain: 'warn' };
const CODEC_NAMES = { av1: 'AV1', hevc: 'HEVC', h264: 'H.264' };
const TRANSPORT_NAMES = {
  'enhanced-rtmp': 'Enhanced RTMP', rtmp: 'RTMP', srt: 'SRT', whip: 'WHIP', virtualcam: 'Virtual camera', file: 'File',
};
const AUDIO_NAMES = { aac: 'AAC', opus: 'Opus', pcm: 'PCM' };

const TEMPLATE = `
<div class="pr">
  <ss-card class="pr-card" heading="Profiles" icon="play" data-ref="profilesCard">
    <div slot="actions" class="pr-actions">
      <ss-button size="sm" variant="ghost" icon="plus" data-ref="importBtn">Import</ss-button>
      <ss-button size="sm" variant="ghost" icon="save" data-ref="exportBtn">Export yours</ss-button>
      <input type="file" accept="application/json,.json" hidden data-ref="importFile" aria-label="Profile file to import">
    </div>
    <p class="hint">One press sets the look, platform, tier, lights, voice and framing.</p>
    <div class="pr-gallery" role="list" aria-label="Profiles" data-ref="gallery"></div>
    <div class="pr-result" role="status" aria-live="polite" data-ref="result" hidden></div>
    <form class="pr-save" data-ref="saveForm" autocomplete="off">
      <label class="pr-label" for="pr-name">Save the current setup as a profile</label>
      <div class="pr-save-row">
        <input id="pr-name" class="text-input" data-ref="saveName" type="text" maxlength="40" placeholder="Name, e.g. Friday show" aria-label="Profile name">
        <ss-button data-ref="saveBtn" icon="check" disabled>Save profile</ss-button>
      </div>
      <p class="hint" data-ref="saveNote">Takes the look, platform and tier chosen below, the lights, the voice preset and the framing mode as they are now.</p>
    </form>
  </ss-card>

  <ss-card class="pr-card" heading="Platform and tier" icon="broadcast" data-ref="streamCard">
    <div slot="actions" data-ref="streamPill"></div>
    <div class="pr-label" id="pr-platform-l">Platform</div>
    <div class="pr-platforms" role="radiogroup" aria-labelledby="pr-platform-l" data-ref="platforms"></div>
    <div class="pr-label" id="pr-tier-l">Bandwidth tier</div>
    <div class="pr-tiers" data-ref="tierWrap">
      <div class="seg" role="radiogroup" aria-labelledby="pr-tier-l" data-ref="tiers"></div>
      <p class="hint" data-ref="tierNote"></p>
    </div>
    <div class="pr-plan" data-ref="plan" aria-live="polite"></div>
  </ss-card>

  <ss-card class="pr-card" heading="Looks" icon="camera" data-ref="looksCard">
    <div slot="actions" class="pr-count" data-ref="lookCount"></div>
    <div class="pr-filter seg" role="group" aria-label="Look category" data-ref="categories"></div>
    <div class="pr-looks" role="list" aria-label="Looks" data-ref="looks"></div>
  </ss-card>
</div>`;

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const kbps = (v) => (v >= 1000 ? `${(v / 1000).toFixed(v % 1000 ? 1 : 0)} Mbps` : `${v} kbps`);
const shortName = (name) => String(name || '').replace(/^StreamStack Voice\s*/, '') || 'Standard';

/** A file the user downloads, built in the page (nothing is uploaded anywhere). */
function download(filename, payload) {
  const url = URL.createObjectURL(new Blob([`${JSON.stringify(payload, null, 2)}\n`], { type: 'application/json' }));
  const a = h('a', { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const fileSlug = (name) => name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'profile';
const portable = (p) => ({
  name: p.name, blurb: p.blurb || '', icon: p.icon || null, look: p.look, platform: p.platform, tier: p.tier,
  lighting: p.lighting, voice: p.voice, framing: p.framing,
});

export async function mount(host, ctx) {
  const { store, act, registerCommands, icon } = ctx;
  await ctx.ensureCss(new URL('./common.css', import.meta.url).href);
  await ctx.ensureCss(new URL('./presets.css', import.meta.url).href);
  document.title = 'Presets · StreamStack';
  host.innerHTML = TEMPLATE;
  const R = {};
  host.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  const off = [];
  const sub = (selector, cb) => off.push(store.subscribe(selector, cb));
  let catalog = null;
  let category = 'all';
  const busy = new Set();
  let disposed = false;

  const platformOf = (id) => catalog?.platforms.find((p) => p.id === id);
  const tierOf = (id) => catalog?.tiers.find((t) => t.id === id);

  // ---------- profiles ----------
  const chip = (text, title) => h('span', { class: 'pr-chip', title }, text);

  function renderProfiles() {
    const { profiles, profile: applied } = store.state.presets;
    renderList(R.gallery, profiles, (p) => (p.builtin ? `b:${p.id}` : `u:${p.name}`), (p) => {
      const card = h('div', { class: 'pr-profile', role: 'listitem' });
      const main = h('button', { type: 'button', class: 'pr-profile-main' },
        h('span', { class: 'pr-profile-head' }, h('span', { class: 'pr-profile-icon' }), h('span', { class: 'pr-profile-name' }),
          h('span', { class: 'pr-on' }, icon('check'), 'Applied')),
        h('span', { class: 'pr-profile-blurb' }),
        h('span', { class: 'pr-chips' }));
      main.addEventListener('click', () => applyProfile(card._profile));
      const tools = h('span', { class: 'pr-profile-tools' });
      const exp = h('ss-button', { class: 'pr-tool', size: 'sm', variant: 'ghost', icon: 'save', 'data-role': 'export' }, 'Export');
      exp.addEventListener('click', () => exportOne(card._profile));
      const del = h('ss-button', { class: 'pr-tool', size: 'sm', variant: 'ghost', icon: 'trash', hold: '800', 'data-role': 'delete' }, 'Delete');
      del.addEventListener('click', () => deleteProfile(card._profile));
      tools.append(exp, del);
      card.append(main, tools);
      return card;
    }, (card, p) => {
      card._profile = p;
      const main = card.firstElementChild;
      main.querySelector('.pr-profile-name').textContent = p.name;
      main.querySelector('.pr-profile-icon').replaceChildren(icon(p.icon || (p.builtin ? 'sliders' : 'save')));
      main.querySelector('.pr-profile-blurb').textContent = p.blurb || (p.builtin ? '' : 'Saved by you.');
      main.setAttribute('aria-label', `Apply profile ${p.name}`);
      main.setAttribute('aria-pressed', String(applied === p.name));
      card.toggleAttribute('data-current', applied === p.name);
      card.toggleAttribute('data-busy', busy.has(p.name));
      card.toggleAttribute('data-yours', !p.builtin);
      const chips = [];
      if (p.look) chips.push(chip(p.look, 'Look'));
      if (p.platform) chips.push(chip(`${platformOf(p.platform)?.name ?? p.platform} · ${tierOf(p.tier)?.name ?? p.tier}`, 'Platform and tier'));
      if (p.lighting) chips.push(chip(p.lighting, 'Lighting'));
      if (p.voice) chips.push(chip(shortName(p.voice), 'Voice preset'));
      if (p.framing) chips.push(chip(`Framing ${p.framing}`, 'Framing mode'));
      main.querySelector('.pr-chips').replaceChildren(...chips);
      const tools = card.querySelector('.pr-profile-tools');
      tools.hidden = p.builtin;
      const exp = tools.querySelector('[data-role="export"]');
      exp.setAttribute('aria-label', `Export profile ${p.name}`);
      exp.setAttribute('title', `Export “${p.name}” as a file`);
      const del = tools.querySelector('[data-role="delete"]');
      del.setAttribute('aria-label', `Hold to delete profile ${p.name}`);
      del.setAttribute('title', `Hold to delete “${p.name}”`);
    });
    R.exportBtn.toggleAttribute('disabled', !profiles.some((p) => !p.builtin));
  }

  async function applyProfile(p) {
    busy.add(p.name);
    renderProfiles();
    const res = await act('presets.profile.apply', p.builtin ? { id: p.id } : { name: p.name }, { quiet: true });
    busy.delete(p.name);
    if (disposed) return;
    renderProfiles();
    const steps = store.state.presets.last_apply?.steps ?? [];
    const level = steps.some((x) => x.status !== 'ok') ? 'warn' : 'success';
    if (res?.message) toast(res.message, { level, timeout: 4000, id: 'profile-apply' });
  }

  async function deleteProfile(p) {
    await act('presets.profile.delete', { name: p.name });
  }

  function exportOne(p) {
    download(`${fileSlug(p.name)}.json`, { schema: 'streamstack.profile/1', profile: portable(p) });
    toast.success(`Exported ${p.name}`, { timeout: 2500 });
  }

  R.exportBtn.addEventListener('click', () => {
    const mine = store.state.presets.profiles.filter((p) => !p.builtin);
    if (!mine.length) return;
    download('streamstack-profiles.json', { schema: 'streamstack.profiles/1', profiles: mine.map(portable) });
    toast.success(`Exported ${plural(mine.length, 'profile')}`, { timeout: 2500 });
  });

  R.importBtn.addEventListener('click', () => R.importFile.click());
  R.importFile.addEventListener('change', async () => {
    const file = R.importFile.files?.[0];
    R.importFile.value = '';
    if (!file) return;
    if (file.size > MAX_IMPORT_BYTES) { toast.error(`That file is too large for a profile file (at most ${MAX_IMPORT_BYTES / 1024} KiB).`); return; }
    let document_;
    try {
      document_ = JSON.parse(await file.text());
    } catch (err) {
      toast.error(`${file.name} is not valid JSON.`);
      return;
    }
    const res = await act('presets.profile.import', { document: document_ }, { quiet: true });
    if (res?.message) toast.success(res.message, { timeout: 4000 });
  });

  // ---------- the last apply, part by part ----------
  function renderResult() {
    const last = store.state.presets.last_apply;
    R.result.hidden = !last;
    if (!last) return;
    R.result.replaceChildren(
      h('strong', {}, `${last.profile}: `),
      ...last.steps.map((s) => h('span', { class: 'pr-step', 'data-status': s.status, title: s.message || '' },
        h('span', { class: 'pr-step-dot', 'aria-hidden': 'true' }),
        `${s.label}: ${STATUS_WORDS[s.status] ?? s.status}`,
        s.status === 'failed' && s.message ? h('small', {}, ` (${s.message})`) : null)),
    );
  }

  // ---------- save the current setup ----------
  function currentSetup(name) {
    const s = store.state;
    const current = s.video.looks.find((l) => sameLook(l.values, s.video.look));
    const { platform, tier } = s.presets;
    return {
      name,
      look: current?.name ?? null,
      platform: platform && tier ? platform : null,
      tier: platform && tier ? tier : null,
      lighting: s.lighting?.preset ?? null,
      voice: s.audio.chain.preset ?? null,
      framing: s.framing?.mode ?? null,
    };
  }
  R.saveName.addEventListener('input', () => { R.saveBtn.toggleAttribute('disabled', !R.saveName.value.trim()); });
  R.saveForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = R.saveName.value.trim();
    if (!name) return;
    const res = await act('presets.profile.save', { profile: currentSetup(name) }, { quiet: true });
    if (res?.ok) {
      toast.success(res.message || `Saved ${name}`, { timeout: 3000 });
      R.saveName.value = '';
      R.saveBtn.setAttribute('disabled', '');
    }
  });
  R.saveBtn.addEventListener('click', () => R.saveForm.requestSubmit());

  // ---------- platform and tier ----------
  function platformLine(p) {
    const top = `${p.max_short_side >= 2160 ? '4K' : `${p.max_short_side}p`}${p.max_fps}`;
    const codecs = p.codecs.length ? p.codecs.map((c) => CODEC_NAMES[c]).join(' ') : 'raw frames';
    return `${top} · ${codecs}`;
  }

  function renderPlatforms() {
    if (!catalog) return;
    const { platform } = store.state.presets;
    renderList(R.platforms, catalog.platforms, (p) => p.id, (p) => {
      const b = h('button', { type: 'button', class: 'pr-platform', role: 'radio' },
        h('span', { class: 'pr-platform-name' }), h('span', { class: 'pr-platform-line' }), h('span', { class: 'pr-platform-tags' }));
      b.addEventListener('click', () => act('presets.platform', { id: b.dataset.key }, { quiet: true }));
      return b;
    }, (b, p) => {
      b.querySelector('.pr-platform-name').textContent = p.name;
      b.querySelector('.pr-platform-line').textContent = platformLine(p);
      b.querySelector('.pr-platform-tags').replaceChildren(
        h('span', { class: 'pill', 'data-state': VERIFIED_STATE[p.verified] }, VERIFIED_WORDS[p.verified]),
        ...(p.orientation === 'portrait' ? [h('span', { class: 'pill' }, '9:16')] : []),
      );
      b.setAttribute('aria-checked', String(platform === p.id));
      b.setAttribute('aria-label', p.name);
      b.tabIndex = platform === p.id || (!platform && p.id === catalog.platforms[0].id) ? 0 : -1;
    });
  }

  function renderTiers() {
    if (!catalog) return;
    const { tier, platform } = store.state.presets;
    renderList(R.tiers, catalog.tiers, (t) => t.id, (t) => {
      const b = h('button', { type: 'button', role: 'radio' });
      b.addEventListener('click', () => act('presets.tier', { id: b.dataset.key }, { quiet: true }));
      return b;
    }, (b, t) => {
      b.textContent = t.name;
      b.setAttribute('aria-checked', String(tier === t.id));
      b.setAttribute('aria-pressed', String(tier === t.id));
      b.setAttribute('title', `${t.blurb} Needs about ${t.min_uplink_mbps} Mbps of upload.`);
      b.tabIndex = tier === t.id || (!tier && t.id === 'balanced') ? 0 : -1;
    });
    const t = tierOf(tier);
    const p = platformOf(platform);
    R.tierNote.textContent = t
      ? `${t.blurb} Needs about ${t.min_uplink_mbps} Mbps of upload.${p && p.tier_policy === 'ignore' ? ' A local recording is not limited by the uplink, so the tier does not apply to it.' : ''}`
      : 'Pick how much of your uplink the stream may use.';
  }

  function row(label, ...value) {
    return [h('dt', {}, label), h('dd', {}, ...value)];
  }

  function renderPlan() {
    if (!catalog) return;
    const { platform, tier, plan, stream } = store.state.presets;
    const p = platformOf(platform);
    if (!p || !plan) {
      R.plan.replaceChildren(h('p', { class: 'hint pr-plan-empty' }, 'Pick a platform to see its resolution, codecs, bitrate, audio and transport.'));
      R.streamPill.replaceChildren();
      return;
    }
    const v = plan.video;
    const a = plan.audio;
    const codecs = v.codecs.length
      ? v.codecs.map((c, i) => h('span', { class: 'pr-codec', 'data-first': i === 0 ? '' : null },
        CODEC_NAMES[c.codec], ' ', h('small', {}, c.bitrate_kbps ? kbps(c.bitrate_kbps) : `quality ${c.quality}`)))
      : [h('span', {}, 'None: the calling app encodes the camera')];
    const dl = h('dl', { class: 'pr-spec' },
      ...row('Resolution', `${v.width}×${v.height} at ${v.fps} fps`),
      ...row('Codecs, best first', h('span', { class: 'pr-codecs' }, ...codecs)),
      ...row('Rate control', v.rate_control === 'none' ? 'Not applicable' : v.rate_control.toUpperCase(),
        v.keyframe_interval_s ? ` · keyframe every ${v.keyframe_interval_s} s (${v.gop_frames} frames)` : ''),
      ...row('Audio', `${AUDIO_NAMES[a.codec]}${a.bitrate_kbps ? ` ${a.bitrate_kbps} kbps` : ''}, ${a.sample_rate / 1000} kHz ${a.channels === 1 ? 'mono' : 'stereo'}`),
      ...row('Loudness', a.loudness_lufs === null ? 'No target (leave the mix as it is)' : `${a.loudness_lufs} LUFS, true peak ${a.true_peak_dbtp} dBTP`),
      ...row('Transport', plan.transport.map((t) => TRANSPORT_NAMES[t]).join(', then ')),
      ...row('Upload needed', plan.uplink_mbps_needed ? `about ${plan.uplink_mbps_needed} Mbps (with headroom)` : 'No uplink used'));
    const why = h('div', { class: 'pr-why' },
      h('p', {}, p.rationale),
      h('p', { class: 'pr-source' }, h('span', { class: 'pill', 'data-state': VERIFIED_STATE[p.verified] }, VERIFIED_WORDS[p.verified]),
        h('a', { href: p.source, target: '_blank', rel: 'noopener noreferrer' }, 'Source'), ` · checked ${catalog.checked}`),
      p.uncertain.length ? h('details', { class: 'pr-uncertain' }, h('summary', {}, `What is uncertain (${p.uncertain.length})`),
        h('ul', {}, ...p.uncertain.map((u) => h('li', {}, u)))) : null);
    R.plan.replaceChildren(h('h3', { class: 'pr-plan-title' }, `${p.name} · ${tierOf(tier)?.name ?? ''}`), dl, why);
    const word = { applied: 'Sent to stream backend', pending: 'stream.set pending', failed: 'Stream backend refused', idle: '' }[stream.status] ?? '';
    const state = { applied: 'ok', pending: 'info', failed: 'fail' }[stream.status];
    R.streamPill.replaceChildren(word ? h('span', { class: 'pill', 'data-state': state, title: stream.message || '' }, word) : '');
  }

  // ---------- looks ----------
  function renderCategories() {
    if (!catalog) return;
    const looks = store.state.video.looks;
    const counts = new Map();
    for (const l of looks) counts.set(l.category || 'yours', (counts.get(l.category || 'yours') || 0) + 1);
    const entries = [{ id: 'all', label: 'All', n: looks.length }, ...catalog.categories.filter((c) => counts.has(c.id)).map((c) => ({ ...c, n: counts.get(c.id) }))];
    if (!entries.some((c) => c.id === category)) category = 'all';
    renderList(R.categories, entries, (c) => c.id, (c) => {
      const b = h('button', { type: 'button' });
      b.addEventListener('click', () => { category = b.dataset.key; renderCategories(); renderLooks(); });
      return b;
    }, (b, c) => {
      b.textContent = `${c.label} ${c.n}`;
      b.setAttribute('aria-pressed', String(category === c.id));
    });
  }

  function renderLooks() {
    const { looks, look: current } = store.state.video;
    const shown = looks.filter((l) => category === 'all' || (l.category || 'yours') === category);
    R.lookCount.textContent = plural(shown.length, 'look');
    renderList(R.looks, shown, (l) => l.name, (l) => {
      const card = h('div', { class: 'pr-look', role: 'listitem' });
      const main = h('button', { type: 'button', class: 'pr-look-main' },
        h('span', { class: 'pr-look-art' },
          h('img', { alt: '', width: 128, height: 80, loading: 'lazy', decoding: 'async', hidden: true }),
          h('canvas', { width: 128, height: 80, 'aria-hidden': 'true', hidden: true }),
          h('span', { class: 'pr-on' }, icon('check'), 'Current'),
          h('span', { class: 'pr-lut', title: 'Uses a 3D colour table (LUT)' }, 'LUT')),
        h('span', { class: 'pr-look-name' }), h('span', { class: 'pr-look-blurb' }));
      main.addEventListener('click', () => applyLook(card.dataset.key));
      card.append(main);
      return card;
    }, (card, l) => {
      const main = card.firstElementChild;
      main.querySelector('.pr-look-name').textContent = l.name;
      main.querySelector('.pr-look-blurb').textContent = l.blurb || '';
      main.setAttribute('aria-label', `Apply look ${l.name}`);
      main.setAttribute('title', l.blurb || l.name);
      const img = main.querySelector('img');
      const canvas = main.querySelector('canvas');
      if (l.thumb) {
        if (img.getAttribute('src') !== l.thumb) img.setAttribute('src', l.thumb);
        img.hidden = false; canvas.hidden = true;
      } else {
        img.hidden = true; canvas.hidden = false;
        const sig = JSON.stringify(l.values);
        if (canvas.dataset.sig !== sig) { canvas.dataset.sig = sig; drawSwatch(canvas, l.values); }
      }
      main.querySelector('.pr-lut').hidden = !l.lut;
      const isCurrent = sameLook(l.values, current);
      card.toggleAttribute('data-current', isCurrent);
      main.setAttribute('aria-pressed', String(isCurrent));
    });
  }

  function applyLook(name) {
    return act('video.looks.apply', { name });
  }

  // ---------- wiring ----------
  sub((s) => [s.presets.profiles, s.presets.profile], renderProfiles);
  sub((s) => s.presets.last_apply, renderResult);
  sub((s) => [s.presets.platform, s.presets.tier, s.presets.plan, s.presets.stream], () => { renderPlatforms(); renderTiers(); renderPlan(); });
  sub((s) => [s.video.looks, s.video.look], () => { renderCategories(); renderLooks(); });

  // radio groups move with the arrow keys (the platform cards and the tier buttons)
  for (const group of [R.platforms, R.tiers]) {
    group.addEventListener('keydown', (e) => {
      const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
      if (!(e.key in keys)) return;
      const items = Array.from(group.querySelectorAll('[role="radio"]'));
      const at = items.indexOf(document.activeElement);
      if (at < 0) return;
      e.preventDefault();
      const next = items[(at + keys[e.key] + items.length) % items.length];
      next.focus();
      next.click();
    });
  }

  renderProfiles();
  renderResult();
  renderLooks();

  off.push(registerCommands?.(() => {
    const cmds = [];
    for (const p of store.state.presets.profiles) {
      cmds.push({ id: `Profiles:${p.name}`, group: 'Profiles', title: `Profile: ${p.name}`, icon: 'play', keywords: 'profile preset one click setup scene',
        run: () => applyProfile(p) });
    }
    return cmds;
  }));

  try {
    catalog = await getJson('api/presets/catalog');
  } catch (err) {
    console.error('presets catalog failed', err);
    R.plan.replaceChildren(h('p', { class: 'hint', 'data-state': 'error' }, `Could not load the platform catalog: ${err.message}`));
    R.platforms.replaceChildren();
    return () => { disposed = true; off.forEach((fn) => fn?.()); };
  }
  if (disposed) return undefined;
  renderPlatforms();
  renderTiers();
  renderPlan();
  renderCategories();
  renderProfiles();

  return () => {
    disposed = true;
    for (const fn of off) if (typeof fn === 'function') fn();
  };
}
