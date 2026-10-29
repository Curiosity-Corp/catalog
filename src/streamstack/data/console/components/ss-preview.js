// <ss-preview> — a live picture from the server (WebRTC/WHEP when asked, MJPEG otherwise), with overlays and graceful failure.
//
//   <ss-preview source="program" fps="10" grid tally="program">
//     <ss-status-chip slot="badges" state="live" label="ON AIR"></ss-status-chip>
//     <span slot="corner">00:12:03</span>
//     <div slot="footer">…controls over the picture…</div>
//   </ss-preview>
//
// Attributes:
//   source   program (default) | camera | <a camera name>
//   transport mjpeg (default) | auto | whep. auto tries a WebRTC (WHEP) picture first, well under a second behind, and falls
//            back to MJPEG when the Console has no WebRTC, the session fails, no picture arrives in 6 s or a picture that
//            was playing freezes for 5 s (a frozen program picture must never sit on screen); it tries WebRTC again after
//            a minute. whep never falls back. Only the program picture is offered over WebRTC. The host's
//            `data-transport` says which one is showing (whep | mjpeg).
//   fps      1…15 (default 10)
//   width    requested picture width 160…1280; default follows the element size (320/480/640/960/1280 buckets)
//   grid     rule-of-thirds overlay (toggle with the G key via the `grid` attribute)
//   tally    program → red frame while this picture is what the audience sees
//   reason   do not connect; show this text instead of a picture ("No camera")
//   paused   stop the stream (the last frame stays)
//   label    accessible name (default "Live picture")
// Slots: badges (top-left), corner (top-right), footer (bottom, over a gradient), default (centred overlay, e.g. a privacy cover),
//        empty (extra content in the “no picture” state, e.g. a Start studio button).
// Properties: image (the inner <img>, for reading frames into a canvas; same-origin so never tainted; only updated while the
//   MJPEG transport shows, so pages that read pixels leave transport at its default), live (a picture is showing), video (the <video>)
// Methods: stats() resolves WebRTC figures (codec, frame rate, jitter buffer ms, round trip) while WHEP shows, else null
// Methods: reload()   reconnect the stream (app.js calls it when the event stream comes back)
// Behaviour: pauses when the tab is hidden or the element scrolls out of view; retries after errors every 3 s;
//   a slow first frame shows a calm “Connecting…” state; never shows a broken-image icon.
// Note: every open preview holds one HTTP connection — keep it to one or two per page (browser limit: 6 per host).

import { css, define, SSElement } from './base.js';
import { iconHTML } from './icons.js';
import { apiUrl } from '../core/api.js';
import { whepConnect, whepSupported } from '../core/whep.js';

const BUCKETS = [320, 480, 640, 960, 1280];
const FIRST_FRAME_MS = 6000; // a WebRTC session that shows nothing by then is given up
const FREEZE_MS = 5000; // a WebRTC picture whose decoded frame count stands still this long is frozen: MJPEG takes over
const FREEZE_POLL_MS = 1000;
const WHEP_RETRY_MS = 60000; // after a failure, MJPEG carries the picture this long before WebRTC is tried again

