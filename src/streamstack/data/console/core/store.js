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
    call: {
      available: false, state: 'idle', active: false, service: null, app: null, apps: [], camera: false, mic: 'none', reason: '',
      since: null, sources: { audio: true, video: true }, external: null, settings: { adapters: false },
      muted_warning: { enabled: true, sensitivity: 'medium', cooldown_s: 20, sound: false, count: 0, last_at: null, talking: false, range: [5, 300], sensitivities: ['high', 'medium', 'low'] },
      demo: false,
    },
    studio: { running: false, busy: false, camera_for_calls: 'off', program_label: null, camera_label: null, bridge: false, voice_chain: false, camlink: null },
    video: {
      scenes: [], program_scene: null, privacy: false, privacy_scene: 'Be Right Back',
      sources: [], selected_source: null,
      look: { brightness: 0, contrast: 0, gamma: 0, saturation: 0, warmth: 0, sharpen_enabled: false, sharpen_amount: 0, lut_enabled: false, lut_amount: 0 },
      limits: { brightness: [-0.5, 0.5], contrast: [-1, 1], gamma: [-1, 1], saturation: [-1, 1], warmth: [-1, 1], sharpen_amount: [0, 1], lut_amount: [0, 1] },
      looks: [], virtualcam: false,
      blur: {
        available: null,
        plugin: { installed: false, version: null, models_ok: false, loaded: null, devices: ['cpu'], device_labels: ['CPU'], device: 'cpu', hint: '' },
        sources: {}, on: false, strength: 8, rate: 2,
        limits: { strength: [1, 20], rate: [1, 6] }, presets: { light: 3, balanced: 2, smooth: 1 },
        cost: { state: 'off', detail: '', limits: { p95_ms: 8, cpu_percent: 1 } },
      },
      recording: { active: false, paused: false, timecode: null }, streaming: { active: false, timecode: null },
      canon: {
        available: false, state: 'disabled', message: null, session: null, usb: [],
        decode: { path: null, label: null, tried: [], render_node: null, ffmpeg: null }, input: null, output: null,
        fps_in: 0, fps_out: 0, grab_ms: 0, pipe_ms: 0, cpu: {}, frames: 0, restarts: { source: 0, encoder: 0 }, stalls: 0,
        last_error: null, uptime_s: 0, controls: {},
      },
    },
    audio: {
      mic: { name: null, description: null, present: false, gain_db: 0, muted: false, stalled: false, stalled_since: null, revive: { state: 'idle', message: null } },
      gain_range: [-30, 12],
      chain: {
        running: false, available: false, preset: null, presets: [], bypass: false, custom: false, missing: [], error: null,
        source: 'streamstack_mic', description: 'StreamStack Mic', rate: 48000, engine: 'deepfilternet', stages: [],
        loudness: { on: false, target_lufs: -16, targets: [-14, -16, -23], auto_db: 0, level_lufs: null, state: 'off', window_db: 12, true_peak_db: -1 },
      },
      tone: { bass_db: 0, presence_db: 0, treble_db: 0, supported: false },
      denoise: { engine: 'deepfilternet', atten_limit_db: 100, atten_range: [0, 100], supported: false, custom: false, engines: [] },
      tone_ranges: { bass_db: [-12, 12], presence_db: [-12, 12], treble_db: [-12, 12] },
      mic_check: { state: 'idle', seconds: 6, clips: [] },
      outputs: {
        available: false, problem: null, default: null, default_label: null, volume_range: [0, 100], devices: [], routes: [], warning: null,
        test: { state: 'idle', name: null, index: 0, total: 0, all: false, seconds: 2.8, phases: [], elapsed: null },
      },
    },
    auto: { running: false, backend: null, can_undo: false, busy: false, last_note: null, last_time: null, journal: [] },
    framing: {
      mode: 'off', state: 'off', held: false, available: false, unavailable_hint: null, reason: null, backend: null, device: null,
      detect_ms: null, detect_p95_ms: null, fps: null, model: null, zoom: 1, window: null, sources: [], skipped: [], modes: [],
      shot: 'auto', shots: [], subjects: 'group', subject_options: [], handoff: false, speaking: false, people: 0, speaker: false,
    },
    slots: {
      available: false, mode: 'off', modes: [], fade_seconds: 4, limits: {}, slots: [], parts: [],
      current: { slot: 'morning', label: 'Morning', minute: 0, clock: '00:00' }, strip: [],
      options: { looks: [], scenes: [], voices: [], lighting: [], canon: { present: false, exposure_choices: [], exposure_readonly: false, kelvin_range: [2500, 10000] }, blur: false },
      status: { state: 'off', summary: '', applied: null, held: null, suggestion: null, can_undo: false },
      director: { on: false }, events: [], demo: false,
    },
    health: { updated: null, summary: 'ok', checks: [] },
    system: { cpu_percent: null, load: [0, 0, 0], cores: 0 },
    virtual: { camera: null, mic: null, seen: [] },
    deck: {
      render: false, device: { connected: false, source: null },
      profile: { layout: { rows: 4, cols: 4 }, model: 'virtual-4x4', home: 'main', settings: { brightness: 70, sleep_minutes: 15 }, pristine: true, mismatch: false, pages: [] },
      surface: { rows: 4, cols: 4, page: { id: '', name: '' }, keys: [], can_back: false, fitted: false },
      models: [], palette_rev: '0',
    },
    hotkeys: {
      available: false, enabled: true, session: 'none',
      keys: { backend: 'none', ok: false, reason: null },
      midi: { ok: false, reason: null, devices: [], last: null },
      bindings: [], learn: { active: false, event: null }, events: [], limits: { bindings: 128 },
    },
    lighting: {
      scanned: false, lights: [], master: { on: false, all_on: false, brightness: 50, kelvin: 4500 },
      presets: [], preset: null, problem: null, kelvin_range: [2700, 6500], kelvin_step: 100,
      automation: {
        available: false, demo: false,
        settings: {
          follow: {
            enabled: true, grace_seconds: 120, start_delay_seconds: 3, turn_off: true,
            apps: true, ignore: [], devices: [],
          },
          schedule: {
            enabled: true, mode: 'sun', slots: {}, hours: null, fade_seconds: 3, hold_minutes: 90, fill_percent: 60,
          },
          lights: {},
        },
        limits: {}, roles: [], modes: [], slots: [], sources: [],
        status: {
          running: false, where: 'none', owner: 'none', owner_label: '', summary: '',
          camera: { in_use: false, reason: '', apps: [], grace_left: null, since: null },
          session: null, lights: {}, events: [],
        },
        service: { state: 'missing', installed: false, message: '', command: '' },
        location: { name: '', latitude: 0, longitude: 0, source: 'timezone' },
        sun: {}, now: { minute: 0, brightness: 50, kelvin: 4800, slot: 'morning', slot_label: 'Morning' },
        curve: { step: 15, brightness: [], kelvin: [] }, hours: [],
      },
    },
    stream: {
      status: 'unavailable', uptime_s: 0, bitrate_kbps: 0, dropped_frames: 0, latency_ms: 0, cpu_percent: null, gpu_percent: null,
      encoders: [], encoder: '', destinations: [], recording: { active: false, file: '', bytes: 0, seconds: 0, format: '' },
      settings: {
        codec: 'auto', encoder: 'auto', tier: 'source', fps: 0, bitrate_kbps: 0, rate_control: 'cbr', keyint_s: 2, quality: 0, audio_bitrate_kbps: 128,
      },
      output: { width: 0, height: 0, fps: 0 }, audio: { source: '', bitrate_kbps: 0, ok: false, error: '' },
      last_swap: null, events: [], probe: null,
    },
    interface: {
      present: false, checked: false, setting: 'auto', profile: null, label: null, usb: null, card: null, mode: null,
      notice: null, error: null, info: [], controls: [], capabilities: {}, unsupported: [], devices: [],
      software_gain: true, demo_profiles: [],
    },
    storage: { warnings: [] },
    hardware: {
      checked: false, demo: false, taken_at: null, hotplug: { mode: 'stopped', error: null }, rig: null, devices: [],
      gpus: [], roles: {}, encoder: null, audio: null, capture: null, loopback: null, deck: false, lights: false,
      notices: [], sources: [], changes: [], error: null, hosts: [], host: null,
    },
    diagnostics: {
      telemetry: { enabled: false, path: '/metrics', url: null, scrapes: 0, last_scrape: null, token_set: false },
      crashes: { dir: '', reports: [] },
    },
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
