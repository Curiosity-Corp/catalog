// app.js — StreamStack Console entry point and the facade pages import.
//
//   import { store, act, actStream, flushStream, toast, navigate, … } from '../app.js'
//   (pages normally receive the same things as `ctx` in mount(host, ctx).)
//
// Boot: theme → shell → live connection (snapshot + SSE with reconnect) → router → page.
// Public API (stable; documented in docs/console.md “Frontend API”):
//   store        state, get(), subscribe(selector, cb), onMeters(cb), patch(), onStatus(cb)     core/store.js
//   act(action, params, opts)       POST an action with optimistic update, toast on failure    core/actions.js
//   actStream(action, params)       ≤15/s throttled action for drags;  flushStream(action)
//   toast(text, opts) / toast.error(e)                                                         core/toast.js
//   navigate(id), onRoute(cb)                                                                  core/router.js
//   registerKeys([...]), registerCommands(() => [...])                                         core/keys.js
//   ops.*  toggleMute, togglePrivacy, selectScene, …                                           core/ops.js
//   ui / setUi / onUi               client-only UI state (thirds grid)                          core/ui.js
// Page modules: `export async function mount(host, ctx)`; return a cleanup function. See pages/index.js.

import './components/index.js';
import { icon, iconHTML } from './components/index.js';
import { store } from './core/store.js';
import { act, actStream, flushStream } from './core/actions.js';
import { toast } from './core/toast.js';
import { connect, retryNow } from './core/connection.js';
import { startRouter, onRoute, navigate, currentRoute } from './core/router.js';
import { registerKeys, registerCommands, installKeyboard } from './core/keys.js';
import { registerReviveControls } from './core/revive.js';
import { initTheme, toggleTheme, getTheme } from './core/theme.js';
import { ui, setUi, onUi } from './core/ui.js';
import * as ops from './core/ops.js';
import { pages } from './pages/index.js';
import { h, esc, renderList } from './core/util.js';

export { store, act, actStream, flushStream, toast, navigate, onRoute, currentRoute, registerKeys, registerCommands, ops, ui, setUi, onUi, icon, iconHTML };

const $ = (sel) => document.querySelector(sel);

// ---------- css loader for page stylesheets (CSP: external files only) ----------
const cssLoaded = new Map();
function ensureCss(href) {
  let p = cssLoaded.get(href);
  if (!p) {
    p = new Promise((resolve) => {
      const link = h('link', { rel: 'stylesheet', href });
      link.addEventListener('load', () => resolve());
      link.addEventListener('error', () => { console.error('stylesheet failed', href); resolve(); });
      document.head.append(link);
    });
    cssLoaded.set(href, p);
  }
  return p;
}

const ctx = {
  store, act, actStream, flushStream, toast, navigate, registerKeys, registerCommands, ui, icon, ensureCss, ops,
  page: null,
};

// ---------- page mounting ----------
let cleanup = null;
let mountToken = 0;
let hasData = false;

async function showPage(entry) {
  const token = ++mountToken;
  document.body.dataset.chrome = entry.chrome === false ? 'none' : 'full';
  document.body.dataset.page = entry.id;
  if (!hasData) return;                                  // wait for the first snapshot
  try { cleanup?.(); } catch (err) { console.error('page cleanup failed', err); }
  cleanup = null;
  const host = h('div', { id: 'page', 'data-page': entry.id });
  $('#page').replaceWith(host);
  try {
    const mod = await entry.load();
    if (token !== mountToken) return;
    const result = await mod.mount(host, { ...ctx, page: entry });
    if (token !== mountToken) { if (typeof result === 'function') result(); return; }
    cleanup = typeof result === 'function' ? result : null;
  } catch (err) {
    console.error('page failed', entry.id, err);
    if (token !== mountToken) return;
    host.innerHTML = `<div class="boot">${iconHTML('alert')}<div>Could not open ${esc(entry.title)}.<br><small>${esc(err.message)}</small></div></div>`;
  }
  updateRail(entry);
}

