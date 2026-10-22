// <ss-meter> — a dBFS level meter drawn on a canvas, animated at display rate with real ballistics.
//
//   <ss-meter source="mic" label="Mic" target="-24,-12"></ss-meter>        auto-subscribes to the live meter frames
//   <ss-meter label="Custom"></ss-meter>  + meter.push({peak_db, rms_db, clip})   manual feed
//
// Attributes:
//   source        mic | voice — which field of the `meters` frame to follow (omit for manual `push`)
//   orientation   vertical (default) | horizontal
//   min, max      dBFS range of the scale (default -60 … 0)
//   warn, danger  dB where the colour turns amber / red (defaults -12 and -4)
//   target        "lo,hi" dB: a soft band marking where speech should sit (e.g. "-24,-12"); drawn behind the bar
//   scale         "auto" (default: shown when there is room) | "none"
//   mode          led (segments, default) | smooth
//   label         text under/over the meter (also its accessible name)
//   thickness     bar thickness in rem (default 0.9)
// Properties: push(frame)  frame = {peak_db, rms_db, clip}; null/undefined = silence
// Methods:  resetClip()
// Ballistics: peak = instant attack, 20 dB/s release; level (RMS) = 50 ms attack, 350 ms release;
//   peak-hold tick holds 1.6 s then falls 14 dB/s; the clip lamp latches for 3 s (click it to clear).
// Rendering never touches the page: one shared requestAnimationFrame loop, only while a meter is moving.
// The element has role="meter"; aria-valuenow is refreshed ~4×/s (not per frame).

import { css, define, SSElement } from './base.js';
import { store } from '../core/store.js';
import { clamp } from '../core/util.js';

const FLOOR = -90;
const styles = css`
  :host { display: inline-flex; flex-direction: column; align-items: center; gap: var(--s-2); min-width: 0; min-height: 0; --bar: 0.9rem; }
  :host([orientation="horizontal"]) { flex-direction: row-reverse; align-items: center; width: 100%; }
  .stage { position: relative; flex: 1; min-height: 4rem; display: flex; flex-direction: column; align-items: center; }
  :host([orientation="horizontal"]) .stage { flex-direction: row; min-height: 0; width: 100%; height: 2.4rem; }
  canvas { display: block; width: 100%; height: 100%; }
  .stage { width: 100%; }
  .clip { width: 100%; max-width: 3.2rem; height: 0.55rem; border-radius: 0.2rem; background: var(--meter-off); border: 1px solid var(--line); flex: none; margin-bottom: 0.3rem; cursor: pointer; transition: background var(--dur-1); display: block; padding: 0; }
  :host([orientation="horizontal"]) .clip { width: 1.1rem; max-width: none; height: 100%; margin: 0 0 0 0.4rem; order: 2; }
  .clip[data-on] { background: var(--red); box-shadow: 0 0 0.9rem var(--red-glow); border-color: var(--red); }
  .cv { flex: 1; width: 100%; min-height: 0; min-width: 0; position: relative; }
  :host([orientation="horizontal"]) .cv { height: 100%; }
  .label { font-size: var(--fs-2xs); font-weight: 700; letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--text-3); white-space: nowrap; }
  .clipword { position: absolute; left: 0; right: 0; top: 0; text-align: center; font-size: var(--fs-2xs); font-weight: 800; color: var(--red); pointer-events: none; opacity: 0; }
`;

/** One shared animation loop for every meter. */
const live = new Set();
let raf = 0;
let lastT = 0;
function loop(now) {
  const dt = Math.min(0.1, (now - lastT) / 1000 || 0.016);
  lastT = now;
  for (const m of live) m.tick(dt, now);
  raf = live.size ? requestAnimationFrame(loop) : 0;
}
function wake(m) {
  live.add(m);
  if (!raf) { lastT = performance.now(); raf = requestAnimationFrame(loop); }
}

export class SSMeter extends SSElement {
  static styles = styles;
  static observedAttributes = ['source', 'orientation', 'min', 'max', 'warn', 'danger', 'target', 'scale', 'mode', 'label', 'thickness'];

  constructor() {
    super();
    this.peak = FLOOR; this.rms = FLOOR; this.hold = FLOOR; this.holdAt = 0;
    this.inPeak = FLOOR; this.inRms = FLOOR;
    this.clipUntil = 0;
    this.size = { w: 0, h: 0, dpr: 1 };
    this._ariaAt = 0;
    this.rootPx = 14;
  }

