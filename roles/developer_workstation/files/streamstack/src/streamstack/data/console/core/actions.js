// core/actions.js — the only way the UI changes the rig.
//
//   act(action, params?, {quiet?})      -> Promise<{ok, message?}|null>
//        Applies the optimistic update at once, POSTs api/action, toasts the server's
//        `error` text on failure (with a one-press fix when OBS/studio is down) and
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

const NEUTRAL_LOOK = { brightness: 0, contrast: 0, gamma: 0, saturation: 0, warmth: 0 };

export const OPTIMISTIC = {
  'audio.revive': () => ({ 'audio.mic.revive': { state: 'reviving', message: 'Suspending and resuming the microphone…' } }),
  'audio.mute': (p) => ({ 'audio.mic.muted': !!p.on }),
  'audio.gain': (p) => ({ 'audio.mic.gain_db': Number(p.db) }),
  'audio.preset': (p) => ({ 'audio.chain.preset': p.name }),
  'audio.bypass': (p) => ({ 'audio.chain.bypass': !!p.on }),
  'audio.tone': (p) => Object.fromEntries(Object.entries(p).map(([k, v]) => [`audio.tone.${k}`, v])),
  'audio.denoise': (p) => ({
    ...(p.engine !== undefined ? { 'audio.denoise.engine': p.engine } : {}),
    ...(p.atten_limit_db !== undefined ? { 'audio.denoise.atten_limit_db': Number(p.atten_limit_db) } : {}),
  }),
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
  'studio.start': () => ({ 'studio.busy': true }),
  'studio.stop': () => ({ 'studio.busy': true }),
};

/** Actions whose server message is worth showing (they take seconds and report through state). */
const SLOW = new Set(['studio.start', 'studio.stop', 'audio.mic_check', 'auto.once', 'auto.reset', 'health.refresh', 'open.app', 'video.looks.save', 'video.looks.delete', 'video.looks.apply']);

/** Wording of the server's 409s that have a one-press fix. */
const STUDIO_DOWN = /not running|start the studio|studio first/i;

function fixFor(message) {
  if (STUDIO_DOWN.test(message)) {
    return { label: 'Start studio', run: () => act('studio.start') };
  }
  return undefined;
}

export async function act(action, params = {}, { quiet = false, hold } = {}) {
  const optimistic = OPTIMISTIC[action];
  let paths = [];
  if (optimistic) {
    const patch = optimistic(params, store.state);
    paths = Object.keys(patch);
    store.patch(patch, hold === undefined ? undefined : { hold });
  }
  try {
    const res = await postAction(action, params);
    if (res && res.message && !quiet && SLOW.has(action)) toast(res.message, { level: 'info', timeout: 3500, id: `act:${action}` });
    return res;
  } catch (err) {
    store.release(paths);
    const message = err instanceof ApiError ? err.message : String(err);
    toast(message, { level: 'error', timeout: 7000, action: fixFor(message), id: `err:${action}` });
    return null;
  }
}

const streams = new Map();   // key -> { params, timer, last }

/** Throttled action for continuous controls (faders, knobs). */
export function actStream(action, params, { ms = 1000 / 15, key = action } = {}) {
  let s = streams.get(key);
  if (!s) {
    s = { params: null, timer: 0, last: 0 };
    streams.set(key, s);
  }
  // Merge `values` objects so two look faders dragged together do not drop each other.
  if (s.params && params.values && s.params.values) s.params = { ...params, values: { ...s.params.values, ...params.values } };
  else s.params = params;
  // Optimistic update on every call, so the UI never waits for the network.
  const optimistic = OPTIMISTIC[action];
  if (optimistic) store.patch(optimistic(params, store.state));
  if (s.timer) return;
  const fire = () => {
    s.timer = 0;
    const p = s.params;
    s.params = null;
    s.last = performance.now();
    if (p) act(action, p, { quiet: true });
  };
  const wait = ms - (performance.now() - s.last);
  if (wait <= 0) fire();
  else s.timer = setTimeout(fire, wait);
}

/** Send whatever a stream still holds right now. Call it on `change`: the control has already passed its final
 *  value through `input` (commit() flushes that first), so `change` must not stream the value a second time. */
export function flushStream(key) {
  const s = streams.get(key);
  if (!s || !s.timer) return;
  clearTimeout(s.timer);
  s.timer = 0;
  const p = s.params;
  s.params = null;
  s.last = performance.now();
  if (p) act(key, p, { quiet: true });
}
