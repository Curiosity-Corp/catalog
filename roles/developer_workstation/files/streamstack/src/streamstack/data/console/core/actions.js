// core/actions.js — the only way the UI changes the rig.
//
//   act(action, params?, {quiet?})      -> Promise<{ok, message?}|null>
//        Applies the optimistic update at once, POSTs api/action (posts of one action go out in call order), toasts the server's
//        `error` text on failure (with a one-press fix when the studio is down) and
//        rolls the optimistic value back to the server's. Never rejects.
//   actStream(action, params, {ms})     -> void
//        For drags: coalesces to at most 15 requests/s, always delivers the last value.
//        Params of the same action are merged (look values from different faders combine).
//   OPTIMISTIC                          action -> (params, state) => { 'dotted.path': value }
//
// Add an entry to OPTIMISTIC when a new action has a visible effect in the snapshot.

import { postAction, ApiError } from './api.js';
import { store } from './store.js';
import { toast } from './toast.js';

/** Dotted-path patches for the lights an action targets (`id`: a light id, or "all"/absent for every connected one). */
function lightPatch(p, s, fields) {
  const out = {};
  s.lighting.lights.forEach((l, i) => {
    if (!l.connected || !(p.id === undefined || p.id === 'all' || p.id === l.id)) return;
    for (const [key, value] of Object.entries(fields(l))) out[`lighting.lights.${i}.${key}`] = value;
  });
  return out;
}

/** The master strip follows its lights: any-on / all-on from the patched power values. */
function masterPatch(patch, s, extra = {}) {
  const power = s.lighting.lights.map((l, i) => (l.connected ? (patch[`lighting.lights.${i}.power`] ?? l.power) : null)).filter((v) => v !== null);
  return { ...patch, 'lighting.master.on': power.some(Boolean), 'lighting.master.all_on': power.length > 0 && power.every(Boolean), ...extra };
}

const lumenFor = (l, percent) => Math.round(l.lumen_range[0] + (l.lumen_range[1] - l.lumen_range[0]) * (percent - 1) / 99);
const targetsAll = (p) => p.id === undefined || p.id === 'all';

/** Dotted-path patches for the output an action names (`name: null` patches every output). */
function outputPatch(p, s, fields) {
  const out = {};
  s.audio.outputs.devices.forEach((d, i) => {
    if (p.name !== null && d.name !== p.name) return;
    for (const [key, value] of Object.entries(fields(d))) out[`audio.outputs.devices.${i}.${key}`] = value;
  });
  return out;
}

/** The encoder settings of `stream.set` (the rest of its params, the destination list, is not a setting). */
const STREAM_SETTINGS = ['codec', 'encoder', 'tier', 'fps', 'bitrate_kbps', 'rate_control', 'keyint_s', 'quality', 'audio_bitrate_kbps'];

/** Dotted-path patches for chain parameter changes `{key: value}`: a stage switch is `<stage>_on`, the rest are faders. */
function chainPatch(values, s) {
  const out = {};
  s.audio.chain.stages.forEach((st, i) => {
    if (`${st.id}_on` in values) out[`audio.chain.stages.${i}.on`] = !!values[`${st.id}_on`];
    st.params.forEach((param, j) => {
      if (param.key in values) out[`audio.chain.stages.${i}.params.${j}.value`] = Number(values[param.key]);
    });
  });
  if (Object.keys(out).length) out['audio.chain.custom'] = true;
  return out;
}

/** `{prefix.key: value}` for the listed params that were sent. */
function keysOf(p, keys, prefix, convert = (k, v) => v) {
  const out = {};
  for (const k of keys) if (p[k] !== undefined) out[`${prefix}.${k}`] = convert(k, p[k]);
  return out;
}

/** Same, for the params that already name a field of `group` (a settings object the server sends whole). */
function existingKeysOf(p, group, prefix) {
  return keysOf(p, Object.keys(p).filter((k) => k in group), prefix);
}

/** Deck profile edits (#157). The editor draws from `deck.profile`, which also changes when a key lights up, so an edit
 *  that is not held on screen is overwritten by the next snapshot and the key, label or tab flips back to what it was.
 *  The views are the server's to compute (rev, lit, tone); the patch carries only what the edit decides. */
const deckPage = (s, id) => s.deck.profile.pages.findIndex((p) => p.id === id);
const keyPath = (pi, k) => `deck.profile.pages.${pi}.keys.${k}`;
const emptyKey = (i) => ({ i, empty: true, rev: '0' });