// ---------- shell ----------
function buildShell() {
  document.querySelectorAll('[data-icon]').forEach((el) => { el.replaceChildren(icon(el.dataset.icon)); });
  const rail = $('#rail');
  for (const p of pages.filter((x) => x.nav !== false)) {
    const a = h('a', { class: 'rail-item', href: `#/${p.id}`, 'data-id': p.id, title: `${p.title} (Alt+${p.key})` }, icon(p.icon), h('span', {}, p.title));
    rail.append(a);
  }
  rail.append(h('span', { class: 'rail-spacer' }));
  rail.append(h('a', { class: 'rail-item', href: '#/mini', 'data-id': 'mini', title: 'Open the compact strip' }, icon('mini'), h('span', {}, 'Mini')));

  const right = $('#top-right');
  const demo = h('ss-status-chip', { state: 'info', label: 'DEMO', icon: 'info', hidden: true, id: 'demo-chip', title: 'Simulated rig: nothing here touches real hardware' });
  const conn = h('ss-status-chip', { id: 'conn-chip', state: 'off', label: 'Connecting', icon: 'loader' });
  const pal = h('button', { class: 'palette-btn', type: 'button', 'aria-label': 'Open command palette', onclick: () => $('ss-palette').open() }, icon('search'), h('span', {}, 'Commands'), h('kbd', {}, 'Ctrl K'));
  const help = h('button', { class: 'icon-btn', type: 'button', title: 'Keyboard shortcuts (?)', 'aria-label': 'Keyboard shortcuts', onclick: () => $('ss-shortcuts').open() }, icon('keyboard'));
  const theme = h('button', { class: 'icon-btn', type: 'button', id: 'theme-btn', onclick: () => toggleTheme() });
  right.append(demo, conn, pal, help, theme);
  const syncTheme = () => {
    const light = getTheme() === 'light';
    theme.replaceChildren(icon(light ? 'moon' : 'sun'));
    theme.title = light ? 'Switch to dark theme' : 'Switch to light theme';
    theme.setAttribute('aria-label', theme.title);
  };
  syncTheme();
  window.addEventListener('ss:theme', syncTheme);
}