const styles = css`
  :host { display: block; position: relative; overflow: hidden; background: #000; border-radius: var(--r-3); isolation: isolate; box-shadow: 0 0 0 1px var(--line-strong), var(--shadow-2); aspect-ratio: 16 / 9; transition: box-shadow var(--dur-3); }
  :host([tally="program"]) { box-shadow: 0 0 0 2px var(--red-solid), 0 0 2.4rem -0.2rem var(--red-glow); }
  img, video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; display: block; background: #000; }
  img[hidden], video[hidden] { display: none; }
  .grid { position: absolute; inset: 0; pointer-events: none; display: none; background:
    linear-gradient(to right, transparent calc(33.333% - 0.5px), rgba(255,255,255,0.55) calc(33.333% - 0.5px) calc(33.333% + 0.5px), transparent calc(33.333% + 0.5px) calc(66.666% - 0.5px), rgba(255,255,255,0.55) calc(66.666% - 0.5px) calc(66.666% + 0.5px), transparent calc(66.666% + 0.5px)),
    linear-gradient(to bottom, transparent calc(33.333% - 0.5px), rgba(255,255,255,0.55) calc(33.333% - 0.5px) calc(33.333% + 0.5px), transparent calc(33.333% + 0.5px) calc(66.666% - 0.5px), rgba(255,255,255,0.55) calc(66.666% - 0.5px) calc(66.666% + 0.5px), transparent calc(66.666% + 0.5px)); filter: drop-shadow(0 0 1px rgba(0,0,0,0.8)); }
  :host([grid]) .grid { display: block; }
  .empty { position: absolute; inset: 0; display: none; flex-direction: column; align-items: center; justify-content: center; gap: var(--s-3); text-align: center; padding: var(--s-4); color: #aab4c4; background: radial-gradient(60% 80% at 50% 40%, #1a2030, #090b10); }
  .empty[data-show] { display: flex; }
  .empty .icon { width: 2.4rem; height: 2.4rem; opacity: 0.8; }
  .empty strong { color: #eaeef5; font-size: var(--fs-md); }
  .empty span { font-size: var(--fs-sm); max-width: 28ch; text-wrap: balance; }
  .empty[data-state="connecting"] .icon { animation: pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: 0.25; } }
  .badges { position: absolute; top: var(--s-3); left: var(--s-3); display: flex; gap: var(--s-2); flex-wrap: wrap; align-items: center; z-index: 2; max-width: 70%; }
  .corner { position: absolute; top: var(--s-3); right: var(--s-3); z-index: 2; display: flex; gap: var(--s-2); align-items: center; font-family: var(--font-mono); font-weight: 650; color: #fff; text-shadow: 0 1px 4px rgba(0,0,0,0.9); }
  .footer { position: absolute; left: 0; right: 0; bottom: 0; z-index: 2; padding: var(--s-5) var(--s-3) var(--s-3); background: linear-gradient(to top, rgba(0,0,0,0.62), transparent); display: flex; align-items: flex-end; gap: var(--s-2); }
  .footer:not(:has(::slotted(*))) { display: none; }
  .over { position: absolute; inset: 0; z-index: 1; display: contents; }
  ::slotted([slot="badges"]) { backdrop-filter: blur(6px); }
`;

export class SSPreview extends SSElement {
  static styles = styles;
  static observedAttributes = ['source', 'fps', 'width', 'grid', 'reason', 'paused', 'label', 'tally', 'transport'];

  build(root) {
    root.innerHTML = `<img alt="" hidden decoding="async" draggable="false"><video muted playsinline disablepictureinpicture hidden></video>
      <div class="grid"></div>
      <div class="empty" data-show data-state="connecting"><span class="ic"></span><strong></strong><span class="why"></span><slot name="empty"></slot></div>
      <slot class="over"></slot>
      <div class="badges"><slot name="badges"></slot></div>
      <div class="corner"><slot name="corner"></slot></div>
      <div class="footer"><slot name="footer"></slot></div>`;
    this.$ = { img: root.querySelector('img'), video: root.querySelector('video'), empty: root.querySelector('.empty'), ic: root.querySelector('.ic'), strong: root.querySelector('.empty strong'), why: root.querySelector('.why') };
    this.$.ic.style.display = 'contents';
    this.bucket = 640;
    this.visible = true;
    this.mode = 'mjpeg';
    this.whepToken = 0;
    this.whepUntil = 0;
    this.$.img.addEventListener('load', () => { if (this.mode === 'mjpeg') this.setState('live'); });
    this.$.video.addEventListener('playing', () => {
      if (this.mode !== 'whep') return;
      clearTimeout(this.whepTimer);
      this.setState('live');
      this.watchFrames(this.whepToken);
    });
    this.$.img.addEventListener('error', () => {
      if (!this.$.img.getAttribute('src')) return;
      this.setState('none', 'The picture stopped.');
      this.scheduleRetry();
    });
    this.ro = new ResizeObserver(() => this.measure());
    this.io = new IntersectionObserver((entries) => { this.visible = entries.some((e) => e.isIntersecting); this.sync(); });
    this._vis = () => this.sync();
    this._hide = () => this.closeWhep(); // a closing tab ends its WebRTC session instead of leaving it to time out
    this._reload = () => this.reload();
    this.setAttribute('role', 'img');
  }