function deckKeySet(p, s) {
  const pi = deckPage(s, p.page);
  const view = pi < 0 ? null : s.deck.profile.pages[pi].keys[p.key];
  if (!view || !p.spec) return {};
  const raw = { action: p.spec.action || '', params: p.spec.params || {}, ...(p.spec.label ? { label: p.spec.label } : {}), ...(p.spec.icon ? { icon: p.spec.icon } : {}) };
  return {
    [keyPath(pi, p.key)]: {
      ...view, i: p.key, empty: false, action: raw.action, raw,
      label: raw.label || (view.empty ? raw.action || 'Set action' : view.label),
      icon: raw.icon || (view.empty ? 'command' : view.icon),
    },
  };
}

function deckKeyMove(p, s) {
  const a = deckPage(s, p.page);
  const b = deckPage(s, p.to_page || p.page);
  if (a < 0 || b < 0) return {};
  const from = s.deck.profile.pages[a].keys[p.key];
  const to = s.deck.profile.pages[b].keys[p.to];
  if (!from || !to) return {};
  return { [keyPath(a, p.key)]: { ...to, i: p.key }, [keyPath(b, p.to)]: { ...from, i: p.to } };
}

function deckPageDelete(p, s) {
  const pages = s.deck.profile.pages.filter((x) => x.id !== p.page);
  if (pages.length === s.deck.profile.pages.length || !pages.length) return {};
  return { 'deck.profile.pages': pages, ...(s.deck.profile.home === p.page ? { 'deck.profile.home': pages[0].id } : {}) };
}

function deckPageMove(p, s) {
  const pages = [...s.deck.profile.pages];
  const at = pages.findIndex((x) => x.id === p.page);
  if (at < 0 || !Number.isInteger(p.to) || p.to < 0 || p.to >= pages.length) return {};
  pages.splice(p.to, 0, ...pages.splice(at, 1));
  return { 'deck.profile.pages': pages };
}

/** Settings that live under a light id, a slot id or an hour: replaced whole, because an id may contain a dot. */
function lightSettings(p, s) {
  const fields = ['name', 'role', 'min_brightness', 'max_brightness', 'min_kelvin', 'max_kelvin'].filter((k) => p[k] !== undefined);
  if (!p.id || !fields.length) return {};
  const lights = s.lighting.automation.settings.lights;
  return { 'lighting.automation.settings.lights': { ...lights, [p.id]: { ...(lights[p.id] || {}), ...Object.fromEntries(fields.map((k) => [k, p[k]])) } } };
}

const NEUTRAL_LOOK = { brightness: 0, contrast: 0, gamma: 0, saturation: 0, warmth: 0 };

