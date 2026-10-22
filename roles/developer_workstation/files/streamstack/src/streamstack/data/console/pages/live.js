// pages/live.js — THE COCKPIT. Everything needed mid-stream, no scrolling at 1280×720.
//
// Layout (desktop): left = program picture, scene pads, look faders; right = microphone strip
// (gain fader, mic + voice meters, MUTE, PRIVACY), voice presets, auto director; bottom = status rail.
// Narrow screens stack the same cards with the mic strip first (a phone is a remote control).
//
// Data flow: one static DOM, then fine-grained store subscriptions that touch only what changed.
// Controls call ops/act; the store gives optimistic values back immediately.

import { h, renderList, signed, clockTime } from '../core/util.js';
import { iconHTML } from '../components/icons.js';
import { toggleLights, toggleMute, togglePrivacy, toggleBypass, toggleVirtualcam, selectScene, selectPreset, toggleAuto, setRecord, startStudio } from '../core/ops.js';
import { setUi, onUi, ui } from '../core/ui.js';
import { mountReviveBanner, reviveView } from '../core/revive.js';
import { mountFraming } from './framing.js';
import { mountBlur, blurCommands } from './blur.js';

const LOOKS = [
  { key: 'brightness', label: 'Brightness', icon: 'sun' },
  { key: 'warmth', label: 'Warmth', icon: 'warmth' },
  { key: 'contrast', label: 'Contrast', icon: 'contrast' },
  { key: 'saturation', label: 'Saturation', icon: 'saturation' },
];

const CAMERA_FOR_CALLS = {
  graded: { value: 'Graded', state: 'ok' },
  raw: { value: 'Raw', state: 'info' },
  'obs-idle': { value: 'OBS idle', state: 'warn' },
  off: { value: 'Off', state: 'off' },
};

const OBS_REASON = {
  offline: { reason: 'OBS is not running', fix: 'Start studio' },
  'no-websocket': { reason: 'OBS is running, but its WebSocket server is off (Tools → WebSocket Server Settings)', fix: '' },
  error: { reason: 'OBS reported an error', fix: '' },
};

