// <ss-preview> — a live picture from the server's MJPEG endpoint, with overlays and graceful failure.
//
//   <ss-preview source="program" fps="10" grid tally="program">
//     <ss-status-chip slot="badges" state="live" label="ON AIR"></ss-status-chip>
//     <span slot="corner">00:12:03</span>
//     <div slot="footer">…controls over the picture…</div>
//   </ss-preview>
//
// Attributes:
//   source   program (default) | camera | <an OBS source name>
//   fps      1…15 (default 10)
//   width    requested picture width 160…1280; default follows the element size (320/480/640/960/1280 buckets)
//   grid     rule-of-thirds overlay (toggle with the G key via the `grid` attribute)
//   tally    program → red frame while this picture is what the audience sees
//   reason   do not connect; show this text instead of a picture ("OBS is not running")
//   paused   stop the stream (the last frame stays)
//   label    accessible name (default "Live picture")
// Slots: badges (top-left), corner (top-right), footer (bottom, over a gradient), default (centred overlay, e.g. a privacy cover),
//        empty (extra content in the “no picture” state, e.g. a Start studio button).
// Properties: image (the inner <img>, for reading frames into a canvas; same-origin so never tainted), live (a picture is showing)
// Methods: reload()   reconnect the stream (app.js calls it when the event stream comes back)
// Behaviour: pauses when the tab is hidden or the element scrolls out of view; retries after errors every 3 s;
//   a slow first frame shows a calm “Connecting…” state; never shows a broken-image icon.
// Note: every open preview holds one HTTP connection — keep it to one or two per page (browser limit: 6 per host).

import { css, define, SSElement } from './base.js';
import { iconHTML } from './icons.js';
import { apiUrl } from '../core/api.js';

const BUCKETS = [320, 480, 640, 960, 1280];

const styles = css`
  :host { display: block; position: relative; overflow: hidden; background: #000; border-radius: var(--r-3); isolation: isolate; box-shadow: 0 0 0 1px var(--line-strong), var(--shadow-2); aspect-ratio: 16 / 9; transition: box-shadow var(--dur-3); }
  :host([tally="program"]) { box-shadow: 0 0 0 2px var(--red-solid), 0 0 2.4rem -0.2rem var(--red-glow); }
  img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; display: block; background: #000; }
  img[hidden] { display: none; }
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
  static observedAttributes = ['source', 'fps', 'width', 'grid', 'reason', 'paused', 'label', 'tally'];

  build(root) {
    root.innerHTML = `<img alt="" hidden decoding="async" draggable="false">
      <div class="grid"></div>
      <div class="empty" data-show data-state="connecting"><span class="ic"></span><strong></strong><span class="why"></span><slot name="empty"></slot></div>
      <slot class="over"></slot>
      <div class="badges"><slot name="badges"></slot></div>
      <div class="corner"><slot name="corner"></slot></div>
      <div class="footer"><slot name="footer"></slot></div>`;
    this.$ = { img: root.querySelector('img'), empty: root.querySelector('.empty'), ic: root.querySelector('.ic'), strong: root.querySelector('.empty strong'), why: root.querySelector('.why') };
    this.$.ic.style.display = 'contents';
    this.bucket = 640;
    this.visible = true;
    this.$.img.addEventListener('load', () => { this.setState('live'); });
    this.$.img.addEventListener('error', () => {
      if (!this.$.img.getAttribute('src')) return;
      this.setState('none', 'The picture stopped.');
      this.scheduleRetry();
    });
    this.ro = new ResizeObserver(() => this.measure());
    this.io = new IntersectionObserver((entries) => { this.visible = entries.some((e) => e.isIntersecting); this.sync(); });
    this._vis = () => this.sync();
    this._reload = () => this.reload();
    this.setAttribute('role', 'img');
  }

  connected() {
    this.ro.observe(this);
    this.io.observe(this);
    document.addEventListener('visibilitychange', this._vis);
    window.addEventListener('ss:reconnected', this._reload);
    this.measure();
  }

  disconnected() {
    this.ro.disconnect();
    this.io.disconnect();
    document.removeEventListener('visibilitychange', this._vis);
    window.removeEventListener('ss:reconnected', this._reload);
    clearTimeout(this.retry);
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
  get live() { return !this.$.img.hidden && this.$.img.naturalWidth > 0; }

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
    const u = this.url();
    if (force || this.current !== u) {
      this.current = u;
      if (this.$.img.hidden) this.setState('connecting');
      this.$.img.src = u;
    }
  }

  stop() {
    if (this.current) { this.current = null; this.$.img.removeAttribute('src'); }
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
    this.$.img.hidden = !live;
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