export const OPTIMISTIC = {
  'audio.revive': () => ({ 'audio.mic.revive': { state: 'reviving', message: 'Suspending and resuming the microphone…' } }),
  'audio.mute': (p) => ({ 'audio.mic.muted': !!p.on }),
  'audio.gain': (p) => ({ 'audio.mic.gain_db': Number(p.db) }),
  'audio.preset': (p) => ({ 'audio.chain.preset': p.name }),
  'audio.bypass': (p) => ({ 'audio.chain.bypass': !!p.on }),
  'audio.tone': (p) => Object.fromEntries(Object.entries(p).map(([k, v]) => [`audio.tone.${k}`, v])),
  'audio.chain': (p, s) => chainPatch(p.params || {}, s),
  'audio.loudness': (p) => ({
    ...(p.on !== undefined ? { 'audio.chain.loudness.on': !!p.on } : {}),
    ...(p.target_lufs !== undefined ? { 'audio.chain.loudness.target_lufs': Number(p.target_lufs) } : {}),
  }),
  'audio.denoise': (p) => ({
    ...(p.engine !== undefined ? { 'audio.denoise.engine': p.engine } : {}),
    ...(p.atten_limit_db !== undefined ? { 'audio.denoise.atten_limit_db': Number(p.atten_limit_db) } : {}),
  }),
  'audio.output_volume': (p, s) => outputPatch(p, s, () => ({ volume: Number(p.percent) })),
  'audio.output_mute': (p, s) => outputPatch(p, s, () => ({ muted: !!p.on })),
  'audio.output_default': (p, s) => ({
    'audio.outputs.default': p.name,
    ...outputPatch({ name: null }, s, (d) => ({ default: d.name === p.name })),
    ...Object.fromEntries(s.audio.outputs.routes.map((r, i) => [`audio.outputs.routes.${i}.default`, r.name === p.name])),
    'audio.outputs.warning': null,
  }),
  'audio.output_dismiss': () => ({ 'audio.outputs.warning': null }),
  'video.scene': (p, s) => ({
    'video.scenes': s.video.scenes.map((sc) => ({ ...sc, active: sc.name === p.name })),
    'video.program_scene': p.name,
    // choosing a scene by hand ends privacy (unless it IS the privacy scene)
    'video.privacy': p.name === s.video.privacy_scene ? s.video.privacy : false,
  }),
  'video.privacy': (p, s) => (p.on
    ? {
      'video.privacy': true,
      'video.scenes': s.video.scenes.map((sc) => ({ ...sc, active: sc.name === s.video.privacy_scene })),
      'video.program_scene': s.video.privacy_scene,
    }
    : { 'video.privacy': false }),
  'video.look': (p) => Object.fromEntries(Object.entries(p.values || {}).map(([k, v]) => [`video.look.${k}`, v])),
  'video.look.reset': () => Object.fromEntries(Object.entries(NEUTRAL_LOOK).map(([k, v]) => [`video.look.${k}`, v])),
  'video.blur': (p, s) => {
    const b = s.video.blur;
    const names = p.source ? [p.source] : Object.keys(b.sources);
    const sources = { ...b.sources };    // replaced whole: camera names may contain dots, so no per-source path
    for (const name of names) {
      const entry = sources[name] || { present: true, enabled: false, strength: b.strength, rate: b.rate };
      sources[name] = {
        ...entry,
        present: true,
        ...(typeof p.on === 'boolean' ? { enabled: p.on } : {}),
        ...(p.strength !== undefined ? { strength: Number(p.strength) } : {}),
        ...(p.rate !== undefined ? { rate: Number(p.rate) } : {}),
      };
    }
    return {
      'video.blur.sources': sources,
      'video.blur.on': Object.values(sources).some((e) => e.enabled),
      ...(p.strength !== undefined ? { 'video.blur.strength': Number(p.strength) } : {}),
      ...(p.rate !== undefined ? { 'video.blur.rate': Number(p.rate) } : {}),
    };
  },
  'video.source': (p) => ({ 'video.selected_source': p.name }),
  'video.virtualcam': (p) => ({ 'video.virtualcam': !!p.on }),
  'video.record': (p, s) => ({ 'video.recording': { ...s.video.recording, active: !!p.on, paused: false } }),
  'video.stream': (p, s) => ({ 'video.streaming': { ...s.video.streaming, active: !!p.on } }),
  'interface.set': (p, s) => ({ 'interface.controls': s.interface.controls.map((w) => (w.id === p.id ? { ...w, value: p.value } : w)) }),
  'auto.on': () => ({ 'auto.running': true }),
  'auto.off': () => ({ 'auto.running': false }),
  'auto.persona': (p) => ({ 'auto.director.persona': p.id }),
  'lighting.power': (p, s) => masterPatch(lightPatch(p, s, () => ({ power: !!p.on })), s),
  'lighting.brightness': (p, s) => masterPatch(
    lightPatch(p, s, (l) => ({ brightness: Number(p.percent), lumen: lumenFor(l, Number(p.percent)) })), s,
    targetsAll(p) ? { 'lighting.master.brightness': Number(p.percent) } : {},
  ),
  'lighting.temperature': (p, s) => masterPatch(
    lightPatch(p, s, () => ({ kelvin: Number(p.kelvin) })), s,
    targetsAll(p) ? { 'lighting.master.kelvin': Number(p.kelvin) } : {},
  ),
  'lighting.preset': (p, s) => {
    const preset = s.lighting.presets.find((x) => x.name === p.name);
    if (!preset) return {};
    const fields = (l) => (preset.on ? { power: true, brightness: preset.brightness, lumen: lumenFor(l, preset.brightness), kelvin: preset.kelvin } : { power: false });
    const extra = preset.on ? { 'lighting.master.brightness': preset.brightness, 'lighting.master.kelvin': preset.kelvin } : {};
    return masterPatch(lightPatch({}, s, fields), s, { 'lighting.preset': p.name, ...extra });
  },
  // Settings pages draw their controls from a broad slice (`call`, `framing`, `network`, ...) that changes many times a
  // second for other reasons. Without an entry here a click was overwritten by the next snapshot, before the server had
  // saved it, and the control seemed not to move (#155).
  'call.set': (p) => keysOf(p, ['enabled', 'sensitivity', 'cooldown_s', 'sound'], 'call.muted_warning', (k, v) => (k === 'cooldown_s' ? Number(v) : v)),
  'network.set': (p) => (p.enabled !== undefined ? { 'network.enabled': !!p.enabled } : {}),
  // not 'telemetry.set': an opt-in that opens a metrics endpoint must show only what the server has done
  'hotkeys.enable': (p) => ({ 'hotkeys.enabled': !!p.on }),
  'framing.mode': (p) => ({ 'framing.mode': p.mode }),
  'framing.hold': (p) => ({ 'framing.held': !!p.on }),
  'framing.handoff': (p) => ({ 'framing.handoff': !!p.on }),
  'framing.shot': (p) => ({ 'framing.shot': p.shot }),
  'framing.subjects': (p) => ({ 'framing.subjects': p.subjects }),
  'slots.mode': (p) => ({ 'slots.mode': p.mode }),
  'deck.key_set': deckKeySet,
  'deck.key_clear': (p, s) => {
    const pi = deckPage(s, p.page);
    return pi < 0 || !s.deck.profile.pages[pi].keys[p.key] ? {} : { [keyPath(pi, p.key)]: emptyKey(p.key) };
  },
  'deck.key_move': deckKeyMove,
  'deck.page_rename': (p, s) => (deckPage(s, p.page) < 0 ? {} : { [`deck.profile.pages.${deckPage(s, p.page)}.name`]: p.name }),
  'deck.page_move': deckPageMove,
  'deck.page_delete': deckPageDelete,
  // the model only: the grid and every key re-flow on the server, and a half-drawn grid is worse than a late one
  'deck.layout': (p, s) => (p.model && s.deck.models.some((m) => m.id === p.model) ? { 'deck.profile.model': p.model } : {}),
  'presets.platform': (p, s) => ({ 'presets.platform': p.id, ...(s.presets.tier ? {} : { 'presets.tier': 'balanced' }) }),
  'presets.tier': (p) => ({ 'presets.tier': p.id }),
  'slots.fade': (p) => ({ 'slots.fade_seconds': Number(p.seconds) }),
  'slots.profile': (p, s) => {
    const i = s.slots.slots.findIndex((x) => x.id === p.slot);
    const profile = p.profile || {};
    return i < 0 ? {} : { [`slots.slots.${i}.profile`]: profile, [`slots.slots.${i}.empty`]: Object.keys(profile).length === 0 };
  },
  'slots.clear': (p, s) => {
    const i = s.slots.slots.findIndex((x) => x.id === p.slot);
    return i < 0 ? {} : { [`slots.slots.${i}.profile`]: {}, [`slots.slots.${i}.empty`]: true };
  },
  'lighting.auto.slot': (p, s) => (s.lighting.automation.settings.schedule.slots[p.slot]
    ? keysOf(p, ['brightness', 'kelvin'], `lighting.automation.settings.schedule.slots.${p.slot}`, (k, v) => Number(v)) : {}),
  'lighting.auto.hour': (p, s) => (s.lighting.automation.hours[Number(p.hour)]
    ? keysOf(p, ['brightness', 'kelvin'], `lighting.automation.hours.${Number(p.hour)}`, (k, v) => Number(v)) : {}),
  'lighting.light.settings': lightSettings,
  'deck.settings': (p) => keysOf(p, ['brightness', 'sleep_minutes'], 'deck.profile.settings', (k, v) => Number(v)),
  'lighting.auto.follow': (p, s) => existingKeysOf(p, s.lighting.automation.settings.follow, 'lighting.automation.settings.follow'),
  'lighting.auto.schedule': (p, s) => existingKeysOf(p, s.lighting.automation.settings.schedule, 'lighting.automation.settings.schedule'),
  'stream.set': (p) => Object.fromEntries(STREAM_SETTINGS.filter((k) => p[k] !== undefined).map((k) => [`stream.settings.${k}`, p[k]])),
  'studio.start': () => ({ 'studio.busy': true }),
  'studio.stop': () => ({ 'studio.busy': true }),
};