const TEMPLATE = `
<div class="cockpit">
  <div class="col col-main">
    <ss-card bare class="area-preview" data-ref="previewCard">
      <div class="pv-cell">
        <ss-preview data-ref="pv" source="program" fps="12" label="Program picture, what the audience sees">
          <ss-button slot="empty" variant="primary" icon="power" data-ref="pvFix" hidden>Start studio</ss-button>
          <div slot="badges" class="badges" data-ref="badges"></div>
          <span slot="corner" class="tc" data-ref="tc"></span>
          <div slot="footer" class="pv-tools">
            <ss-button size="sm" variant="ghost" icon="grid" data-ref="grid" title="Thirds grid (G)">Grid</ss-button>
            <span class="spacer"></span>
            <ss-button size="sm" icon="vcam" data-ref="vcam">Virtual camera</ss-button>
            <ss-button size="sm" icon="record" data-ref="rec">Record</ss-button>
          </div>
        </ss-preview>
      </div>
    </ss-card>
    <ss-card bare inline-reason class="area-scenes" data-ref="scenesCard">
      <div class="scene-row"><span class="eyebrow">Scenes</span><div class="scene-pads" data-ref="scenes" role="group" aria-label="Scenes"></div></div>
      <div class="framing-row" data-ref="framingHost"></div>
    </ss-card>
    <ss-card inline-reason class="area-look" heading="Look" icon="sliders" data-ref="lookCard">
      <ss-button slot="actions" size="sm" variant="ghost" icon="refresh" data-ref="lookReset">Reset look</ss-button>
      <div class="looks" data-ref="looks"></div>
      <div class="bl-row" data-ref="blurRow" role="group" aria-label="Background blur" hidden></div>
    </ss-card>
  </div>

  <div class="col col-side">
    <ss-card class="area-mic" heading="Microphone" icon="mic" data-ref="micCard">
      <span slot="actions" class="dev" data-ref="micName"></span>
      <div class="revive" data-ref="revive"></div>
      <div class="mic-body">
        <ss-fader data-ref="gain" label="Gain" unit="dB" step="0.5" default="0" signed decimals="1" accent="green"></ss-fader>
        <div class="mic-meters">
          <ss-meter source="mic" label="Mic" target="-24,-12"></ss-meter>
          <ss-meter source="voice" label="Voice" target="-24,-12" scale="none"></ss-meter>
        </div>
        <div class="mic-actions">
          <ss-pad big solid tone="red" icon="mic" label="Mute mic" sub="Mic is live" hotkey="M" data-ref="mute"></ss-pad>
          <ss-pad big row solid tone="amber" icon="eye-off" label="Privacy" sub="Cut camera to Be Right Back" hotkey="P" data-ref="privacy"></ss-pad>
        </div>
      </div>
    </ss-card>

    <ss-card class="area-voice" heading="Voice" icon="wave" data-ref="voiceCard">
      <ss-toggle slot="actions" label="Bypass" tone="amber" data-ref="bypass" title="Effects bypass, A/B compare (B)"></ss-toggle>
      <div class="presets" data-ref="presets" role="group" aria-label="Voice presets"></div>
      <p class="preset-note" data-ref="presetNote"></p>
    </ss-card>

    <ss-card class="area-auto" heading="Auto director" icon="auto" data-ref="autoCard">
      <ss-toggle slot="actions" label="Director" data-ref="autoToggle"></ss-toggle>
      <p class="note" data-ref="autoNote"></p>
      <div class="auto-row">
        <span class="note-time" data-ref="autoTime"></span>
        <ss-button size="sm" icon="undo" variant="ghost" data-ref="autoUndo">Undo changes</ss-button>
        <ss-button size="sm" icon="auto" data-ref="autoNow">Ask now</ss-button>
      </div>
    </ss-card>
  </div>

  <nav class="status-rail" aria-label="Status" data-ref="status"></nav>
</div>`;

