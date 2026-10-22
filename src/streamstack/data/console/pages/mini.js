// pages/mini.js — the compact strip for a ~420×760 window next to a call: meter + mute, privacy, scenes, preset, status.
// Rendered without the app chrome (pages/index.js: chrome:false). Same ops as the Live page.

import { h, renderList } from '../core/util.js';
import { iconHTML } from '../components/icons.js';
import { mountReviveBanner } from '../core/revive.js';
import { toggleMute, togglePrivacy, selectScene, selectPreset } from '../core/ops.js';

const TEMPLATE = `
<div class="mini">
  <header class="m-head">
    <span class="brand-mark">${iconHTML('logo')}</span>
    <strong>StreamStack</strong>
    <span class="m-badges" data-ref="badges"></span>
    <a class="m-open" href="#/live" title="Open the full console" aria-label="Open the full console">${iconHTML('external')}</a>
  </header>
  <div class="m-mic">
  <ss-pad big solid tone="red" icon="mic" label="Mute mic" sub="Mic is live" hotkey="M" data-ref="mute"></ss-pad>
  <div class="m-level">
    <ss-meter source="mic" orientation="horizontal" label="Mic" target="-24,-12" scale="none" thickness="1.1"></ss-meter>
    <ss-fader data-ref="gain" orientation="horizontal" label="Gain" unit="dB" step="0.5" default="0" signed decimals="1" ticks="none" accent="green"></ss-fader>
  </div>
  <div class="revive" data-ref="revive"></div>
  </div>
  <ss-pad big row solid tone="amber" icon="eye-off" label="Privacy" sub="Cut camera to Be Right Back" hotkey="P" data-ref="privacy"></ss-pad>
  <ss-card bare inline-reason data-ref="scenesCard"><div class="m-scenes" data-ref="scenes" role="group" aria-label="Scenes"></div></ss-card>
  <div class="m-block"><span class="eyebrow">Voice</span><div class="m-presets" data-ref="presets" role="group" aria-label="Voice presets"></div></div>
  <nav class="m-status" data-ref="status" aria-label="Status"></nav>
</div>`;

export async function mount(host, ctx) {
  const { store, actStream, flushStream } = ctx;
  await ctx.ensureCss(new URL('./mini.css', import.meta.url).href);
  await ctx.ensureCss(new URL('../core/revive.css', import.meta.url).href);
  document.title = 'StreamStack mini';
  host.innerHTML = TEMPLATE;
  const R = {};
  host.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  const off = [];
  const sub = (selector, cb) => off.push(store.subscribe(selector, cb));

  sub((s) => s.audio.mic, (mic) => {
    R.mute.toggleAttribute('active', mic.muted);
    R.mute.setAttribute('icon', mic.muted ? 'mic-off' : 'mic');
    R.mute.setAttribute('label', mic.muted ? 'MUTED' : 'Mute mic');
    R.mute.setAttribute('sub', mic.muted ? 'Nobody can hear you. Press M' : 'Mic is live');
    R.gain.toggleAttribute('disabled', !mic.present);
  });
  R.mute.addEventListener('click', () => toggleMute());
  off.push(mountReviveBanner(R.revive, ctx, { compact: true }));

  sub((s) => s.audio.gain_range, ([lo, hi]) => { R.gain.setAttribute('min', lo); R.gain.setAttribute('max', hi); });
  sub((s) => s.audio.mic.gain_db, (v) => { R.gain.value = v; });
  R.gain.addEventListener('input', (e) => actStream('audio.gain', { db: e.detail.value }));
  R.gain.addEventListener('change', () => flushStream('audio.gain'));

  sub((s) => [s.video.privacy, s.video.privacy_scene, s.video.obs], ([on, scene, obs]) => {
    R.privacy.toggleAttribute('active', on);
    R.privacy.setAttribute('label', on ? 'PRIVACY ON' : 'Privacy');
    R.privacy.setAttribute('sub', on ? `${scene} is on air. Press P` : `Cut camera to ${scene}`);
    R.privacy.toggleAttribute('disabled', obs !== 'connected');
  });
  R.privacy.addEventListener('click', () => togglePrivacy());

  sub((s) => [s.video.scenes, s.video.program_scene, s.video.obs], ([scenes, program, obs]) => {
    if (obs !== 'connected') R.scenesCard.setAttribute('reason', obs === 'offline' ? 'OBS is not running' : 'OBS is unavailable');
    else R.scenesCard.removeAttribute('reason');
    renderList(R.scenes, scenes, (sc) => sc.name, () => {
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

  sub((s) => s.audio.chain, (chain) => {
    renderList(R.presets, chain.presets, (p) => p.name, () => {
      const pad = h('ss-pad', { compact: true });
      pad.addEventListener('click', () => selectPreset(pad.dataset.key));
      return pad;
    }, (pad, p) => {
      pad.setAttribute('label', p.label);
      pad.setAttribute('title', p.blurb || '');
      pad.toggleAttribute('active', p.name === chain.preset);
      pad.toggleAttribute('disabled', !chain.available);
    });
  });

  sub((s) => [s.video.streaming.active, s.video.recording.active, s.video.virtualcam, s.audio.chain.bypass, s.video.obs, s.studio.camera_for_calls], ([stream, rec, vcam, bypass, obs, cam]) => {
    const chips = [];
    if (stream || vcam) chips.push({ id: 'air', state: 'live', label: 'ON AIR', icon: 'broadcast', pulse: true });
    if (rec) chips.push({ id: 'rec', state: 'live', label: 'REC', icon: 'record', pulse: true });
    renderList(R.badges, chips, (c) => c.id, () => h('ss-status-chip', { size: 'sm' }), (el, c) => {
      el.setAttribute('state', c.state); el.setAttribute('label', c.label); el.setAttribute('icon', c.icon); el.toggleAttribute('pulse', !!c.pulse);
    });
    const items = [
      { id: 'obs', label: 'OBS', value: obs === 'connected' ? 'on' : 'off', state: obs === 'connected' ? 'ok' : 'fail', icon: 'scenes' },
      { id: 'cam', label: 'Cam', value: { graded: 'graded', raw: 'raw', 'obs-idle': 'idle', off: 'off' }[cam] || cam, state: cam === 'graded' ? 'ok' : cam === 'raw' ? 'info' : cam === 'off' ? 'off' : 'warn', icon: 'camera' },
      { id: 'fx', label: 'Voice', value: bypass ? 'bypass' : 'fx', state: bypass ? 'warn' : 'ok', icon: 'wave' },
    ];
    renderList(R.status, items, (i) => i.id, () => h('ss-status-chip', { size: 'sm' }), (el, i) => {
      el.setAttribute('label', i.label); el.setAttribute('value', i.value); el.setAttribute('state', i.state); el.setAttribute('icon', i.icon);
    });
  });

  return () => off.forEach((fn) => fn());
}