  connected() {
    this.ro.observe(this);
    this.io.observe(this);
    document.addEventListener('visibilitychange', this._vis);
    window.addEventListener('pagehide', this._hide);
    window.addEventListener('ss:reconnected', this._reload);
    this.measure();
  }

  disconnected() {
    this.ro.disconnect();
    this.io.disconnect();
    document.removeEventListener('visibilitychange', this._vis);
    window.removeEventListener('pagehide', this._hide);
    window.removeEventListener('ss:reconnected', this._reload);
    clearTimeout(this.retry);
    clearTimeout(this.upgrade);
    this.stop();
  }

  update() {
    this.setAttribute('aria-label', this.getAttribute('label') || 'Live picture');
    this.sync();
  }

  measure() {
    const w = this.clientWidth * (window.devicePixelRatio || 1);
    const want = this.num('width', 0);
    let b = want ? clamp(want) : BUCKETS.find((x) => x >= w * 0.9) || BUCKETS[BUCKETS.length - 1];
    if (b !== this.bucket) { this.bucket = b; this.sync(true); }
  }

  get image() { return this.$.img; }
  get video() { return this.$.video; }
  get live() {
    if (this.mode === 'whep') return !this.$.video.hidden && this.$.video.videoWidth > 0;
    return !this.$.img.hidden && this.$.img.naturalWidth > 0;
  }

  wantsWhep() {
    const t = this.getAttribute('transport');
    return (t === 'auto' || t === 'whep') && whepSupported() && (this.getAttribute('source') || 'program') === 'program'
      && (t === 'whep' || Date.now() >= this.whepUntil);
  }

  get active() {
    return this._ready && !this.getAttribute('reason') && !this.bool('paused') && !document.hidden && this.visible && this.isConnected;
  }

  url() {
    const fps = Math.min(15, Math.max(1, this.num('fps', 10)));
    return apiUrl('api/preview.mjpg', { source: this.getAttribute('source') || 'program', fps, width: this.bucket });
  }

  sync(force = false) {
    if (!this._ready) return;
    const reason = this.getAttribute('reason');
    if (reason) { this.stop(); this.setState('none', reason, 'No picture'); return; }
    if (!this.active) { if (document.hidden || !this.visible || this.bool('paused')) this.stop(); return; }
    const whep = this.wantsWhep();
    // A WebRTC picture is not restarted when the element is resized: it keeps the size it started with.
    const key = whep ? `whep:${this.getAttribute('source') || 'program'}:${this.num('fps', 10)}` : this.url();
    if (force || this.current !== key) {
      this.stop();
      this.current = key;
      if (this.$.img.hidden && this.$.video.hidden) this.setState('connecting');
      if (whep) this.startWhep();
      else { this.mode = 'mjpeg'; this.$.img.src = key; }
    }
  }

  stop() {
    if (this.current) { this.current = null; this.$.img.removeAttribute('src'); }
    this.closeWhep();
  }

  closeWhep() {
    this.whepToken += 1; // answers and states of an older session are ignored from here on
    clearTimeout(this.whepTimer);
    clearInterval(this.whepWatch);
    const session = this.whep;
    this.whep = null;
    if (session) session.close();
    if (this.$.video.srcObject) this.$.video.srcObject = null;
    this.$.video.hidden = true;
  }

  async startWhep() {
    const token = this.whepToken + 1;
    this.whepToken = token;
    this.mode = 'whep';
    this.whepTimer = setTimeout(() => this.whepFailed(token, 'No picture arrived over WebRTC in time.'), FIRST_FRAME_MS);
    let session;
    try {
      session = await whepConnect({
        video: this.$.video,
        source: this.getAttribute('source') || 'program',
        width: this.bucket,
        fps: Math.min(15, Math.max(1, this.num('fps', 10))),
        onState: (state) => { if (state === 'failed') this.whepFailed(token, 'The WebRTC picture stopped.'); },
      });
    } catch (err) {
      this.whepFailed(token, err.message);
      return;
    }
    if (token !== this.whepToken) { session.close(); return; }
    this.whep = session;
    this.$.video.play().catch(() => { /* muted video may start on its own; the playing event decides */ });
  }

