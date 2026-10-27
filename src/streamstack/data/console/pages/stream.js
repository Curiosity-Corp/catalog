// pages/stream.js — THE STREAM PAGE: go live, record, destinations, encoder, live controls and meters.
//
// Top: two blocks with big, unmistakable state. STREAM (Go live / Stop stream, held to stop) and RECORD (Record / Stop).
// Next: live meters (encoder fps, bitrate, dropped frames, latency, CPU and GPU) with a one-minute sparkline each.
// Then: DESTINATIONS (SRT, RTMP, Enhanced RTMP, WHIP; the local recording is pinned on top) with health and reconnect
// state per destination, the ENCODER (codec, the encoders this host really has, the transports and why one is
// unavailable), and LIVE CONTROLS: faders for bitrate, keyframe interval, quality and audio bitrate that change the
// running stream through the hot-swap path with no reconnect. Settings that reconnect once (codec, resolution, fps) are
// staged and applied with one button that says so.
//
// Keys: a stream key is typed into a password field, sent once, and cleared. The server never sends it back (the state
// says only "set" or "none") and this page never stores, renders or logs it.
//
// Data: `state.stream` (docs/stream.md). It arrives with every state push of the live connection (the server's
// Server-Sent Events channel, one push a second while anything changes, and at once after an action), so there is no
// polling here. Every action is stream.* or record.* (docs/stream.md).

import { h, renderList } from '../core/util.js';
import { toast } from '../core/toast.js';

const STATUS = {
  idle: { word: 'Offline', state: 'off' },
  starting: { word: 'Connecting', state: 'warn' },
  live: { word: 'On air', state: 'live' },
  degraded: { word: 'Degraded', state: 'warn' },
  recording: { word: 'Offline', state: 'off' },
  error: { word: 'Encoder failed', state: 'fail' },
  unavailable: { word: 'Not available', state: 'off' },
};
const DEST_STATE = {
  idle: 'Idle', connecting: 'Connecting', live: 'Live', backoff: 'Reconnecting', failed: 'Failed',
};
const DEST_PILL = { ok: 'ok', warn: 'warn', fail: 'fail', idle: '' };
const CODEC_NAMES = { av1: 'AV1', hevc: 'HEVC', h264: 'H.264' };
const CODECS = ['auto', 'av1', 'hevc', 'h264'];
const KIND_NAMES = { srt: 'SRT', rtmp: 'RTMP', whip: 'WHIP', record: 'Recording' };
const RATE_CONTROLS = [['cbr', 'Constant (CBR)'], ['vbr', 'Variable (VBR)'], ['cqp', 'Quality (CQP)']];
const TIERS = ['source', '480p', '720p', '900p', '1080p', '1440p', '2160p'];
const FPS = [0, 24, 25, 30, 48, 50, 60];
const HISTORY = 60;
const LIVE_GAP_MS = 1300;     // a hot swap takes about a second; the server refuses another while one is under way
const REF_FORM = /^(env|file|cfg|cmd):\S/;