function updateRail(entry = currentRoute()) {
  document.querySelectorAll('#rail .rail-item').forEach((a) => {
    if (a.dataset.id === entry.id) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
}

function bindShell() {
  // global state mirrored onto <body> so CSS can react (red edge when muted, etc.)
  store.subscribe((s) => s.audio.mic.muted, (m) => { document.body.toggleAttribute('data-muted', m); });
  store.subscribe((s) => s.video.privacy, (p) => { document.body.toggleAttribute('data-privacy', p); });
  store.subscribe((s) => s.demo, (d) => { $('#demo-chip').hidden = !d; });
  store.subscribe((s) => s.video.obs, (o) => { document.body.dataset.obs = o; });

  // top bar: what matters on every page
  const center = $('#top-center');
  store.subscribe((s) => [s.audio.mic.muted, s.video.privacy, s.video.virtualcam || s.video.streaming.active, s.video.recording.active], ([muted, privacy, air, rec]) => {
    const chips = [];
    if (muted) chips.push({ id: 'muted', state: 'live', label: 'MUTED', icon: 'mic-off' });
    if (privacy) chips.push({ id: 'privacy', state: 'warn', label: 'PRIVACY', icon: 'eye-off' });
    if (air) chips.push({ id: 'air', state: 'live', label: 'ON AIR', icon: 'broadcast', pulse: true });
    if (rec) chips.push({ id: 'rec', state: 'live', label: 'REC', icon: 'record', pulse: true });
    renderList(center, chips, (c) => c.id, () => h('ss-status-chip', {}), (el, c) => {
      el.setAttribute('state', c.state); el.setAttribute('label', c.label); el.setAttribute('icon', c.icon); el.toggleAttribute('pulse', !!c.pulse);
    });
  });

  // health dot on the System item
  store.subscribe((s) => s.health.summary, (sum) => {
    const item = document.querySelector('#rail [data-id="system"]');
    if (!item) return;
    item.querySelector('.rail-dot')?.remove();
    if (sum !== 'ok') item.append(h('span', { class: 'rail-dot', 'data-state': sum, title: sum === 'fail' ? 'Health: needs attention' : 'Health: warnings' }));
  });

  // connection chip + banner
  const banner = $('#banner');
  const chip = $('#conn-chip');
  let wasOffline = false;
  store.onStatus((status, d) => {
    const main = $('#main');
    document.body.dataset.conn = status;
    if (status === 'online') {
      chip.setAttribute('state', 'ok'); chip.setAttribute('label', 'Live'); chip.setAttribute('icon', 'check');
      banner.removeAttribute('data-show');
      main.removeAttribute('data-stale');
      if (wasOffline) { wasOffline = false; window.dispatchEvent(new CustomEvent('ss:reconnected')); toast.success('Reconnected', { timeout: 2000 }); }
    } else if (status === 'reconnecting') {
      wasOffline = true;
      chip.setAttribute('state', 'warn'); chip.setAttribute('label', 'Offline'); chip.setAttribute('icon', 'alert');
      banner.dataset.show = ''; banner.dataset.kind = 'reconnecting';
      banner.replaceChildren(icon('loader', 'icon spin'), h('span', {}, `Reconnecting to StreamStack… ${d.retryIn ? `next try in ${d.retryIn}s` : 'trying now'}`),
        h('button', { type: 'button', onclick: () => retryNow() }, 'Retry now'));
      if (hasData) main.dataset.stale = '';
    } else if (status === 'unauthorized') {
      chip.setAttribute('state', 'fail'); chip.setAttribute('label', 'No access'); chip.setAttribute('icon', 'lock');
      banner.dataset.show = ''; banner.dataset.kind = 'unauthorized';
      banner.replaceChildren(icon('lock'), h('span', {}, d.message || 'Not authorised. Open the Console with “streamstack console”.'));
    } else {
      chip.setAttribute('state', 'off'); chip.setAttribute('label', 'Connecting'); chip.setAttribute('icon', 'loader');
    }
  });
}

// ---------- keys and commands ----------
function bindKeys() {
  const pageKeys = pages.filter((p) => p.nav !== false).map((p) => ({ key: `alt+${p.key}`, desc: `Go to ${p.title}`, group: 'Navigate', run: () => navigate(p.id) }));
  const sceneKeys = Array.from({ length: 9 }, (_, i) => ({ key: String(i + 1), desc: i === 0 ? 'Switch to scene 1 … 9' : '', group: 'Scenes', label: i === 0 ? '1 … 9' : '', run: () => ops.selectSceneByIndex(i) }));
  registerKeys([
    { key: 'm', desc: 'Mute / unmute the microphone', group: 'Live', run: () => ops.toggleMute() },
    { key: 'p', desc: 'Privacy: camera to Be Right Back', group: 'Live', run: () => ops.togglePrivacy() },
    { key: 'b', desc: 'Voice effects bypass (A/B)', group: 'Live', run: () => ops.toggleBypass() },
    { key: 'g', desc: 'Thirds grid on the picture', group: 'Live', run: () => setUi('grid', !ui.grid) },
    ...sceneKeys,
    ...pageKeys,
    { key: 'ctrl+k', desc: 'Command palette', group: 'Console', run: () => $('ss-palette').toggle() },
    { key: 'meta+k', desc: '', group: 'Console', run: () => $('ss-palette').toggle() },
    { key: '?', desc: 'This help', group: 'Console', label: '?', run: () => $('ss-shortcuts').open() },
  ]);

  // Shift+R and "Revive mic" work from every page, but only while the watchdog says the mic is stalled.
  registerReviveControls(ctx);

  registerCommands(() => {
    const s = store.state;
    const cmds = [];
    const add = (group, title, run, extra = {}) => cmds.push({ id: `${group}:${title}`, group, title, run, ...extra });
    for (const p of pages) add('Navigate', p.id === 'mini' ? 'Open the compact strip' : `Go to ${p.title}`, () => navigate(p.id), { icon: p.icon, hint: p.nav === false ? '' : `Alt ${p.key}` });
    add('Mic', s.audio.mic.muted ? 'Unmute microphone' : 'Mute microphone', ops.toggleMute, { icon: s.audio.mic.muted ? 'mic' : 'mic-off', hint: 'M', keywords: 'mute unmute mic microphone' });
    add('Camera', s.video.privacy ? 'Privacy off: back to the scene' : 'Privacy: camera to Be Right Back', ops.togglePrivacy, { icon: 'eye-off', hint: 'P', keywords: 'privacy brb be right back camera off' });
    add('Voice', s.audio.chain.bypass ? 'Voice effects on' : 'Bypass voice effects (A/B)', ops.toggleBypass, { icon: 'wave', hint: 'B', keywords: 'bypass ab effects' });
    s.video.scenes.forEach((sc, i) => add('Scenes', `Scene: ${sc.name}`, () => ops.selectScene(sc.name), { icon: 'scenes', hint: i < 9 ? String(i + 1) : '', keywords: 'scene switch' }));
    s.audio.chain.presets.forEach((p) => add('Voice', `Voice preset: ${p.label.replace(/^Voice (?=\S)/, '')}`, () => ops.selectPreset(p.name), { icon: 'mic', keywords: `preset voice ${p.blurb || ''}` }));
    s.video.looks.forEach((l) => add('Looks', `Look: ${l.name}`, () => act('video.looks.apply', { name: l.name }), { icon: 'sliders', keywords: 'look grade colour' }));
    add('Looks', 'Reset look to neutral', () => act('video.look.reset'), { icon: 'refresh', keywords: 'reset look neutral' });
    add('Camera', s.video.virtualcam ? 'Stop virtual camera' : 'Start virtual camera', ops.toggleVirtualcam, { icon: 'vcam', keywords: 'virtual camera vcam' });
    if (s.video.recording.active) add('Camera', 'Stop recording', () => ops.confirmThen('Stop the recording?', 'Stop recording', () => ops.setRecord(false)), { icon: 'stop', danger: true });
    else add('Camera', 'Start recording', () => ops.setRecord(true), { icon: 'record' });
    add('Studio', 'Start studio', ops.startStudio, { icon: 'play', keywords: 'start studio obs' });
    add('Studio', 'Stop studio', () => ops.confirmThen('Stop the studio? Calls fall back to the raw camera.', 'Stop studio', ops.stopStudio), { icon: 'power', danger: true, keywords: 'stop studio obs' });
    add('Auto', s.auto.running ? 'Turn auto director off' : 'Turn auto director on', ops.toggleAuto, { icon: 'auto', keywords: 'auto director claude' });
    add('Auto', 'Ask the director now', () => act('auto.once'), { icon: 'auto', keywords: 'auto claude once now' });
    add('Auto', 'Undo every auto change', () => act('auto.reset'), { icon: 'undo' });
    add('Audio', 'Record a mic check', () => act('audio.mic_check', {}), { icon: 'mic', keywords: 'mic check test recording' });
    add('Studio', 'Re-run health checks', () => act('health.refresh'), { icon: 'pulse', keywords: 'health doctor refresh' });
    for (const [name, label] of [['obs', 'OBS Studio'], ['easyeffects', 'Easy Effects'], ['qpwgraph', 'qpwgraph'], ['pavucontrol', 'pavucontrol'], ['qv4l2', 'qv4l2']]) {
      add('Open', `Open ${label}`, () => act('open.app', { name }), { icon: 'external', keywords: 'open launch app tool' });
    }
    add('View', getTheme() === 'dark' ? 'Light theme' : 'Dark theme', toggleTheme, { icon: getTheme() === 'dark' ? 'sun' : 'moon', keywords: 'theme dark light' });
    add('View', ui.grid ? 'Hide thirds grid' : 'Show thirds grid', () => setUi('grid', !ui.grid), { icon: 'grid', hint: 'G' });
    add('View', 'Keyboard shortcuts', () => $('ss-shortcuts').open(), { icon: 'keyboard', hint: '?' });
    return cmds;
  });
  installKeyboard();
}

// ---------- boot ----------
function boot() {
  initTheme();
  buildShell();
  bindShell();
  bindKeys();
  connect();
  store.subscribe(() => store.loaded, (loaded) => {
    if (loaded && !hasData) {
      hasData = true;
      showPage(currentRoute());
    }
  });
  onRoute((entry) => { if (hasData) showPage(entry); else { document.body.dataset.chrome = entry.chrome === false ? 'none' : 'full'; updateRail(entry); } });
  startRouter();
}

boot();