  build(root) {
    root.innerHTML = `<div class="stage"><button class="clip" type="button" aria-label="Clip indicator, press to clear" title="Clip — click to clear"></button><div class="cv"><canvas></canvas></div></div><span class="label"></span>`;
    this.$ = { canvas: root.querySelector('canvas'), cv: root.querySelector('.cv'), clip: root.querySelector('.clip'), label: root.querySelector('.label') };
    this.ctx = this.$.canvas.getContext('2d');
    this.$.clip.addEventListener('click', () => this.resetClip());
    this.ro = new ResizeObserver(() => this.resize());
    this.setAttribute('role', 'meter');
    this._themeFn = () => { this.readColors(); this.dirty = true; wake(this); };
  }

  connected() {
    this.ro.observe(this.$.cv);
    window.addEventListener('ss:theme', this._themeFn);
    this.readColors();
    this.subscribe();
    this.dirty = true;
    wake(this);
  }

  disconnected() {
    this.ro.disconnect();
    window.removeEventListener('ss:theme', this._themeFn);
    this.unsub?.();
    this.unsub = null;
    live.delete(this);
  }

  attributeChangedCallback(name, o, n) {
    super.attributeChangedCallback(name, o, n);
    if (this._ready && name === 'source') this.subscribe();
    this.dirty = true;
    if (this.isConnected) wake(this);
  }

  subscribe() {
    this.unsub?.();
    this.unsub = null;
    const src = this.getAttribute('source');
    if (!src) return;
    this.unsub = store.onMeters((frame) => this.push(frame ? frame[src] : null));
    this.push(store.meters ? store.meters[src] : null);
  }

  update() {
    this.$.label.textContent = this.getAttribute('label') || '';
    this.$.label.hidden = !this.getAttribute('label');
    this.setAttribute('aria-label', this.getAttribute('label') ? `${this.getAttribute('label')} level` : 'Level');
    this.setAttribute('aria-valuemin', String(this.min));
    this.setAttribute('aria-valuemax', String(this.max));
    this.resize();
  }

  get min() { return this.num('min', -60); }
  get max() { return this.num('max', 0); }
  get horizontal() { return this.getAttribute('orientation') === 'horizontal'; }

  /** Feed one level frame. null/undefined = no signal. */
  push(frame) {
    if (!frame) { this.inPeak = FLOOR; this.inRms = FLOOR; }
    else {
      this.inPeak = Number.isFinite(frame.peak_db) ? frame.peak_db : FLOOR;
      this.inRms = Number.isFinite(frame.rms_db) ? frame.rms_db : FLOOR;
      if (frame.clip || this.inPeak >= -0.5) this.clipUntil = performance.now() + 3000;
    }
    wake(this);
  }

  resetClip() { this.clipUntil = 0; this.dirty = true; wake(this); }

