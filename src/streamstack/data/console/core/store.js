// core/store.js — the single source of truth in the browser.
//
//   store.state                 the latest snapshot (always every key of docs/console.md)
//   store.get('video.look.brightness')
//   store.subscribe(selector, cb, {immediate})   selector: 'a.b.c' | (state) => value
//                                 cb(value, state) runs only when the selected value changed
//                                 (deep equality). Returns an unsubscribe function.
//   store.meters / store.onMeters(cb)   live levels; NEVER triggers state subscribers
//   store.status                'connecting' | 'online' | 'reconnecting' | 'unauthorized'
//   store.onStatus(cb)
//   store.applyServer(snapshot) called by the SSE client with each full snapshot
//   store.patch({ 'a.b': value }, {hold})  optimistic local write, held against server
//                                 snapshots for `hold` ms (default 600) after the last write
//   store.release(paths)        drop holds (after a failed action) and re-apply the server value
//
// The “dirty until 600 ms after last input” rule lives here: a held path keeps its local
// value when a snapshot arrives, and snaps to the server value once the hold expires.

import { deepEqual, getPath, setPath } from './util.js';

export const HOLD_MS = 600;

/** Every key present, nothing guarded (docs/console.md). Used before the first snapshot. */
export function emptyState() {
  return {
    version: '', demo: false,
    studio: { running: false, busy: false, camera_for_calls: 'off', program_label: null, camera_label: null, bridge: false, voice_chain: false },
    video: {
      obs: 'offline', obs_error: null, scenes: [], program_scene: null, privacy: false, privacy_scene: 'Be Right Back',
      sources: [], selected_source: null,
      look: { brightness: 0, contrast: 0, gamma: 0, saturation: 0, warmth: 0, sharpen_enabled: false, sharpen_amount: 0, lut_enabled: false, lut_amount: 0 },
      limits: { brightness: [-0.5, 0.5], contrast: [-1, 1], gamma: [-1, 1], saturation: [-1, 1], warmth: [-1, 1], sharpen_amount: [0, 1], lut_amount: [0, 1] },
      looks: [], virtualcam: false,
      recording: { active: false, paused: false, timecode: null }, streaming: { active: false, timecode: null },
    },
    audio: {
      mic: { name: null, description: null, present: false, gain_db: 0, muted: false, stalled: false, stalled_since: null, revive: { state: 'idle', message: null } },
      gain_range: [-30, 12],
      chain: { running: false, available: false, preset: null, presets: [], bypass: false },
      tone: { bass_db: 0, presence_db: 0, treble_db: 0, pitch_semitones: 0, supported: false },
      tone_ranges: { bass_db: [-12, 12], presence_db: [-12, 12], treble_db: [-12, 12], pitch_semitones: [-6, 6] },
      mic_check: { state: 'idle', seconds: 6, clips: [] },
    },
    auto: { running: false, backend: null, can_undo: false, busy: false, last_note: null, last_time: null, journal: [] },
    health: { updated: null, summary: 'ok', checks: [] },
    system: { cpu_percent: null, load: [0, 0, 0], cores: 0 },
  };
}

/** Fill gaps in a server snapshot from the empty skeleton so pages never guard keys. */
function withDefaults(snapshot, skeleton = emptyState()) {
  if (typeof snapshot !== 'object' || snapshot === null || Array.isArray(snapshot)) return snapshot ?? skeleton;
  const out = { ...snapshot };
  for (const [k, v] of Object.entries(skeleton)) {
    if (!(k in out) || out[k] === undefined) out[k] = v;
    else if (v && typeof v === 'object' && !Array.isArray(v)) out[k] = withDefaults(out[k], v);
  }
  return out;
}

class Store {
  constructor() {
    this.state = emptyState();
    this.server = this.state;          // last snapshot exactly as the server sent it
    this.loaded = false;               // a first snapshot has arrived
    this.status = 'connecting';
    this.statusDetail = {};            // { retryIn, attempt, message }
    this.meters = { t: 0, mic: null, voice: null };
    this._subs = new Set();
    this._meterSubs = new Set();
    this._statusSubs = new Set();
    this._holds = new Map();           // path -> { value, until }
    this._holdTimer = 0;
  }

  get(path) { return getPath(this.state, path); }

  subscribe(selector, cb, { immediate = true } = {}) {
    const select = typeof selector === 'function' ? selector : (s) => getPath(s, selector);
    const sub = { select, cb, last: select(this.state) };
    this._subs.add(sub);
    if (immediate) cb(sub.last, this.state);
    return () => this._subs.delete(sub);
  }

  onMeters(cb) { this._meterSubs.add(cb); return () => this._meterSubs.delete(cb); }
  onStatus(cb) { this._statusSubs.add(cb); cb(this.status, this.statusDetail); return () => this._statusSubs.delete(cb); }

  setStatus(status, detail = {}) {
    if (status === this.status && deepEqual(detail, this.statusDetail)) return;
    this.status = status;
    this.statusDetail = detail;
    for (const cb of [...this._statusSubs]) cb(status, detail);
  }

  pushMeters(frame) {
    this.meters = frame;
    for (const cb of this._meterSubs) cb(frame);
  }

  /** A full snapshot from the server (GET api/state or an SSE `state` event). */
  applyServer(snapshot) {
    this.server = withDefaults(snapshot);
    this.loaded = true;
    this._commit();
  }

  /** Optimistic write of dotted paths; held against snapshots for `hold` ms after the last write. */
  patch(values, { hold = HOLD_MS } = {}) {
    const until = performance.now() + hold;
    for (const [path, value] of Object.entries(values)) this._holds.set(path, { value, until });
    this._commit();
    this._armHoldTimer();
  }

  release(paths) {
    for (const p of paths) this._holds.delete(p);
    this._commit();
  }

  _armHoldTimer() {
    clearTimeout(this._holdTimer);
    if (!this._holds.size) return;
    const next = Math.min(...[...this._holds.values()].map((h) => h.until));
    this._holdTimer = setTimeout(() => this._commit(), Math.max(0, next - performance.now()) + 5);
  }

  /** server snapshot + live holds -> state; notify only subscribers whose value changed. */
  _commit() {
    const now = performance.now();
    const next = structuredClone(this.server);
    for (const [path, hold] of [...this._holds]) {
      if (hold.until <= now) this._holds.delete(path);
      else setPath(next, path, hold.value);
    }
    this.state = next;
    for (const sub of [...this._subs]) {
      const value = sub.select(next);
      if (!deepEqual(value, sub.last)) {
        sub.last = value;
        try {
          sub.cb(value, next);
        } catch (err) {
          console.error('store subscriber failed', err);
        }
      }
    }
    this._armHoldTimer();
  }
}

export const store = new Store();