const TEMPLATE = `
<div class="st">
  <section class="st-hero" aria-label="Stream and record">
    <ss-card class="st-block" heading="Stream" icon="broadcast" data-ref="streamCard">
      <div class="st-state" data-ref="streamState" data-state="off">
        <span class="st-led" aria-hidden="true"></span>
        <div class="st-headline">
          <strong class="st-word" data-ref="streamWord" role="status">Offline</strong>
          <span class="st-clock" data-ref="streamClock" hidden></span>
        </div>
      </div>
      <p class="st-sub" data-ref="streamSub"></p>
      <div class="st-go">
        <ss-button size="lg" variant="primary" icon="broadcast" data-ref="goBtn">Go live</ss-button>
        <ss-button size="lg" variant="danger" icon="stop" hold="800" data-ref="stopBtn" hidden>Stop stream</ss-button>
      </div>
      <p class="hint st-why" data-ref="goWhy" role="status"></p>
    </ss-card>

    <ss-card class="st-block" heading="Record" icon="record" data-ref="recordCard">
      <div class="st-state" data-ref="recordState" data-state="off">
        <span class="st-led" aria-hidden="true"></span>
        <div class="st-headline">
          <strong class="st-word" data-ref="recordWord" role="status">Not recording</strong>
          <span class="st-clock" data-ref="recordClock" hidden></span>
        </div>
      </div>
      <p class="st-sub" data-ref="recordSub"></p>
      <div class="st-go">
        <ss-button size="lg" variant="default" icon="record" data-ref="recBtn">Record</ss-button>
        <ss-button size="lg" variant="danger" icon="stop" data-ref="recStopBtn" hidden>Stop recording</ss-button>
        <div class="seg" role="group" aria-label="Recording format" data-ref="recFormat">
          <button type="button" data-format="mkv" aria-pressed="true" title="Matroska: a crash loses at most the last second">MKV</button>
          <button type="button" data-format="mp4" aria-pressed="false" title="Fragmented MP4: plays everywhere">MP4</button>
        </div>
      </div>
      <p class="hint st-why" data-ref="recWhy" role="status"></p>
    </ss-card>
  </section>

  <section class="st-meters" role="group" aria-label="Live meters" data-ref="meters"></section>

  <div class="st-grid">
    <ss-card class="st-card st-dests" heading="Destinations" icon="server" data-ref="destCard">
      <ss-button slot="actions" size="sm" variant="ghost" icon="plus" data-ref="addBtn">Add destination</ss-button>
      <div class="st-dest-list" role="list" aria-label="Destinations" data-ref="destList"></div>
      <form class="st-form" data-ref="form" autocomplete="off" hidden>
        <h3 class="st-form-title" data-ref="formTitle">Add a destination</h3>
        <div class="st-field">
          <label for="st-f-type">Type</label>
          <select id="st-f-type" class="text-input" data-ref="fType"></select>
          <p class="hint" data-ref="fTypeHint"></p>
        </div>
        <div class="st-field">
          <label for="st-f-name">Name</label>
          <input id="st-f-name" class="text-input" data-ref="fName" type="text" maxlength="60" placeholder="e.g. Twitch" autocomplete="off">
        </div>
        <div class="st-field" data-ref="fUrlWrap">
          <label for="st-f-url" data-ref="fUrlLabel">URL</label>
          <input id="st-f-url" class="text-input" data-ref="fUrl" type="text" inputmode="url" spellcheck="false" autocomplete="off" placeholder="rtmp://live.example.com/app">
          <p class="hint" data-ref="fUrlHint"></p>
        </div>
        <div class="st-field" data-ref="fKeyWrap">
          <label for="st-f-key" data-ref="fKeyLabel">Stream key</label>
          <input id="st-f-key" class="text-input" data-ref="fKey" type="password" autocomplete="new-password" spellcheck="false" autocapitalize="off" placeholder="Paste it here">
          <p class="hint" data-ref="fKeyHint">Kept privately on this machine and never shown again. A reference such as env:TWITCH_KEY uses a vault instead.</p>
          <label class="st-check" data-ref="fKeyClearWrap" hidden><input type="checkbox" data-ref="fKeyClear"> Remove the stored key</label>
        </div>
        <div class="st-field" data-ref="fModeWrap" hidden>
          <label for="st-f-mode" data-ref="fModeLabel">Mode</label>
          <select id="st-f-mode" class="text-input" data-ref="fMode"></select>
        </div>
        <div class="st-field" data-ref="fLatencyWrap" hidden>
          <label for="st-f-latency">Latency (ms)</label>
          <input id="st-f-latency" class="text-input" data-ref="fLatency" type="number" min="20" max="8000" step="10" value="120">
          <p class="hint">How long the receiver may wait for a lost packet. Lower is faster and less forgiving.</p>
        </div>
        <div class="st-field" data-ref="fCodecWrap">
          <label for="st-f-codec">Codec for this destination</label>
          <select id="st-f-codec" class="text-input" data-ref="fCodec"></select>
        </div>
        <p class="hint" data-state="error" role="alert" data-ref="fError" hidden></p>
        <div class="st-form-actions">
          <ss-button variant="primary" icon="check" data-ref="fSave">Save destination</ss-button>
          <ss-button variant="ghost" icon="x" data-ref="fCancel">Cancel</ss-button>
        </div>
      </form>
    </ss-card>

    <div class="st-col">
      <ss-card class="st-card" heading="Live controls" icon="sliders" data-ref="liveCard">
        <div slot="actions" data-ref="livePill"></div>
        <p class="hint st-lead">These change the running stream with no reconnect: a second encoder starts and takes over at a planned keyframe. Video bitrate applies to Constant and Variable; quality to Quality (CQP).</p>
        <div class="seg" role="radiogroup" aria-label="Rate control" data-ref="rate"></div>
        <div class="st-faders">
          <div class="st-fader" data-fader="bitrate_kbps">
            <ss-fader data-ref="fBitrate" orientation="horizontal" label="Video bitrate" unit="kbps" min="500" max="25000" step="100" default="6000" ticks="500,5000,10000,15000,20000,25000" tick-labels="ends"></ss-fader>
            <ss-button size="sm" variant="ghost" data-ref="bitrateAuto" aria-label="Automatic video bitrate">Auto</ss-button>
          </div>
          <div class="st-fader" data-fader="keyint_s">
            <ss-fader data-ref="fKeyint" orientation="horizontal" label="Keyframe interval" unit="s" min="0.5" max="10" step="0.5" decimals="1" default="2" ticks="0.5,2,4,6,8,10" tick-labels="ends"></ss-fader>
          </div>
          <div class="st-fader" data-fader="quality">
            <ss-fader data-ref="fQuality" orientation="horizontal" label="Quality (QP, lower is sharper)" unit="QP" min="1" max="63" step="1" default="28" ticks="1,16,32,48,63" tick-labels="ends"></ss-fader>
            <ss-button size="sm" variant="ghost" data-ref="qualityAuto" aria-label="Automatic quality">Auto</ss-button>
          </div>
          <div class="st-fader" data-fader="audio_bitrate_kbps">
            <ss-fader data-ref="fAudio" orientation="horizontal" label="Audio bitrate" unit="kbps" min="32" max="320" step="8" default="128" ticks="32,128,224,320" tick-labels="ends"></ss-fader>
          </div>
        </div>
        <p class="hint" data-ref="liveNote" role="status"></p>
        <p class="hint" data-ref="audioNote" hidden></p>
      </ss-card>

      <ss-card class="st-card" heading="Encoder" icon="system" data-ref="encCard">
        <div class="st-label" id="st-enc-l">Encoder (changes live)</div>
        <div class="st-encoders" role="radiogroup" aria-labelledby="st-enc-l" data-ref="encoders"></div>
        <div class="st-label">Transports on this host</div>
        <div class="st-transports" data-ref="transports"></div>
        <ul class="st-reasons" data-ref="reasons"></ul>
      </ss-card>

      <ss-card class="st-card" heading="Picture and codec" icon="monitor" data-ref="picCard">
        <div class="st-reconnect" data-ref="reconnectNote">
          <span class="st-reconnect-ic" data-ref="reconnectIc"></span>
          <p data-ref="reconnectText"></p>
        </div>
        <div class="st-label" id="st-codec-l">Codec</div>
        <div class="seg" role="radiogroup" aria-labelledby="st-codec-l" data-ref="codec"></div>
        <div class="st-pair">
          <div class="st-field">
            <label for="st-tier">Resolution</label>
            <select id="st-tier" class="text-input" data-ref="tier"></select>
          </div>
          <div class="st-field">
            <label for="st-fps">Frame rate</label>
            <select id="st-fps" class="text-input" data-ref="fps"></select>
          </div>
        </div>
        <div class="st-apply">
          <ss-button variant="primary" icon="check" data-ref="applyBtn" disabled>Apply</ss-button>
          <ss-button variant="ghost" icon="undo" data-ref="revertBtn" hidden>Discard</ss-button>
          <span class="hint" data-ref="outputNote"></span>
        </div>
      </ss-card>
    </div>
  </div>

  <ss-card class="st-card" heading="Events" icon="list" data-ref="eventCard">
    <ul class="st-events" aria-label="Recent events" data-ref="events"></ul>
    <p class="hint" data-ref="noEvents">Nothing yet. Starts, stops, reconnects and encoder swaps show here.</p>
  </ss-card>
</div>`;

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const kbps = (v) => (v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)} Mbps` : `${Math.round(v)} kbps`);
const pad2 = (n) => String(n).padStart(2, '0');
const clock = (s) => `${pad2(Math.floor(s / 3600))}:${pad2(Math.floor(s / 60) % 60)}:${pad2(Math.floor(s) % 60)}`;
const bytes = (n) => {
  if (!(n > 0)) return '0 MB';
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  return `${(n / 1e6).toFixed(n >= 1e8 ? 0 : 1)} MB`;
};
const timeOfDay = (at) => new Date(at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const baseName = (path) => String(path || '').split('/').pop();
const slug = (text) => String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 24);
const svg = (tag, attrs = {}) => {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
};

/** The meter tiles: how each reads from the state. `warn` marks a tile worth a look. */
const METERS = [
  {
    id: 'fps', label: 'Encoder fps', icon: 'pulse',
    read: (s) => (s.encoders.length ? Math.min(...s.encoders.map((e) => e.fps)) : null),
    text: (v) => v.toFixed(1), sub: (s) => (s.output.fps ? `of ${Number(s.output.fps).toFixed(0)} wanted` : ''),
    warn: (v, s) => s.output.fps > 0 && v < s.output.fps * 0.9,
  },
  {
    id: 'bitrate', label: 'Bitrate', icon: 'wave',
    read: (s) => (s.bitrate_kbps > 0 ? s.bitrate_kbps : null),
    text: (v) => kbps(v), sub: (s) => (s.settings.rate_control === 'cqp' ? 'constant quality' : s.settings.bitrate_kbps ? `target ${kbps(s.settings.bitrate_kbps)}` : 'automatic target'),
  },
  {
    id: 'dropped', label: 'Dropped frames', icon: 'alert',
    read: (s) => (s.encoders.length || s.destinations.some((d) => d.active) ? s.dropped_frames : null),
    text: (v) => String(v), sub: () => 'at the sinks and encoders', warn: (v) => v > 0,
  },
  {
    id: 'latency', label: 'Latency', icon: 'clock',
    read: (s) => (s.latency_ms > 0 ? s.latency_ms : null),
    text: (v) => `${Math.round(v)} ms`, sub: () => 'estimate, camera to network',
  },
  {
    id: 'cpu', label: 'CPU', icon: 'system',
    read: (s) => s.cpu_percent, text: (v) => `${v.toFixed(0)} %`, sub: () => 'encoders, of one core', warn: (v) => v > 300,
  },
  {
    id: 'gpu', label: 'GPU', icon: 'monitor',
    read: (s) => s.gpu_percent, text: (v) => `${v.toFixed(0)} %`, sub: () => 'busiest engine', warn: (v) => v > 90,
  },
];

export async function mount(host, ctx) {
  const { store, act, actStream, flushStream, registerCommands, icon } = ctx;
  await ctx.ensureCss(new URL('./common.css', import.meta.url).href);
  await ctx.ensureCss(new URL('./stream.css', import.meta.url).href);
  document.title = 'Stream · StreamStack';
  host.innerHTML = TEMPLATE;
  const R = {};
  host.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  const off = [];
  const sub = (selector, cb) => off.push(store.subscribe(selector, cb));
  const S = () => store.state.stream;
  let disposed = false;
  let recFormat = 'mkv';
  const busy = new Set();

  const live = (s = S()) => s.destinations.filter((d) => d.active && d.kind !== 'record');
  const liveKinds = (s = S()) => s.destinations.filter((d) => d.kind !== 'record');
  const swapping = (s = S()) => s.encoders.some((e) => e.swapping);
  const available = (s = S()) => s.status !== 'unavailable';

  // ---------- messages from the server, kept visible (toasts vanish) ----------
  const notes = { go: '', rec: '', live: '' };
  const say = (key, text) => { notes[key] = text || ''; renderNotes(); };

  async function run(key, action, params, ok) {
    if (busy.has(key)) return null;
    busy.add(key);
    renderHero();
    const res = await act(action, params, { quiet: true });
    busy.delete(key);
    if (disposed) return res;
    if (res?.message) {
      say(key === 'rec' ? 'rec' : 'go', res.message);
      if (ok) toast.success(res.message, { timeout: 3000, id: `stream:${key}` });
    }
    renderHero();
    return res;
  }

  // ---------- hero: stream and record ----------
  function renderHero() {
    const s = S();
    const status = STATUS[s.status] ?? STATUS.idle;
    const onAir = live(s);
    const streaming = onAir.length > 0;
    const targets = liveKinds(s).filter((d) => d.enabled);
    R.streamState.dataset.state = streaming ? status.state : s.status === 'error' ? 'fail' : 'off';
    R.streamWord.textContent = streaming || s.status === 'error' || s.status === 'unavailable' ? status.word : 'Offline';
    R.streamClock.hidden = !streaming;
    R.streamClock.textContent = streaming ? clock(s.uptime_s) : '';
    R.streamCard.setAttribute('tone', streaming && s.status === 'live' ? 'live' : streaming || s.status === 'error' ? 'warn' : '');
    if (streaming) {
      const up = onAir.filter((d) => d.state === 'live').length;
      R.streamSub.textContent = `${up} of ${plural(onAir.length, 'destination')} live · ${CODEC_NAMES[s.encoders[0]?.codec] ?? ''} ${s.output.width}×${s.output.height} at ${Number(s.output.fps).toFixed(0)} fps`.replace('  ', ' ');
    } else if (s.status === 'unavailable') {
      R.streamSub.textContent = 'Streaming is not part of this rig.';
    } else {
      R.streamSub.textContent = targets.length ? `Ready: ${targets.map((d) => d.name).join(', ')}.` : 'No destination yet.';
    }
    R.goBtn.hidden = streaming;
    R.stopBtn.hidden = !streaming;
    const cannot = !available(s) ? 'Streaming is not part of this rig.'
      : !targets.length ? 'Add a destination to go live.'
        : s.status === 'error' ? 'An encoder failed; see the events below.' : '';
    R.goBtn.toggleAttribute('disabled', !!cannot);
    R.goBtn.toggleAttribute('busy', busy.has('go'));
    R.stopBtn.toggleAttribute('busy', busy.has('stop'));
    R.goWhy.dataset.state = cannot ? 'warn' : '';
    R.goWhy.textContent = cannot || notes.go;

    const rec = s.recording;
    R.recordState.dataset.state = rec.active ? 'rec' : 'off';
    R.recordWord.textContent = rec.active ? 'Recording' : 'Not recording';
    R.recordClock.hidden = !rec.active;
    R.recordClock.textContent = rec.active ? clock(rec.seconds) : '';
    R.recordCard.setAttribute('tone', rec.active ? 'live' : '');
    R.recordSub.textContent = rec.active ? `${baseName(rec.file)} · ${bytes(rec.bytes)}${rec.free_mb != null ? ` · ${rec.free_mb >= 10240 ? `${Math.round(rec.free_mb / 1024)} GB` : `${rec.free_mb} MB`} free${rec.minutes_left != null ? ` (about ${Math.round(rec.minutes_left)} min)` : ''}` : ''}${rec.raw_mic ? ' · raw mic track' : ''}` : rec.stopped ? `Stopped: ${rec.stopped}. Everything recorded so far is saved.` : `Saves the program to a ${recFormat.toUpperCase()} file on this machine.`;
    R.recBtn.hidden = rec.active;
    R.recStopBtn.hidden = !rec.active;
    R.recBtn.toggleAttribute('disabled', !available(s));
    R.recBtn.toggleAttribute('busy', busy.has('rec'));
    R.recStopBtn.toggleAttribute('busy', busy.has('recstop'));
    R.recFormat.querySelectorAll('button').forEach((b) => {
      const format = rec.active ? rec.format : recFormat;
      b.setAttribute('aria-pressed', String(b.dataset.format === format));
      b.disabled = rec.active;
    });
    const recWhy = !available(s) ? 'Recording is not part of this rig.' : rec.last_error ? rec.last_error : '';
    R.recWhy.dataset.state = recWhy ? 'warn' : '';
    R.recWhy.textContent = recWhy || notes.rec;
  }

  R.recFormat.addEventListener('click', (ev) => {
    const b = ev.target.closest('button[data-format]');
    if (!b || S().recording.active) return;
    recFormat = b.dataset.format;
    renderHero();
  });
  R.goBtn.addEventListener('click', () => run('go', 'stream.start', {}, false));
  R.stopBtn.addEventListener('click', () => run('stop', 'stream.stop', {}, false));
  R.recBtn.addEventListener('click', () => run('rec', 'record.start', { format: recFormat }, false));
  R.recStopBtn.addEventListener('click', () => run('recstop', 'record.stop', {}, false));
  const renderNotes = () => renderHero();
  sub((s) => [s.stream.status, s.stream.uptime_s, s.stream.output, s.stream.recording, s.stream.destinations.map((d) => [d.state, d.active, d.enabled, d.kind]), s.stream.encoders.map((e) => e.codec)], renderHero);

  // ---------- meters ----------
  const history = Object.fromEntries(METERS.map((m) => [m.id, []]));
  const tiles = new Map();
  for (const m of METERS) {
    const tile = h('div', { class: 'st-meter', 'data-meter': m.id, role: 'group', 'aria-label': m.label },
      h('div', { class: 'st-meter-head' }, icon(m.icon), h('span', {}, m.label)),
      h('output', { class: 'st-meter-value' }, '–'),
      h('span', { class: 'st-meter-sub' }),
      svg('svg', { class: 'st-spark', viewBox: '0 0 120 28', preserveAspectRatio: 'none', 'aria-hidden': 'true' }));
    tiles.set(m.id, tile);
    R.meters.append(tile);
  }

  function spark(el, values, warn) {
    el.replaceChildren();
    if (values.length < 2) return;
    const max = Math.max(...values, 1e-9);
    const min = Math.min(...values, 0);
    const span = max - min || 1;
    const points = values.map((v, i) => `${((i / (HISTORY - 1)) * 120).toFixed(1)},${(26 - ((v - min) / span) * 24).toFixed(1)}`);
    el.append(svg('polyline', { points: points.join(' '), fill: 'none', 'stroke-width': 1.6, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke', class: warn ? 'warn' : '' }));
  }

  function renderMeters() {
    const s = S();
    const active = s.encoders.length > 0 || s.destinations.some((d) => d.active);
    for (const m of METERS) {
      const tile = tiles.get(m.id);
      const value = active ? m.read(s) : null;
      const series = history[m.id];
      if (value === null || value === undefined || Number.isNaN(value)) {
        if (!active) series.length = 0;
      } else {
        series.push(value);
        if (series.length > HISTORY) series.shift();
      }
      const known = value !== null && value !== undefined && !Number.isNaN(value);
      const warn = known && !!m.warn?.(value, s);
      tile.dataset.state = warn ? 'warn' : known ? 'ok' : 'off';
      tile.querySelector('.st-meter-value').textContent = known ? m.text(value) : (active ? 'n/a' : '–');
      tile.querySelector('.st-meter-sub').textContent = known ? m.sub(s) : active ? 'not reported by this host' : 'idle';
      spark(tile.querySelector('.st-spark'), series, warn);
    }
  }
  sub((s) => s.stream, renderMeters);

  // ---------- destinations ----------
  const chip = (text, title) => h('span', { class: 'st-chip', title }, text);

  function destCreate() {
    const row = h('div', { class: 'st-dest', role: 'listitem' });
    row.append(
      h('div', { class: 'st-dest-main' },
        h('div', { class: 'st-dest-head' },
          h('span', { class: 'st-dest-icon' }),
          h('strong', { class: 'st-dest-name' }),
          h('span', { class: 'pill st-dest-pill' })),
        h('div', { class: 'st-dest-meta' }),
        h('div', { class: 'st-dest-stats' }),
        h('p', { class: 'st-dest-error', role: 'status' })),
      h('div', { class: 'st-dest-tools' },
        h('ss-toggle', { class: 'st-dest-enable', label: 'In Go live' }),
        h('ss-button', { size: 'sm', variant: 'default', icon: 'play', 'data-role': 'start' }, 'Start'),
        h('ss-button', { size: 'sm', variant: 'danger', icon: 'stop', hold: '700', 'data-role': 'stop' }, 'Stop'),
        h('ss-button', { size: 'sm', variant: 'ghost', icon: 'sliders', 'data-role': 'edit' }, 'Edit'),
        h('ss-button', { size: 'sm', variant: 'ghost', icon: 'trash', hold: '800', 'data-role': 'remove' }, 'Remove')));
    row.querySelector('ss-toggle').addEventListener('change', (ev) => {
      const d = row._dest;
      if (d) saveDestinations(S().destinations.map((x) => (x.id === d.id ? { ...x, enabled: ev.detail.checked } : x)), { quiet: true });
    });
    row.querySelector('[data-role="start"]').addEventListener('click', () => row._dest && run(`start:${row._dest.id}`, 'stream.start', { destinations: [row._dest.id] }, false));
    row.querySelector('[data-role="stop"]').addEventListener('click', () => row._dest && run(`stopd:${row._dest.id}`, 'stream.stop', { destination: row._dest.id }, false));
    row.querySelector('[data-role="edit"]').addEventListener('click', () => row._dest && openForm(row._dest));
    row.querySelector('[data-role="remove"]').addEventListener('click', () => row._dest && removeDestination(row._dest));
    return row;
  }

  function destUpdate(row, d) {
    row._dest = d;
    const isLive = d.kind !== 'record';
    row.dataset.health = d.active ? d.health : 'idle';
    row.querySelector('.st-dest-icon').replaceChildren(icon(d.kind === 'record' ? 'record' : 'broadcast'));
    row.querySelector('.st-dest-name').textContent = d.name;
    const pill = row.querySelector('.st-dest-pill');
    const word = d.active ? DEST_STATE[d.state] ?? d.state : d.enabled ? 'Ready' : 'Not in Go live';
    pill.textContent = word;
    pill.dataset.state = d.active ? DEST_PILL[d.health] ?? '' : '';
    const mode = d.options?.mode;
    const kindName = d.kind === 'rtmp' ? (mode === 'classic' ? 'RTMP' : mode === 'enhanced' ? 'Enhanced RTMP only' : 'Enhanced RTMP') : KIND_NAMES[d.kind] ?? d.kind;
    const meta = [chip(kindName, 'Transport')];
    if (d.url) meta.push(h('span', { class: 'st-dest-url', title: d.url }, d.url));
    meta.push(chip(d.key === 'set' ? 'Key set' : 'No key', d.key === 'set' ? 'A key is stored; it is never shown' : 'No key or passphrase is stored'));
    if (d.active && d.codec) meta.push(chip(`${CODEC_NAMES[d.codec] ?? d.codec}${d.moved ? ' (fallback)' : ''}`, 'Codec in use'));
    else if (d.codec_wanted && d.codec_wanted !== 'auto') meta.push(chip(CODEC_NAMES[d.codec_wanted] ?? d.codec_wanted, 'Codec wanted'));
    row.querySelector('.st-dest-meta').replaceChildren(...meta);
    const stats = [];
    if (d.active) {
      if (d.state === 'live') stats.push(`Connected ${clock(d.connected_s)}`);
      if (d.bitrate_kbps) stats.push(kbps(d.bitrate_kbps));
      if (d.latency_ms) stats.push(`${Math.round(d.latency_ms)} ms`);
      if (d.kind === 'whip' && d.whip?.rtt_ms != null) stats.push(`RTT ${Math.round(d.whip.rtt_ms)} ms`);
      if (d.kind === 'whip' && d.state === 'live') stats.push(`${d.whip?.loss_percent ?? 0}% loss`);
      stats.push(`${plural(d.dropped_frames, 'drop')}`);
      if (d.restarts) stats.push(`${plural(d.restarts, 'reconnect')}`);
    }
    row.querySelector('.st-dest-stats').textContent = stats.join(' · ');
    const err = row.querySelector('.st-dest-error');
    const note = d.state === 'backoff' ? `Reconnecting. ${d.last_error}` : d.last_error;
    err.textContent = d.active || d.last_error ? note : '';
    err.hidden = !err.textContent;
    err.dataset.state = d.state === 'failed' ? 'fail' : 'warn';
    const toggle = row.querySelector('ss-toggle');
    toggle.checked = d.enabled;
    toggle.hidden = !isLive;
    const locked = d.active;
    const start = row.querySelector('[data-role="start"]');
    const stop = row.querySelector('[data-role="stop"]');
    start.hidden = !isLive || d.active || !available();
    stop.hidden = !isLive || !d.active;
    start.toggleAttribute('busy', busy.has(`start:${d.id}`));
    stop.toggleAttribute('busy', busy.has(`stopd:${d.id}`));
    for (const role of ['edit', 'remove']) {
      const b = row.querySelector(`[data-role="${role}"]`);
      b.toggleAttribute('disabled', locked);
      b.setAttribute('title', locked ? 'Stop this destination first' : role === 'edit' ? `Edit ${d.name}` : `Hold to remove ${d.name}`);
      b.setAttribute('aria-label', role === 'edit' ? `Edit ${d.name}` : `Hold to remove ${d.name}`);
    }
    toggle.setAttribute('aria-label', `Include ${d.name} when going live`);
    start.setAttribute('aria-label', `Start ${d.name}`);
    stop.setAttribute('aria-label', `Hold to stop ${d.name}`);
  }

  function recordingRow() {
    const row = h('div', { class: 'st-dest st-dest-record', 'data-pinned': '' });
    row.append(
      h('div', { class: 'st-dest-main' },
        h('div', { class: 'st-dest-head' }, h('span', { class: 'st-dest-icon' }, icon('record')), h('strong', { class: 'st-dest-name' }, 'Recording'), h('span', { class: 'pill st-dest-pill' })),
        h('div', { class: 'st-dest-meta' }, chip('Local file', 'Written on this machine')),
        h('div', { class: 'st-dest-stats' })));
    return row;
  }
  const recRow = recordingRow();
  R.destList.before(recRow);

  function renderDestinations() {
    const s = S();
    const dests = s.destinations;
    renderList(R.destList, dests, (d) => d.id, destCreate, destUpdate);
    const rec = s.recording;
    const pill = recRow.querySelector('.st-dest-pill');
    pill.textContent = rec.active ? 'Recording' : 'Idle';
    pill.dataset.state = rec.active ? (rec.state === 'live' || !rec.state ? 'ok' : 'warn') : '';
    recRow.querySelector('.st-dest-stats').textContent = rec.active ? `${baseName(rec.file)} · ${bytes(rec.bytes)} · ${clock(rec.seconds)}` : 'Starts with the Record button above.';
    R.addBtn.toggleAttribute('disabled', !available(s));
    const empty = !dests.length;
    R.destList.dataset.empty = String(empty);
  }
  sub((s) => s.stream, renderDestinations);

  /** The destination list as the server wants it: everything the state shows, no key (the server keeps it). */
  const portable = (d) => ({
    id: d.id, kind: d.kind, name: d.name, url: d.url, codec: d.codec_wanted || 'auto', options: { ...d.options }, enabled: d.enabled,
  });

  async function saveDestinations(list, { quiet = false } = {}) {
    const res = await act('stream.set', { destinations: list.map((d) => ({ ...portable(d), ...(d.key !== undefined && d.key !== 'set' && d.key !== 'none' ? { key: d.key } : {}), ...(d.key_ref !== undefined ? { key_ref: d.key_ref } : {}) })) }, { quiet: true });
    if (res && !quiet && res.message) say('go', res.message);
    return res;
  }

  async function removeDestination(d) {
    const res = await saveDestinations(S().destinations.filter((x) => x.id !== d.id), { quiet: true });
    if (res) toast(`Removed ${d.name}.`, { level: 'info', timeout: 3000, id: 'stream:remove' });
  }

  // ---------- add / edit form ----------
  let editing = null;      // the destination being edited, or null for a new one
  const TYPES = [
    { id: 'rtmp-enhanced', label: 'Enhanced RTMP (AV1, HEVC, falls back to H.264)', kind: 'rtmp', mode: 'auto' },
    { id: 'rtmp', label: 'RTMP (classic, H.264)', kind: 'rtmp', mode: 'classic' },
    { id: 'rtmp-strict', label: 'Enhanced RTMP only (no fallback)', kind: 'rtmp', mode: 'enhanced' },
    { id: 'srt', label: 'SRT', kind: 'srt' },
    { id: 'whip', label: 'WHIP (WebRTC)', kind: 'whip' },
  ];
  const typeOf = (d) => (d.kind === 'rtmp' ? (d.options?.mode === 'classic' ? 'rtmp' : d.options?.mode === 'enhanced' ? 'rtmp-strict' : 'rtmp-enhanced') : d.kind);
  const transport = (id) => S().probe?.transports.find((t) => t.id === id);
  const whipReason = () => {
    const t = transport('whip');
    return t && !t.ok ? t.reason || 'WHIP is not available on this host.' : '';
  };

  function fillTypes(selected) {
    R.fType.replaceChildren(...TYPES.map((t) => {
      const blocked = t.id === 'whip' && !!whipReason();
      return h('option', { value: t.id, disabled: blocked && t.id !== selected, selected: t.id === selected }, blocked ? `${t.label} (unavailable)` : t.label);
    }));
    R.fType.value = selected;
  }

  function fillCodecs(kind, selected) {
    const base = kind === 'srt' ? ['auto', 'hevc', 'h264', 'av1'] : kind === 'whip' ? ['auto', 'h264', 'hevc', 'av1'] : CODECS;
    R.fCodec.replaceChildren(...base.map((c) => h('option', { value: c }, c === 'auto' ? 'Automatic (best the host allows)' : CODEC_NAMES[c])));
    R.fCodec.value = base.includes(selected) ? selected : 'auto';
  }

  function syncForm() {
    const type = TYPES.find((t) => t.id === R.fType.value) ?? TYPES[0];
    const kind = type.kind;
    const whipOk = kind === 'whip' && !whipReason();
    R.fTypeHint.textContent = whipReason() ? `WHIP is unavailable: ${whipReason()}` : whipOk ? 'StreamStack publishes H.264 and Opus over WebRTC; HEVC and AV1 only if the server accepts them.' : '';
    R.fTypeHint.dataset.state = whipReason() ? 'warn' : '';
    R.fUrl.placeholder = { rtmp: 'rtmp://live.example.com/app', srt: 'srt://host.example.com:9000', whip: 'https://whip.example.com/endpoint' }[kind];
    R.fUrlHint.textContent = { rtmp: 'Ends at the application name; the key goes in the field below, never in the address.', srt: 'Host and port.', whip: 'The ingest endpoint.' }[kind];
    R.fKeyLabel.textContent = { rtmp: 'Stream key', srt: 'Passphrase (optional)', whip: 'Bearer token (optional)' }[kind];
    R.fKey.placeholder = editing?.key === 'set' ? 'A key is set; type a new one to replace it' : kind === 'rtmp' ? 'Paste the stream key' : 'Optional';
    R.fKeyClearWrap.hidden = !(editing && editing.key === 'set');
    R.fModeWrap.hidden = kind !== 'srt';
    R.fLatencyWrap.hidden = kind !== 'srt';
    if (kind === 'srt') {
      R.fModeLabel.textContent = 'Connection';
      if (!R.fMode.options.length || R.fMode.dataset.kind !== 'srt') {
        R.fMode.replaceChildren(h('option', { value: 'caller' }, 'Call the receiver (caller)'), h('option', { value: 'listener' }, 'Wait for the receiver (listener)'));
        R.fMode.dataset.kind = 'srt';
      }
    }
    fillCodecs(kind, R.fCodec.value || 'auto');
  }
  R.fType.addEventListener('change', syncForm);

  function openForm(d) {
    editing = d || null;
    R.form.hidden = false;
    R.formTitle.textContent = d ? `Edit ${d.name}` : 'Add a destination';
    fillTypes(d ? typeOf(d) : 'rtmp-enhanced');
    R.fName.value = d?.name ?? '';
    R.fUrl.value = d?.url ?? '';
    R.fKey.value = '';
    R.fKeyClear.checked = false;
    R.fLatency.value = d?.options?.latency_ms ?? 120;
    fillCodecs(d ? portable(d).kind : 'rtmp', d?.codec_wanted ?? 'auto');
    syncForm();
    if (R.fMode.dataset.kind === 'srt') R.fMode.value = d?.options?.mode ?? 'caller';
    R.fCodec.value = d?.codec_wanted ?? 'auto';
    R.fError.hidden = true;
    R.fType.disabled = !!d;      // the transport of an existing destination does not change; remove and add instead
    R.fName.focus();
    R.form.scrollIntoView({ block: 'nearest' });
  }

  function closeForm() {
    R.fKey.value = '';
    R.form.hidden = true;
    editing = null;
  }

  function uniqueId(name, kind) {
    const taken = new Set(S().destinations.map((d) => d.id));
    const base = /^[a-z]/.test(slug(name)) ? slug(name) : `${kind}-${slug(name) || 'dest'}`.slice(0, 24);
    let id = base;
    for (let n = 2; taken.has(id); n += 1) id = `${base.slice(0, 28)}-${n}`;
    return id;
  }

  R.addBtn.addEventListener('click', () => openForm(null));
  R.fCancel.addEventListener('click', closeForm);
  R.form.addEventListener('submit', (ev) => ev.preventDefault());
  R.fSave.addEventListener('click', async () => {
    const type = TYPES.find((t) => t.id === R.fType.value) ?? TYPES[0];
    const name = R.fName.value.trim();
    const url = R.fUrl.value.trim();
    const fail = (text) => { R.fError.textContent = text; R.fError.hidden = false; };
    if (!name) { fail('Give the destination a name.'); R.fName.focus(); return; }
    if (!url) { fail('Enter the address of the destination.'); R.fUrl.focus(); return; }
    const options = { ...(editing?.options ?? {}) };
    if (type.kind === 'rtmp') options.mode = type.mode;
    if (type.kind === 'srt') {
      options.mode = R.fMode.value;
      options.latency_ms = Number(R.fLatency.value) || 120;
    }
    const entry = {
      id: editing?.id ?? uniqueId(name, type.kind), kind: type.kind, name, url, codec: R.fCodec.value, options, enabled: editing?.enabled ?? true,
    };
    const typed = R.fKey.value.trim();
    if (typed) {
      if (REF_FORM.test(typed)) entry.key_ref = typed; else entry.key = typed;
    } else if (R.fKeyClear.checked) {
      entry.key = '';
    }
    const others = S().destinations.filter((d) => d.id !== editing?.id).map((d) => ({ ...portable(d) }));
    const list = editing
      ? S().destinations.map((d) => (d.id === editing.id ? entry : portable(d)))
      : [...others, entry];
    R.fSave.setAttribute('busy', '');
    const res = await act('stream.set', { destinations: list }, { quiet: true });
    R.fSave.removeAttribute('busy');
    if (disposed) return;
    if (res) {
      closeForm();
      toast.success(`${editing ? 'Saved' : 'Added'} ${name}.`, { timeout: 2500, id: 'stream:dest' });
    } else {
      fail('The server did not accept this destination; the notice at the top of the page says why.');
    }
  });

  // ---------- live controls ----------
  const faders = {
    bitrate_kbps: R.fBitrate, keyint_s: R.fKeyint, quality: R.fQuality, audio_bitrate_kbps: R.fAudio,
  };
  const pending = {};
  let lastSent = 0;
  let gateTimer = 0;

  /** Send what the controls hold, at most one change per hot swap: the server refuses a second while one runs. */
  function pump() {
    clearTimeout(gateTimer);
    gateTimer = 0;
    const fields = Object.keys(pending);
    if (!fields.length || disposed) return;
    const wait = LIVE_GAP_MS - (performance.now() - lastSent);
    if (swapping() || wait > 0) {
      gateTimer = setTimeout(pump, swapping() ? 200 : wait + 20);
      return;
    }
    const batch = { ...pending };
    for (const k of fields) delete pending[k];
    lastSent = performance.now();
    actStream('stream.set', batch);
    flushStream('stream.set');
    say('live', '');
  }

  /** Hold a live change and send it when the gate is open. The controls show it at once, held against server state. */
  function liveSet(values) {
    Object.assign(pending, values);
    store.patch(Object.fromEntries(Object.entries(values).map(([k, v]) => [`stream.settings.${k}`, v])), { hold: 4000 });
    pump();
  }

  for (const [field, fader] of Object.entries(faders)) {
    fader.addEventListener('input', (ev) => liveSet({ [field]: ev.detail.value }));
  }
  R.bitrateAuto.addEventListener('click', () => {
    const s = S();
    liveSet({ bitrate_kbps: s.settings.bitrate_kbps ? 0 : Math.round(currentVideoKbps() / 100) * 100 || 6000 });
  });
  R.qualityAuto.addEventListener('click', () => {
    const s = S();
    liveSet({ quality: s.settings.quality ? 0 : 28 });
  });
  const currentVideoKbps = () => S().encoders[0]?.bitrate_kbps ?? 0;

  R.rate.addEventListener('click', (ev) => {
    const b = ev.target.closest('button[data-rate]');
    if (b && !b.disabled) liveSet({ rate_control: b.dataset.rate });
  });
  R.rate.replaceChildren(...RATE_CONTROLS.map(([id, label]) => h('button', { type: 'button', role: 'radio', 'data-rate': id, 'aria-checked': 'false', 'aria-pressed': 'false' }, label)));

  function renderLive() {
    const s = S();
    const st = s.settings;
    const cqp = st.rate_control === 'cqp';
    R.rate.querySelectorAll('button').forEach((b) => {
      const on = b.dataset.rate === st.rate_control;
      b.setAttribute('aria-pressed', String(on));
      b.setAttribute('aria-checked', String(on));
      b.disabled = !available(s);
    });
    const auto = st.bitrate_kbps === 0;
    R.fBitrate.value = auto ? Math.max(500, Math.round((currentVideoKbps() || 6000) / 100) * 100) : st.bitrate_kbps;
    R.fBitrate.toggleAttribute('disabled', cqp || !available(s));
    R.bitrateAuto.toggleAttribute('pressed', auto);
    R.bitrateAuto.setAttribute('aria-pressed', String(auto));
    R.bitrateAuto.toggleAttribute('disabled', cqp || !available(s));
    R.fKeyint.value = st.keyint_s;
    R.fKeyint.toggleAttribute('disabled', !available(s));
    R.fQuality.value = st.quality || 28;
    R.fQuality.toggleAttribute('disabled', !cqp || !available(s));
    R.qualityAuto.toggleAttribute('pressed', st.quality === 0);
    R.qualityAuto.setAttribute('aria-pressed', String(st.quality === 0));
    R.qualityAuto.toggleAttribute('disabled', !cqp || !available(s));
    R.fAudio.value = st.audio_bitrate_kbps;
    R.fAudio.toggleAttribute('disabled', !available(s));

    // the pill: what the encoder is doing about the last change
    const applying = swapping(s) || Object.keys(pending).length > 0;
    const last = s.last_swap;
    let pill;
    if (applying) pill = h('span', { class: 'pill', 'data-state': 'info' }, icon('loader', 'icon spin'), 'Applying');
    else if (last && last.ok === false) pill = h('span', { class: 'pill', 'data-state': 'warn', title: last.reason }, icon('alert'), 'Not applied');
    else pill = h('span', { class: 'pill', 'data-state': live(s).length || s.recording.active ? 'ok' : '' }, icon('check'), live(s).length || s.recording.active ? 'Live' : 'Saved');
    R.livePill.replaceChildren(pill);
    const bits = [];
    if (last && last.ok === false) bits.push(`The last change was not applied and the previous encoder keeps running: ${last.reason}`);
    else if (last && last.ok) bits.push(`Last swap${last.encoder ? ` to ${last.encoder}` : ''}${last.gap_ms ? ` with a ${last.gap_ms} ms gap` : ' without a gap'} at ${timeOfDay(last.at)}.`);
    if (!live(s).length && !s.recording.active && available(s)) bits.push('Not streaming: changes are saved and apply when the stream starts.');
    R.liveNote.textContent = notes.live || bits.join(' ');
    R.liveNote.dataset.state = last && last.ok === false ? 'warn' : '';
    const audio = s.audio;
    R.audioNote.hidden = !audio.error && audio.ok !== false;
    R.audioNote.textContent = audio.error ? `Audio: ${audio.error}` : '';
    R.audioNote.dataset.state = audio.error ? 'warn' : '';
  }
  sub((s) => [s.stream.settings, s.stream.last_swap, s.stream.encoders.map((e) => [e.swapping, e.bitrate_kbps]), s.stream.status, s.stream.audio, s.stream.recording.active, s.stream.destinations.map((d) => d.active)], renderLive);

  // ---------- encoder picker and transports ----------
  function renderEncoders() {
    const s = S();
    const probe = s.probe;
    const pinned = s.settings.codec;
    const current = s.settings.encoder;
    const rows = [{ name: 'auto', label: 'Automatic', codec: '', ok: true, reason: '', backend: '' }];
    if (probe) rows.push(...probe.encoders);
    renderList(R.encoders, rows, (e) => e.name, (e) => {
      const btn = h('button', { type: 'button', role: 'radio', class: 'st-enc', 'data-encoder': e.name });
      btn.append(h('span', { class: 'st-enc-name' }), h('span', { class: 'st-enc-tags' }), h('span', { class: 'st-enc-why' }));
      btn.addEventListener('click', () => { if (!btn.disabled) liveSet({ encoder: btn._enc.name }); });
      return btn;
    }, (btn, e) => {
      btn._enc = e;
      const wrongCodec = e.codec && pinned !== 'auto' && e.codec !== pinned;
      const usable = e.ok && !wrongCodec && available(s);
      btn.disabled = !usable && e.name !== current;
      btn.setAttribute('aria-checked', String(e.name === current));
      btn.setAttribute('aria-disabled', String(!usable));
      btn.toggleAttribute('data-current', e.name === current);
      btn.toggleAttribute('data-off', !usable);
      btn.querySelector('.st-enc-name').textContent = e.name === 'auto' ? 'Automatic: the best working encoder' : e.label || e.name;
      const tags = [];
      if (e.codec) tags.push(chip(CODEC_NAMES[e.codec] ?? e.codec));
      if (e.backend) tags.push(chip(e.backend === 'software' ? 'CPU' : e.backend.toUpperCase()));
      btn.querySelector('.st-enc-tags').replaceChildren(...tags);
      const why = !e.ok ? e.reason || 'Not usable on this host.' : wrongCodec ? `The codec is set to ${CODEC_NAMES[pinned]}.` : '';
      btn.querySelector('.st-enc-why').textContent = why;
      if (why) btn.title = why; else btn.removeAttribute('title');
    });
    // transports, WHIP included: an unavailable one says why
    const transports = probe?.transports ?? [];
    R.transports.replaceChildren(...transports.map((t) => h('span', { class: 'pill', 'data-state': t.ok ? 'ok' : 'warn', 'data-transport': t.id, title: t.reason || t.label },
      icon(t.ok ? 'check' : 'alert'), `${t.label}${t.ok ? '' : ' (unavailable)'}`)));
    if (!probe) R.transports.replaceChildren(h('span', { class: 'hint' }, 'Checking what this host can encode and send…'));
    R.reasons.replaceChildren(...transports.filter((t) => !t.ok).map((t) => h('li', { 'data-transport': t.id }, h('strong', {}, `${t.label}: `), t.reason || 'unavailable')),
      ...(probe?.problems ?? []).map((p) => h('li', {}, p)));
  }
  sub((s) => [s.stream.probe, s.stream.settings.encoder, s.stream.settings.codec, s.stream.status], renderEncoders);

  // ---------- picture and codec (reconnects once) ----------
  const staged = {};
  const touched = new Set();
  const serverPicture = () => { const { codec, tier, fps } = S().settings; return { codec, tier, fps }; };

  R.tier.replaceChildren(...TIERS.map((t) => h('option', { value: t }, t === 'source' ? 'Same as the camera' : t)));
  R.fps.replaceChildren(...FPS.map((f) => h('option', { value: String(f) }, f === 0 ? 'Same as the camera' : `${f} fps`)));

  function stage(field, value) {
    staged[field] = value;
    if (staged[field] === serverPicture()[field]) touched.delete(field); else touched.add(field);
    renderPicture();
  }
  R.tier.addEventListener('change', () => stage('tier', R.tier.value));
  R.fps.addEventListener('change', () => stage('fps', Number(R.fps.value)));
  R.codec.addEventListener('click', (ev) => {
    const b = ev.target.closest('button[data-codec]');
    if (b && !b.disabled) stage('codec', b.dataset.codec);
  });
  R.revertBtn.addEventListener('click', () => { touched.clear(); renderPicture(); });
  R.applyBtn.addEventListener('click', async () => {
    const changes = Object.fromEntries([...touched].map((k) => [k, staged[k]]));
    if (!Object.keys(changes).length) return;
    busy.add('picture');
    renderPicture();
    const res = await act('stream.set', changes, { quiet: true });
    busy.delete('picture');
    if (disposed) return;
    if (res) {
      touched.clear();
      if (res.message) say('live', res.message);
      toast(res.message ?? 'Applied.', { level: 'success', timeout: 4000, id: 'stream:picture' });
    }
    renderPicture();
  });

  function renderPicture() {
    const s = S();
    const server = serverPicture();
    for (const field of ['codec', 'tier', 'fps']) if (!touched.has(field)) staged[field] = server[field];
    const codecOk = (c) => c === 'auto' || (s.probe ? s.probe.codecs.includes(c) : false);
    const codecWhy = (c) => (s.probe?.encoders.find((e) => e.codec === c && e.reason)?.reason) || 'No working encoder for this codec on this host.';
    renderList(R.codec, CODECS, (c) => c, (c) => {
      const b = h('button', { type: 'button', role: 'radio', 'data-codec': c }, c === 'auto' ? 'Auto' : CODEC_NAMES[c]);
      return b;
    }, (b, c) => {
      const ok = codecOk(c) && available(s);
      b.disabled = !ok;
      b.setAttribute('aria-checked', String(staged.codec === c));
      b.setAttribute('aria-pressed', String(staged.codec === c));
      if (!ok && available(s)) b.title = s.probe ? codecWhy(c) : 'Checking this host…'; else b.removeAttribute('title');
    });
    R.tier.value = staged.tier;
    R.fps.value = String(staged.fps);
    const dirty = touched.size > 0;
    const streaming = live(s).length > 0 || s.recording.active;
    R.applyBtn.toggleAttribute('disabled', !dirty || swapping(s) || busy.has('picture') || !available(s));
    R.applyBtn.toggleAttribute('busy', busy.has('picture'));
    R.applyBtn.textContent = !dirty ? 'Apply' : streaming ? 'Apply and reconnect once' : 'Save';
    R.revertBtn.hidden = !dirty;
    R.tier.disabled = R.fps.disabled = !available(s);
    const targets = live(s).length;
    const geometry = touched.has('tier') || touched.has('fps');
    const codecChange = touched.has('codec');
    let text;
    if (!streaming) text = 'Resolution, frame rate and codec apply when the stream starts.';
    else if (dirty) {
      const parts = [];
      if (geometry) parts.push(`every destination reconnects once, starting on a keyframe${s.recording.active ? ', and the recording continues in a new file' : ''}`);
      if (codecChange) parts.push('the destinations that move to another codec reconnect');
      text = `Applying this: ${parts.join('; ')}.`;
    } else {
      text = `Changing resolution, frame rate or codec reconnects ${targets ? plural(targets, 'destination') : 'the destinations'} once, starting on a keyframe. Everything above this card changes live without that.`;
    }
    R.reconnectText.textContent = text;
    R.reconnectNote.dataset.state = streaming && dirty ? 'warn' : '';
    R.reconnectIc.replaceChildren(icon(streaming && dirty ? 'alert' : 'info'));
    R.outputNote.textContent = s.output.width ? `Now ${s.output.width}×${s.output.height} at ${Number(s.output.fps).toFixed(0)} fps` : '';
  }
  sub((s) => [s.stream.settings, s.stream.probe, s.stream.output, s.stream.status, s.stream.recording.active, s.stream.destinations.map((d) => d.active), s.stream.encoders.map((e) => e.swapping)], renderPicture);

  // ---------- events ----------
  function renderEvents() {
    const events = [...S().events].reverse();
    R.noEvents.hidden = events.length > 0;
    R.events.replaceChildren(...events.map((e) => h('li', { 'data-level': e.level },
      h('span', { class: 'st-ev-ic' }, icon(e.level === 'error' ? 'alert' : e.level === 'warn' ? 'alert' : 'info')),
      h('time', {}, timeOfDay(e.at)), h('span', { class: 'st-ev-text' }, e.text))));
  }
  sub((s) => s.stream.events, renderEvents);

  renderHero();
  renderMeters();
  renderDestinations();
  renderLive();
  renderEncoders();
  renderPicture();
  renderEvents();

  // ---------- palette ----------
  off.push(registerCommands(() => {
    const s = S();
    if (!available(s)) return [];
    const streaming = live(s).length > 0;
    return [
      streaming
        ? { id: 'stream.stop', title: 'Stop the stream', group: 'Stream', icon: 'stop', danger: true, run: () => act('stream.stop', {}) }
        : { id: 'stream.start', title: 'Go live', group: 'Stream', icon: 'broadcast', keywords: 'stream start', run: () => act('stream.start', {}) },
      s.recording.active
        ? { id: 'record.stop', title: 'Stop recording', group: 'Stream', icon: 'stop', run: () => act('record.stop', {}) }
        : { id: 'record.start', title: 'Start recording', group: 'Stream', icon: 'record', run: () => act('record.start', { format: recFormat }) },
    ];
  }));

  return () => {
    disposed = true;
    clearTimeout(gateTimer);
    R.fKey.value = '';
    off.forEach((fn) => fn());
  };
}