  resize() {
    if (!this.$) return;
    const r = this.$.cv.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(r.width * dpr));
    const h = Math.max(1, Math.round(r.height * dpr));
    if (w !== this.size.w || h !== this.size.h || dpr !== this.size.dpr) {
      this.size = { w, h, dpr };
      this.rootPx = parseFloat(getComputedStyle(document.documentElement).fontSize) || 14;
      this.$.canvas.width = w;
      this.$.canvas.height = h;
      this.dirty = true;
      wake(this);
    }
  }

  readColors() {
    const cs = getComputedStyle(this);
    const g = (n, d) => cs.getPropertyValue(n).trim() || d;
    this.colors = {
      low: g('--meter-low', '#2fd47a'), mid: g('--meter-mid', '#ffc23a'), hi: g('--meter-hi', '#ff4a5c'),
      off: g('--meter-off', 'rgba(255,255,255,.07)'), target: g('--meter-target', 'rgba(94,164,255,.12)'), targetEdge: g('--accent', '#5ea4ff'),
      text: g('--text-3', '#8591a4'), font: g('--font-mono', 'monospace'),
    };
  }

  /** Ballistics, then draw if anything moved. dt in seconds. */
  tick(dt, now) {
    const floor = this.min - 6;
    // peak: instant attack, 20 dB/s release
    const prevPeak = this.peak; const prevRms = this.rms; const prevHold = this.hold;
    this.peak = this.inPeak >= this.peak ? this.inPeak : Math.max(this.inPeak, this.peak - 20 * dt);
    // rms: 50 ms attack, 350 ms release (one-pole)
    const tau = this.inRms > this.rms ? 0.05 : 0.35;
    this.rms += (this.inRms - this.rms) * (1 - Math.exp(-dt / tau));
    if (this.peak >= this.hold) { this.hold = this.peak; this.holdAt = now; }
    else if (now - this.holdAt > 1600) this.hold = Math.max(this.peak, this.hold - 14 * dt);
    const clipOn = now < this.clipUntil;
    const moved = Math.abs(this.peak - prevPeak) > 0.05 || Math.abs(this.rms - prevRms) > 0.05 || Math.abs(this.hold - prevHold) > 0.05;
    if (moved || this.dirty || clipOn !== this._clipOn) {
      this._clipOn = clipOn;
      this.$.clip.toggleAttribute('data-on', clipOn);
      this.draw();
      this.dirty = false;
    }
    if (now - this._ariaAt > 250) {
      this._ariaAt = now;
      const v = Math.max(this.min, this.rms);
      this.setAttribute('aria-valuenow', v.toFixed(0));
      this.setAttribute('aria-valuetext', this.rms <= this.min ? 'silent' : `${v.toFixed(0)} dB${clipOn ? ', clipping' : ''}`);
    }
    // sleep when settled at the floor with nothing coming in
    const resting = this.peak <= floor && this.rms <= floor && this.hold <= floor && this.inPeak <= floor && !clipOn && !this.dirty;
    if (resting) live.delete(this);
  }

  draw() {
    const { w, h, dpr } = this.size;
    const c = this.colors || (this.readColors(), this.colors);
    const ctx = this.ctx;
    ctx.clearRect(0, 0, w, h);
    const min = this.min; const max = this.max; const span = max - min;
    const warn = this.num('warn', -12); const danger = this.num('danger', -4);
    const vert = !this.horizontal;
    const showScale = this.getAttribute('scale') !== 'none';
    const fontPx = Math.round(10 * dpr * (this.rootPx / 14));
    const barPx = Math.min(vert ? w : h, Math.round(this.num('thickness', 0.9) * this.rootPx * dpr));
    const labelRoom = showScale ? Math.round(fontPx * 2.9) : 0;
    const len = vert ? h : w;
    const cross = vert ? w : h;
    // bar occupies [bx, bx+barPx] across, whole length along (inset by half a font for end labels)
    const pad = showScale ? Math.round(fontPx * 0.6) : 0;
    const total = barPx + labelRoom;
    const bx = Math.round((cross - total) / 2) + (vert ? 0 : 0);
    const along0 = pad; const alongLen = len - pad * 2;
    const toPos = (db) => clamp((db - min) / span, 0, 1) * alongLen;
    const rect = (a, b, thick, colour) => {   // along a..b (from low end), across offset bx
      ctx.fillStyle = colour;
      if (vert) ctx.fillRect(bx, along0 + alongLen - b, thick, b - a);
      else ctx.fillRect(along0 + a, bx, b - a, thick);
    };
    const colourAt = (db) => (db >= danger ? c.hi : db >= warn ? c.mid : c.low);

    // target band
    const tgt = (this.getAttribute('target') || '').split(',').map(Number);
    if (tgt.length === 2 && tgt.every(Number.isFinite)) {
      const a = toPos(tgt[0]); const b = toPos(tgt[1]);
      const inset = Math.round(barPx * 0.3);
      const edge = Math.max(2, Math.round(1.6 * dpr));
      ctx.fillStyle = c.target;
      if (vert) {
        ctx.fillRect(bx - inset, along0 + alongLen - b, barPx + inset * 2, b - a);
      } else {
        ctx.fillRect(along0 + a, bx - inset, b - a, barPx + inset * 2);
      }
      ctx.fillStyle = c.targetEdge;
      if (vert) {
        ctx.fillRect(bx - inset - edge, along0 + alongLen - b, edge, b - a);
        ctx.fillRect(bx + barPx + inset, along0 + alongLen - b, edge, b - a);
      } else {
        ctx.fillRect(along0 + a, bx - inset - edge, b - a, edge);
        ctx.fillRect(along0 + a, bx + barPx + inset, b - a, edge);
      }
    }

    const smooth = this.getAttribute('mode') === 'smooth';
    if (smooth) {
      rect(0, alongLen, barPx, c.off);
      // body = rms (solid), halo = peak (dim)
      const stops = [[min, c.low], [warn, c.mid], [danger, c.hi]];
      const grad = vert ? ctx.createLinearGradient(0, along0 + alongLen, 0, along0) : ctx.createLinearGradient(along0, 0, along0 + alongLen, 0);
      for (const [db, col] of stops) grad.addColorStop(clamp((db - min) / span, 0, 1), col);
      grad.addColorStop(1, c.hi);
      ctx.save();
      ctx.globalAlpha = 0.38; ctx.fillStyle = grad;
      if (vert) ctx.fillRect(bx, along0 + alongLen - toPos(this.peak), barPx, toPos(this.peak)); else ctx.fillRect(along0, bx, toPos(this.peak), barPx);
      ctx.globalAlpha = 1;
      if (vert) ctx.fillRect(bx, along0 + alongLen - toPos(this.rms), barPx, toPos(this.rms)); else ctx.fillRect(along0, bx, toPos(this.rms), barPx);
      ctx.restore();
    } else {
      const segLen = Math.max(2, Math.round(5 * dpr * (alongLen / (300 * dpr) > 1.2 ? 1.2 : 1)));
      const gap = Math.max(1, Math.round(dpr * 1.2));
      const n = Math.max(8, Math.floor((alongLen + gap) / (segLen + gap)));
      const sl = (alongLen + gap) / n - gap;
      const rad = Math.min(2 * dpr, sl / 2);
      for (let i = 0; i < n; i++) {
        const dbMid = min + ((i + 0.5) / n) * span;
        const a = i * (sl + gap);
        const col = colourAt(dbMid);
        let alpha = 0;
        if (dbMid <= this.rms) alpha = 1; else if (dbMid <= this.peak) alpha = 0.42;
        ctx.fillStyle = c.off;
        this.seg(ctx, vert, bx, along0, alongLen, a, sl, barPx, rad);
        if (alpha) {
          ctx.globalAlpha = alpha; ctx.fillStyle = col;
          this.seg(ctx, vert, bx, along0, alongLen, a, sl, barPx, rad);
          ctx.globalAlpha = 1;
        }
      }
    }
    // peak-hold tick
    if (this.hold > min) {
      const p = toPos(this.hold);
      const t = Math.max(2, Math.round(2.4 * dpr));
      ctx.fillStyle = colourAt(this.hold);
      if (vert) ctx.fillRect(bx - 1, along0 + alongLen - p - t / 2, barPx + 2, t); else ctx.fillRect(along0 + p - t / 2, bx - 1, t, barPx + 2);
    }
    // scale
    if (showScale) {
      ctx.fillStyle = c.text; ctx.font = `600 ${fontPx}px ${c.font}`; ctx.textBaseline = 'middle';
      const ticks = [];
      for (let db = Math.ceil(max / 6) * 6; db >= min; db -= (span > 40 ? 12 : 6)) ticks.push(db);
      if (!ticks.includes(max)) ticks.unshift(max);
      for (const db of ticks) {
        const p = toPos(db);
        const label = db === 0 ? '0' : String(db).replace('-', '−');
        if (vert) {
          ctx.textAlign = 'left';
          ctx.fillText(label, bx + barPx + Math.round(fontPx * 0.5), along0 + alongLen - p);
        } else {
          ctx.textAlign = 'center'; ctx.textBaseline = 'top';
          ctx.fillText(label, along0 + p, bx + barPx + Math.round(fontPx * 0.4));
        }
      }
    }
  }

  seg(ctx, vert, bx, along0, alongLen, a, sl, barPx, rad) {
    const x = vert ? bx : along0 + a;
    const y = vert ? along0 + alongLen - a - sl : bx;
    const w = vert ? barPx : sl;
    const h = vert ? sl : barPx;
    ctx.beginPath();
    ctx.roundRect(x, y, w, h, rad);
    ctx.fill();
  }
}

define('ss-meter', SSMeter);
