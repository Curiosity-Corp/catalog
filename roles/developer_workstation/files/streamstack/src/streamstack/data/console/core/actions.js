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

const NEUTRAL_LOOK = { brightness: 0, contrast: 0, gamma: 0, saturation: 0, warmth: 0 };

export const OPTIMISTIC = {
  'audio.revive': () => ({ 'audio.mic.revive': { state: 'reviving', message: 'Suspending and resuming the microphone…' } }),
  'audio.mute': (p) => ({ 'audio.mic.muted': !!p.on }),
  'audio.gain': (p) => ({ 'audio.mic.gain_db': Number(p.db) }),
  'audio.preset': (p) => ({ 'audio.chain.preset': p.name }),
  'audio.bypass': (p) => ({ 'audio.chain.bypass': !!p.on }),
  'audio.tone': (p) => Object.fromEntries(Object.entries(p).map(([k, v]) => [`audio.tone.${k}`, v])),
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
  'video.source': (p) => ({ 'video.selected_source': p.name }),
  'video.virtualcam': (p) => ({ 'video.virtualcam': !!p.on }),
  'video.record': (p, s) => ({ 'video.recording': { ...s.video.recording, active: !!p.on, paused: false } }),
  'video.stream': (p, s) => ({ 'video.streaming': { ...s.video.streaming, active: !!p.on } }),
  'auto.on': () => ({ 'auto.running': true }),
  'auto.off': () => ({ 'auto.running': false }),
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