/** Actions whose server message is worth showing (they take seconds and report through state). */
const SLOW = new Set(['studio.start', 'studio.stop', 'audio.mic_check', 'audio.output_default', 'auto.once', 'auto.reset', 'health.refresh', 'open.app', 'video.looks.save', 'video.looks.delete', 'video.looks.apply']);

/** Wording of the server's 409s that have a one-press fix. */
const STUDIO_DOWN = /not running|start the studio|studio first/i;

function fixFor(message) {
  if (STUDIO_DOWN.test(message)) {
    return { label: 'Start studio', run: () => act('studio.start') };
  }
  return undefined;
}

// One lane per action name: a post goes out only after the previous post of the same action has answered. The server
// handles requests on separate threads, so two overlapping posts of one action (a tier picked, then picked again with
// the arrow keys) can reach it in the opposite order and leave the older choice standing.
const lanes = new Map();   // action -> promise that settles when the lane's last post has answered

function inOrder(action, send) {
  const run = (lanes.get(action) || Promise.resolve()).then(send);
  const tail = run.then(() => undefined, () => undefined);
  lanes.set(action, tail);
  tail.then(() => { if (lanes.get(action) === tail) lanes.delete(action); });
  return run;
}

/** Actions whose optimistic value is a state that passes (a start in progress, a restart), not one the server keeps. */
const TRANSIENT = new Set(['studio.start', 'studio.stop', 'audio.revive']);