  // The server sends a frame every tick even for a still picture, so a decoded frame count that stops rising means the
  // picture froze (lost packets that never recovered, a stalled decoder) while the connection still reads "connected".
  // `playing` fires once, so without this a frozen preview would show a stale program picture indefinitely.
  watchFrames(token) {
    clearInterval(this.whepWatch);
    const video = this.$.video;
    if (typeof video.getVideoPlaybackQuality !== 'function') return;
    let last = video.getVideoPlaybackQuality().totalVideoFrames;
    let still = 0;
    this.whepWatch = setInterval(() => {
      if (token !== this.whepToken || this.mode !== 'whep') { clearInterval(this.whepWatch); return; }
      if (document.hidden) { still = 0; return; }
      const now = video.getVideoPlaybackQuality().totalVideoFrames;
      if (now !== last) { last = now; still = 0; return; }
      still += 1;
      if (still * FREEZE_POLL_MS >= FREEZE_MS) this.whepFailed(token, 'The WebRTC picture froze.');
    }, FREEZE_POLL_MS);
  }

  whepFailed(token, why) {
    if (token !== this.whepToken || !this.isConnected) return;
    this.whepWhy = why;
    const strict = this.getAttribute('transport') === 'whep';
    this.whepUntil = Date.now() + WHEP_RETRY_MS;
    this.stop();
    if (strict) { this.mode = 'mjpeg'; this.setState('none', why); return; }
    this.mode = 'mjpeg';
    this.sync(true);
    clearTimeout(this.upgrade);
    this.upgrade = setTimeout(() => { if (this.active && this.mode === 'mjpeg') this.sync(true); }, WHEP_RETRY_MS + 100);
  }

  async stats() {
    if (this.mode !== 'whep' || !this.whep) return null;
    const out = { transport: 'whep', codec: '', fps: 0, jitterBufferMs: 0, roundTripMs: 0, width: this.$.video.videoWidth };
    const report = await this.whep.pc.getStats();
    const codecs = new Map();
    report.forEach((r) => { if (r.type === 'codec') codecs.set(r.id, r.mimeType); });
    report.forEach((r) => {
      if (r.type === 'inbound-rtp' && r.kind === 'video') {
        out.codec = codecs.get(r.codecId) || '';
        out.fps = r.framesPerSecond || 0;
        if (r.jitterBufferEmittedCount) out.jitterBufferMs = Math.round((r.jitterBufferDelay / r.jitterBufferEmittedCount) * 1000);
      }
      if (r.type === 'candidate-pair' && r.state === 'succeeded' && r.currentRoundTripTime !== undefined) {
        out.roundTripMs = Math.round(r.currentRoundTripTime * 1000);
      }
    });
    return out;
  }

  reload() {
    this.stop();
    clearTimeout(this.retry);
    this.sync(true);
  }

  scheduleRetry() {
    clearTimeout(this.retry);
    this.retry = setTimeout(() => { if (this.active) { this.stop(); this.sync(true); } }, 3000);
  }

  setState(state, why = '', title = 'No picture') {
    const e = this.$.empty;
    const live = state === 'live';
    const whep = this.mode === 'whep';
    this.$.img.hidden = !(live && !whep);
    this.$.video.hidden = !(live && whep);
    this.dataset.transport = live ? this.mode : '';
    if (live) { e.removeAttribute('data-show'); return; }
    e.setAttribute('data-show', '');
    e.dataset.state = state;
    this.$.ic.innerHTML = iconHTML(state === 'connecting' ? 'camera' : 'eye-off');
    this.$.strong.textContent = state === 'connecting' ? 'Connecting…' : title;
    this.$.why.textContent = state === 'connecting' ? '' : why;
  }
}

function clamp(w) { return Math.min(1280, Math.max(160, Math.round(w))); }

define('ss-preview', SSPreview);