export async function mount(host, ctx) {
  const { store, act, actStream, flushStream, registerKeys, registerCommands } = ctx;
  await ctx.ensureCss(new URL('./live.css', import.meta.url).href);
  await ctx.ensureCss(new URL('./framing.css', import.meta.url).href);
  await ctx.ensureCss(new URL('./blur.css', import.meta.url).href);
  await ctx.ensureCss(new URL('../core/revive.css', import.meta.url).href);
  document.title = 'Live · StreamStack';
  host.innerHTML = TEMPLATE;
  const R = {};
  host.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  const off = [];
  const sub = (selector, cb) => off.push(store.subscribe(selector, cb));
  off.push(mountFraming(R.framingHost, ctx, { compact: true }));

  // ---------- availability of everything that needs OBS ----------
  sub((s) => [s.video.obs, s.video.obs_error, s.studio.busy, s.studio.running], ([obs, err, busy]) => {
    const r = OBS_REASON[obs];
    let reason = '';
    let fix = '';
    if (r) {
      reason = busy ? 'Starting the studio…' : (obs === 'error' && err ? err : r.reason);
      fix = busy ? '' : r.fix;
    }
    for (const card of [R.scenesCard, R.lookCard]) {
      if (reason) card.setAttribute('reason', reason); else card.removeAttribute('reason');
    }
    if (reason) R.pv.setAttribute('reason', reason); else R.pv.removeAttribute('reason');
    R.pvFix.hidden = !fix;
    R.privacy.toggleAttribute('disabled', !!reason);
    R.vcam.toggleAttribute('disabled', !!reason);
    R.rec.toggleAttribute('disabled', !!reason);
  });
  for (const card of [R.voiceCard]) card.addEventListener('fix', () => startStudio());
  R.pvFix.addEventListener('click', () => startStudio());

  // ---------- program picture, badges, timecode ----------
  sub((s) => [s.video.streaming, s.video.recording, s.video.virtualcam, s.video.privacy], ([streaming, rec, vcam, privacy]) => {
    const chips = [];
    const onAir = streaming.active || vcam;
    chips.push(onAir
      ? { id: 'air', state: 'live', label: 'ON AIR', icon: 'broadcast', pulse: true }
      : { id: 'air', state: 'off', label: 'OFF AIR', icon: 'dot' });
    if (rec.active) chips.push({ id: 'rec', state: rec.paused ? 'warn' : 'live', label: rec.paused ? 'REC PAUSED' : 'REC', icon: 'record', pulse: !rec.paused });
    if (vcam) chips.push({ id: 'vcam', state: 'info', label: 'VIRTUAL CAM', icon: 'vcam' });
    if (streaming.active) chips.push({ id: 'stream', state: 'info', label: 'STREAMING', icon: 'live' });
    if (privacy) chips.push({ id: 'privacy', state: 'warn', label: 'PRIVACY', icon: 'eye-off' });
    renderList(R.badges, chips, (c) => c.id, () => h('ss-status-chip', { size: 'md' }), (el, c) => {
      el.setAttribute('state', c.state);
      el.setAttribute('label', c.label);
      el.setAttribute('icon', c.icon);
      el.toggleAttribute('pulse', !!c.pulse);
    });
    R.pv.setAttribute('tally', onAir ? 'program' : 'off');
    R.tc.textContent = streaming.active ? (streaming.timecode || '') : (rec.active ? (rec.timecode || '') : '');
    R.rec.setAttribute('icon', 'record');
    R.rec.toggleAttribute('pressed', rec.active);
    R.rec.setAttribute('variant', rec.active ? 'danger' : 'default');
    R.rec.setAttribute('hold', rec.active ? '700' : '0');
    R.rec.textContent = rec.active ? 'Hold to stop recording' : 'Record';
    R.vcam.toggleAttribute('pressed', vcam);
  });
  R.vcam.addEventListener('click', () => toggleVirtualcam());
  R.rec.addEventListener('click', () => setRecord(!store.state.video.recording.active));
  R.grid.addEventListener('click', () => setUi('grid', !ui.grid));
  const syncGrid = () => { R.pv.toggleAttribute('grid', ui.grid); R.grid.toggleAttribute('pressed', ui.grid); };
  syncGrid();
  off.push(onUi((k) => { if (k === 'grid') syncGrid(); }));
  R.pv.addEventListener('dblclick', () => setUi('grid', !ui.grid));

  // ---------- scenes ----------
  sub((s) => [s.video.scenes, s.video.program_scene], ([scenes, program]) => {
    renderList(R.scenes, scenes, (sc) => sc.name, (sc) => {
      const pad = h('ss-pad', { compact: true });
      pad.addEventListener('click', () => selectScene(pad.dataset.key));
      return pad;
    }, (pad, sc) => {
      const i = scenes.indexOf(sc);
      pad.setAttribute('label', sc.name);
      pad.setAttribute('hotkey', i < 9 ? String(i + 1) : '');
      const live = sc.active || sc.name === program;
      pad.toggleAttribute('active', live);
      if (live) pad.setAttribute('tally', 'program'); else pad.removeAttribute('tally');
    });
  });

  // ---------- microphone ----------
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

  // A stalled microphone: banner over the mic card, red edge until it is back.
  off.push(mountReviveBanner(R.revive, ctx));
  sub((s) => s.audio.mic, (mic) => {
    const view = reviveView(mic);
    if (view === 'stalled' || view === 'failed') R.micCard.setAttribute('tone', 'live');
    else if (view === 'reviving') R.micCard.setAttribute('tone', 'warn');
    else R.micCard.removeAttribute('tone');
  });

  sub((s) => [s.video.privacy, s.video.privacy_scene], ([on, scene]) => {
    R.privacy.toggleAttribute('active', on);
    R.privacy.setAttribute('label', on ? 'PRIVACY ON' : 'Privacy');
    R.privacy.setAttribute('sub', on ? `${scene} is on air. Press P to return` : `Cut camera to ${scene}`);
  });
  R.privacy.addEventListener('click', () => togglePrivacy());

  // ---------- voice ----------
  sub((s) => s.audio.chain, (chain) => {
    renderList(R.presets, chain.presets, (p) => p.name, (p) => {
      const pad = h('ss-pad', { compact: true });
      pad.addEventListener('click', () => selectPreset(pad.dataset.key));
      return pad;
    }, (pad, p) => {
      pad.setAttribute('label', p.label.replace(/^Voice (?=\S)/, ''));
      pad.setAttribute('title', p.unavailable || p.blurb || '');
      pad.toggleAttribute('active', p.name === chain.preset);
      pad.toggleAttribute('disabled', !chain.available || !!p.unavailable);
    });
    const active = chain.presets.find((p) => p.name === chain.preset);
    R.presetNote.textContent = chain.bypass
      ? 'Effects bypassed: you hear the raw microphone.'
      : (active ? `${active.label}: ${active.blurb || ''}` : '');
    R.bypass.toggleAttribute('checked', chain.bypass);
    R.bypass.toggleAttribute('disabled', !chain.available);
    R.voiceCard.toggleAttribute('tone-warn', chain.bypass);
    if (!chain.available) R.voiceCard.setAttribute('reason', 'Easy Effects is not installed');
    else if (!chain.running) { R.voiceCard.setAttribute('reason', 'The voice chain is not running'); R.voiceCard.setAttribute('fix', 'Start studio'); }
    else { R.voiceCard.removeAttribute('reason'); R.voiceCard.removeAttribute('fix'); }
  });
  R.bypass.addEventListener('change', () => toggleBypass());

  // ---------- look ----------
  for (const l of LOOKS) {
    const row = h('div', { class: 'lk' }, h('ss-fader', {
      orientation: 'horizontal', label: l.label, step: '0.01', default: '0', bipolar: true, ticks: 'none', signed: true,
      'data-look': l.key, 'data-ref': `look-${l.key}`,
    }));
    const f = row.querySelector('ss-fader');
    f.formatter = (v) => signed(v * 100, 0);
    f.addEventListener('input', (e) => actStream('video.look', { values: { [l.key]: e.detail.value } }));
    f.addEventListener('change', () => flushStream('video.look'));
    R.looks.append(row);
  }
  const lookFader = (key) => R.looks.querySelector(`[data-look="${key}"]`);
  sub((s) => s.video.limits, (limits) => {
    for (const l of LOOKS) {
      const f = lookFader(l.key);
      const [lo, hi] = limits[l.key] || [-1, 1];
      f.setAttribute('min', lo);
      f.setAttribute('max', hi);
    }
  });
  sub((s) => s.video.look, (look) => { for (const l of LOOKS) lookFader(l.key).value = look[l.key] ?? 0; });
  R.lookReset.addEventListener('click', () => act('video.look.reset'));

  // ---------- background blur (pages/blur.js; the row stays hidden while the plugin is missing) ----------
  off.push(mountBlur(R.blurRow, ctx, { variant: 'compact' }));
  off.push(registerCommands(() => blurCommands(ctx)));

  // ---------- auto director ----------
  sub((s) => s.auto, (auto) => {
    R.autoToggle.toggleAttribute('checked', auto.running);
    R.autoNote.textContent = auto.busy ? 'Claude is looking at the picture…' : (auto.last_note || 'No changes yet. The director watches picture and voice and adjusts gently.');
    R.autoNote.toggleAttribute('data-dim', !auto.running && !auto.busy);
    const dir = auto.director || {};
    const persona = (dir.personas || []).find((p) => p.id === dir.persona);
    const where = persona && dir.time ? ` · ${persona.name}, ${dir.time.sun.slot_label}` : '';
    R.autoTime.textContent = (auto.last_time && !auto.busy ? `Last note ${clockTime(auto.last_time)}` : (auto.running ? 'Director on' : 'Director off')) + where;
    R.autoUndo.toggleAttribute('disabled', !auto.can_undo);
    R.autoNow.toggleAttribute('busy', auto.busy);
  });
  R.autoToggle.addEventListener('change', () => toggleAuto());
  R.autoNow.addEventListener('click', () => act('auto.once'));
  R.autoUndo.addEventListener('click', () => act('auto.reset'));

  // ---------- lights quick control (in the status rail; hidden when no Litra is connected) ----------
  const lq = h('div', { class: 'lq', role: 'group', 'aria-label': 'Lights', hidden: true });
  const lqToggle = h('ss-button', { size: 'sm', icon: 'lights', title: 'Every light on / off (L)' }, 'Lights');
  const lqFader = h('ss-fader', { orientation: 'horizontal', label: 'Light level', unit: '%', min: 1, max: 100, step: 1, default: 70, ticks: 'none', readout: 'none', accent: 'amber' });
  lq.append(lqToggle, lqFader);
  R.status.append(lq);
  lqToggle.addEventListener('click', () => toggleLights());
  lqFader.addEventListener('input', (e) => actStream('lighting.brightness', { id: 'all', percent: e.detail.value }));
  lqFader.addEventListener('change', () => {
    flushStream('lighting.brightness');
    if (!store.state.lighting.master.on) act('lighting.power', { id: 'all', on: true });
  });
  sub((s) => [s.lighting.master, s.lighting.lights], ([m, lights]) => {
    lq.hidden = !lights.some((l) => l.connected);
    lqToggle.setAttribute('pressed', String(m.on));
    lqFader.value = m.brightness;
  });

  // ---------- status rail ----------
  sub((s) => [s.studio.camera_for_calls, s.studio.voice_chain, s.audio.chain.bypass, s.video.obs, s.system.cpu_percent, s.health.summary, s.health.checks, s.version, s.studio.camera_label], ([cam, voice, bypass, obs, cpu, health, checks, version, camLabel]) => {
    const c = CAMERA_FOR_CALLS[cam] || CAMERA_FOR_CALLS.off;
    const issues = checks.filter((k) => k.state !== 'ok').length;
    const items = [
      { id: 'cam', label: 'Camera for calls', value: c.value, state: c.state, icon: 'camera', href: '#/camera' },
      { id: 'voice', label: 'Voice chain', value: !voice ? 'Off' : bypass ? 'Bypassed' : 'On', state: !voice ? 'fail' : bypass ? 'warn' : 'ok', icon: 'wave', href: '#/audio' },
      { id: 'obs', label: 'OBS', value: obs === 'connected' ? 'Connected' : obs === 'offline' ? 'Not running' : obs === 'no-websocket' ? 'No WebSocket' : 'Error', state: obs === 'connected' ? 'ok' : obs === 'offline' ? 'fail' : 'warn', icon: 'scenes', href: '#/system' },
      { id: 'cpu', label: 'CPU', value: cpu === null ? '–' : `${Math.round(cpu)}%`, state: cpu === null ? 'off' : cpu >= 90 ? 'fail' : cpu >= 75 ? 'warn' : 'ok', icon: 'system', href: '#/system' },
      { id: 'health', label: 'Health', value: health === 'ok' ? 'All good' : `${issues || 1} ${health === 'fail' ? 'to fix' : 'to check'}`, state: health, icon: health === 'ok' ? 'check' : 'alert', href: '#/system' },
    ];
    renderList(R.status, items, (i) => i.id, () => h('ss-status-chip', { size: 'lg' }), (el, i) => {
      el.setAttribute('label', i.label);
      el.setAttribute('value', i.value);
      el.setAttribute('state', i.state);
      el.setAttribute('icon', i.icon);
      el.setAttribute('href', i.href);
    });
    let ver = R.status.querySelector('.ver');
    if (!ver) { ver = h('span', { class: 'ver' }); R.status.append(ver); }
    ver.textContent = version ? `StreamStack ${version}` : '';
  });

  return () => off.forEach((fn) => fn());
}