export async function act(action, params = {}, { quiet = false, hold } = {}) {
  const optimistic = OPTIMISTIC[action];
  let paths = [];
  let token = 0;
  if (optimistic) {
    const patch = optimistic(params, store.state);
    paths = Object.keys(patch);
    // Held until the server has answered, however long that takes: a fixed 600 ms ran out on a loaded machine before the
    // snapshot carrying the change arrived, the page flipped back, and the next M press toggled from the stale value.
    token = store.patch(patch, { hold: hold === undefined ? Infinity : hold });
  }
  try {
    const res = await inOrder(action, () => postAction(action, params));
    store.settle(paths, token, { agree: !TRANSIENT.has(action), seq: res && res.seq });
    if (res && res.message && !quiet && SLOW.has(action)) toast(res.message, { level: 'info', timeout: 3500, id: `act:${action}` });
    return res;
  } catch (err) {
    store.release(paths);
    const message = err instanceof ApiError ? err.message : String(err);
    toast(message, { level: 'error', timeout: 7000, action: fixFor(message), id: `err:${action}` });
    return null;
  }
}

const streams = new Map();   // key -> { params, timer, last, busy }

/** Throttled action for continuous controls (faders, knobs).
 *  One request per key is in flight at a time: the server handles requests on separate threads, so two overlapping
 *  posts of the same fader can land out of order and leave an older value on the device. While a post is out, the
 *  newest values are held and sent the moment it answers. */
export function actStream(action, params, { ms = 1000 / 15, key = action } = {}) {
  let s = streams.get(key);
  if (!s) {
    s = { action, params: null, timer: 0, last: 0, busy: false };
    streams.set(key, s);
  }
  // Merge `values` objects so two look faders dragged together do not drop each other.
  if (s.params && params.values && s.params.values) s.params = { ...params, values: { ...s.params.values, ...params.values } };
  else if (s.params && params.params && s.params.params) s.params = { ...params, params: { ...s.params.params, ...params.params } };
  else s.params = params;
  // Optimistic update on every call, so the UI never waits for the network.
  const optimistic = OPTIMISTIC[action];
  if (optimistic) store.patch(optimistic(params, store.state));
  if (s.timer || s.busy) return;
  const wait = ms - (performance.now() - s.last);
  if (wait <= 0) sendStream(s, ms);
  else s.timer = setTimeout(() => sendStream(s, ms), wait);
}

function sendStream(s, ms) {
  s.timer = 0;
  const p = s.params;
  if (!p || s.busy) return;
  s.params = null;
  s.busy = true;
  s.last = performance.now();
  act(s.action, p, { quiet: true }).finally(() => {
    s.busy = false;
    if (!s.params || s.timer) return;
    const wait = ms - (performance.now() - s.last);
    if (wait <= 0) sendStream(s, ms);
    else s.timer = setTimeout(() => sendStream(s, ms), wait);
  });
}

/** Send whatever a stream still holds right now. Call it on `change`: the control has already passed its final
 *  value through `input` (commit() flushes that first), so `change` must not stream the value a second time.
 *  When a post is still in flight the held values go out as soon as it answers. */
export function flushStream(key) {
  const s = streams.get(key);
  if (!s || !s.params) return;
  if (s.timer) {
    clearTimeout(s.timer);
    s.timer = 0;
  }
  if (!s.busy) sendStream(s, 0);
}
