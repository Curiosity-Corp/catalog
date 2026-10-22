// <ss-fader> — a channel-strip fader. Big cap, scale, live readout.
//
//   <ss-fader label="Mic gain" unit="dB" min="-30" max="12" step="0.5" default="0" value="-4"
//             signed decimals="1" ticks="auto" orientation="vertical"></ss-fader>
//
// Attributes: everything in value-control.js plus
//   orientation   vertical (default) | horizontal
//   ticks         "auto" (5 evenly spaced) | "none" | comma list of values, e.g. "-30,-18,-6,0,12"
//   tick-labels   "all" (default) | "ends" | "none"
//   accent        "" (blue) | "red" | "green" | "amber"  — colour of the filled part
//   ground        value drawn as a notch on the groove (defaults to `default` when present)
// Events / properties / keyboard: see value-control.js (input ≤15/s, change on release).
// Pointer: press anywhere on the track to jump, drag to move; hold Shift while dragging for fine control
//   (movement ÷ 8, no jump). Double-click resets to `default`. Mouse wheel works while the fader is focused.
// CSS hooks: --fader-w (track thickness incl. cap, default 3rem), --fader-cap-len (cap length, default 1.9rem),
//   --fader-groove (background of the groove, e.g. a warm<->cool gradient), --fader-groove-h (groove thickness, default 0.5rem),
//   --fader-fill-opacity (0 hides the filled part, for gradient tracks).

import { css, define } from './base.js';
import { SSValueElement } from './value-control.js';
import { clamp } from '../core/util.js';

const styles = css`
  :host { display: flex; flex-direction: column; gap: var(--s-2); min-width: 0; min-height: 0; user-select: none; touch-action: none; --cap: var(--fader-cap-len, 1.9rem); }
  :host([disabled]) { opacity: 0.45; pointer-events: none; }
  :host(:focus-visible) { outline: none; }
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--s-2); }
  .label { font-size: var(--fs-2xs); font-weight: 650; letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--text-3); white-space: nowrap; }
  .readout { font-family: var(--font-mono); font-weight: 600; font-size: var(--fs-md); color: var(--text); white-space: nowrap; }
  .readout small { font-size: var(--fs-xs); color: var(--text-3); margin-left: 0.2em; font-weight: 500; }
  :host([readout="none"]) .readout { display: none; }
  .body { position: relative; flex: 1; min-height: 5rem; display: grid; grid-template-columns: auto 1fr; }
  .scale, .track { position: relative; padding-block: calc(var(--cap) / 2); }
  .rail { position: relative; width: 100%; height: 100%; }
  .scale { min-width: 1.9rem; }
  .tick { position: absolute; bottom: calc(var(--p) * 100%); right: 0.35rem; transform: translateY(50%); font-size: var(--fs-2xs); color: var(--text-3); font-family: var(--font-mono); line-height: 1; display: flex; align-items: center; gap: 0.3rem; }
  .tick::after { content: ""; width: 0.35rem; height: 1px; background: var(--line-strong); }
  .tick[data-zero] { color: var(--text-2); }
  .track { width: var(--fader-w, 3rem); justify-self: center; cursor: grab; }
  :host([dragging]) .track { cursor: grabbing; }
  .groove { position: absolute; left: 50%; top: 0; bottom: 0; width: var(--fader-groove-h, 0.5rem); transform: translateX(-50%); border-radius: var(--r-pill); background: var(--fader-groove, var(--surface-inset)); box-shadow: inset 0 1px 3px rgba(0,0,0,0.55), 0 0 0 1px var(--line); }
  .fill { position: absolute; left: 0; right: 0; bottom: calc(var(--lo) * 100%); height: calc(var(--span) * 100%); border-radius: var(--r-pill); opacity: var(--fader-fill-opacity, 1); background: linear-gradient(to top, color-mix(in srgb, var(--fill-c) 55%, transparent), var(--fill-c)); box-shadow: 0 0 0.8rem -0.1rem var(--fill-c); }
  .ground { position: absolute; left: -0.3rem; right: -0.3rem; bottom: calc(var(--g) * 100%); height: 2px; background: var(--line-strong); border-radius: 1px; }
  .thumb { position: absolute; left: 50%; bottom: calc(var(--pos) * 100%); width: 100%; height: var(--cap); transform: translate(-50%, 50%); border-radius: var(--r-2); background: linear-gradient(180deg, var(--surface-3), var(--surface-2)); box-shadow: var(--shadow-2), 0 0 0 1px var(--line-strong); display: grid; place-items: center; transition: box-shadow var(--dur-1), background var(--dur-1); will-change: bottom; }
  .thumb::before { content: ""; width: 62%; height: 3px; border-radius: 2px; background: var(--fill-c); box-shadow: 0 0 0.6rem var(--fill-c); }
  .thumb::after { content: ""; position: absolute; inset: 0.35rem 0.5rem; border-radius: 0.35rem; background: repeating-linear-gradient(0deg, transparent 0 0.28rem, var(--line) 0.28rem calc(0.28rem + 1px)); opacity: 0.8; pointer-events: none; -webkit-mask: linear-gradient(#000 0 calc(50% - 0.4rem), transparent 0 calc(50% + 0.4rem), #000 0); mask: linear-gradient(#000 0 calc(50% - 0.4rem), transparent 0 calc(50% + 0.4rem), #000 0); }
  :host(:hover) .thumb { background: linear-gradient(180deg, var(--surface-3), var(--surface-3)); }
  :host([dragging]) .thumb { box-shadow: var(--shadow-2), 0 0 0 1px var(--fill-c), 0 0 1.4rem -0.2rem var(--fill-c); }
  :host(:focus-visible) .thumb { outline: 2px solid var(--focus); outline-offset: 3px; }

  /* horizontal */
  :host([orientation="horizontal"]) .body { grid-template-columns: 1fr; grid-template-rows: auto auto; min-height: 0; }
  :host([orientation="horizontal"]) .scale { order: 2; padding-block: 0; padding-inline: calc(var(--cap) / 2); min-height: 1.1rem; min-width: 0; }
  :host([orientation="horizontal"]) .track { order: 1; justify-self: stretch; width: auto; height: var(--fader-w, 2.4rem); padding-block: 0; padding-inline: calc(var(--cap) / 2); }
  :host([orientation="horizontal"]) .groove { top: 50%; left: 0; right: 0; bottom: auto; width: auto; height: var(--fader-groove-h, 0.5rem); transform: translateY(-50%); }
  :host([orientation="horizontal"]) .fill { bottom: 0; top: 0; left: calc(var(--lo) * 100%); right: auto; height: auto; width: calc(var(--span) * 100%); background: linear-gradient(to right, color-mix(in srgb, var(--fill-c) 55%, transparent), var(--fill-c)); }
  :host([orientation="horizontal"]) .ground { top: -0.3rem; bottom: -0.3rem; left: calc(var(--g) * 100%); right: auto; width: 2px; height: auto; }
  :host([orientation="horizontal"]) .thumb { top: 50%; bottom: auto; left: calc(var(--pos) * 100%); width: var(--cap); height: 100%; transform: translate(-50%, -50%); }
  :host([orientation="horizontal"]) .thumb::before { width: 3px; height: 62%; }
  :host([orientation="horizontal"]) .thumb::after { inset: 0.5rem 0.35rem; background: repeating-linear-gradient(90deg, transparent 0 0.28rem, var(--line) 0.28rem calc(0.28rem + 1px)); -webkit-mask: linear-gradient(90deg, #000 0 calc(50% - 0.4rem), transparent 0 calc(50% + 0.4rem), #000 0); mask: linear-gradient(90deg, #000 0 calc(50% - 0.4rem), transparent 0 calc(50% + 0.4rem), #000 0); }
  :host([orientation="horizontal"]) .tick { bottom: auto; right: auto; left: calc(var(--p) * 100%); top: 0.1rem; transform: translateX(-50%); flex-direction: column-reverse; gap: 0.2rem; }
  :host([orientation="horizontal"]) .tick::after { width: 1px; height: 0.3rem; }

  :host { --fill-c: var(--accent); }
  :host([accent="red"]) { --fill-c: var(--red); }
  :host([accent="green"]) { --fill-c: var(--green); }
  :host([accent="amber"]) { --fill-c: var(--amber); }
`;

export class SSFader extends SSValueElement {
  static styles = styles;
  static observedAttributes = [...SSValueElement.observedAttributes, 'orientation', 'ticks', 'tick-labels', 'ground', 'accent'];

  build(root) {
    root.innerHTML = `
      <div class="head"><span class="label" part="label"></span><span class="readout" part="readout"><span class="num"></span><small class="unit"></small></span></div>
      <div class="body">
        <div class="scale"><div class="rail"></div></div>
        <div class="track"><div class="rail"><div class="groove"><div class="fill"></div><div class="ground"></div></div><div class="thumb"></div></div></div>
      </div>`;
    this.$ = {
      label: root.querySelector('.label'), num: root.querySelector('.num'), unit: root.querySelector('.unit'),
      scale: root.querySelector('.scale .rail'), track: root.querySelector('.track'),
      rail: root.querySelector('.track .rail'), fill: root.querySelector('.fill'), ground: root.querySelector('.ground'), thumb: root.querySelector('.thumb'),
    };
    this._ticksKey = '';
    this.$.track.addEventListener('pointerdown', this._down);
    this.$.track.addEventListener('pointermove', this._move);
    this.$.track.addEventListener('pointerup', this._up);
    this.$.track.addEventListener('pointercancel', this._up);
    this.addEventListener('wheel', this._wheel, { passive: false });
  }

  get horizontal() { return this.getAttribute('orientation') === 'horizontal'; }

  update() {
    const $ = this.$;
    this.setAttribute('aria-orientation', this.horizontal ? 'horizontal' : 'vertical');
    if ($.label.textContent !== this.label) $.label.textContent = this.label;
    $.num.textContent = this.format();
    $.unit.textContent = this.unit;
    const r = this.ratio();
    const base = this.bool('bipolar') || this.hasAttribute('bipolar') ? this.ratio(this.defaultValue) : 0;
    const lo = Math.min(r, base);
    this.style.setProperty('--pos', r.toFixed(4));
    this.style.setProperty('--lo', lo.toFixed(4));
    this.style.setProperty('--span', Math.abs(r - base).toFixed(4));
    const ground = this.hasAttribute('ground') ? this.num('ground', 0) : (this.hasAttribute('default') ? this.defaultValue : null);
    $.ground.hidden = ground === null;
    if (ground !== null) this.style.setProperty('--g', this.ratio(ground).toFixed(4));
    this.syncAria();
    this.setBool('dragging', this._dragging);
    this.renderTicks();
  }

  renderTicks() {
    const spec = this.getAttribute('ticks') || 'auto';
    const labels = this.getAttribute('tick-labels') || 'all';
    const key = [spec, labels, this.min, this.max, this.decimals, this.bool('signed')].join('|');
    if (key === this._ticksKey) return;
    this._ticksKey = key;
    this.$.scale.replaceChildren();
    this.$.scale.parentElement.hidden = spec === 'none';
    if (spec === 'none') return;
    let values;
    if (spec === 'auto') values = [0, 0.25, 0.5, 0.75, 1].map((r) => this.min + r * (this.max - this.min));
    else values = spec.split(',').map(Number).filter(Number.isFinite);
    values.forEach((v, i) => {
      const t = document.createElement('div');
      t.className = 'tick';
      t.style.setProperty('--p', this.ratio(v).toFixed(4));
      const show = labels === 'all' || (labels === 'ends' && (i === 0 || i === values.length - 1));
      t.textContent = show ? this.tickLabel(v) : '';
      if (v === 0) t.dataset.zero = '';
      this.$.scale.append(t);
    });
  }

  tickLabel(v) {
    const whole = Number.isInteger(v);
    const text = this.formatter ? this.formatter(v) : (this.bool('signed') ? (v > 0 ? '+' : v < 0 ? '\u2212' : '') + Math.abs(v).toFixed(whole ? 0 : this.decimals) : v.toFixed(whole ? 0 : this.decimals));
    return text;
  }

  // ---- pointer ----
  _railPos(ev) {
    const rect = this.$.rail.getBoundingClientRect();
    return this.horizontal
      ? clamp((ev.clientX - rect.left) / rect.width, 0, 1)
      : clamp((rect.bottom - ev.clientY) / rect.height, 0, 1);
  }

  _down = (ev) => {
    if (this.disabled || ev.button > 0) return;
    ev.preventDefault();
    this.focus({ preventScroll: true });
    this.$.track.setPointerCapture(ev.pointerId);
    this._dragging = true;
    this._fine = ev.shiftKey;
    this._startPos = this._railPos(ev);
    this._startRatio = this.ratio();
    if (!this._fine) this.userSet(this.fromRatio(this._startPos));
    this.setBool('dragging', true);
  };

  _move = (ev) => {
    if (!this._dragging) return;
    const pos = this._railPos(ev);
    if (ev.shiftKey) {
      if (!this._fine) { this._fine = true; this._startPos = pos; this._startRatio = this.ratio(); }
      this.userSet(this.fromRatio(this._startRatio + (pos - this._startPos) / 8));
    } else {
      if (this._fine) this._fine = false;
      this.userSet(this.fromRatio(pos));
    }
  };

  _up = (ev) => {
    if (!this._dragging) return;
    this._dragging = false;
    this.setBool('dragging', false);
    if (this.$.track.hasPointerCapture(ev.pointerId)) this.$.track.releasePointerCapture(ev.pointerId);
    this.commit();
  };

  _wheel = (ev) => {
    if (this.disabled || this.getRootNode().activeElement !== this && document.activeElement !== this) return;
    ev.preventDefault();
    const dir = ev.deltaY < 0 ? 1 : -1;
    this.userSet(this._value + dir * (ev.shiftKey ? this.step / 10 : this.step));
    clearTimeout(this._commitTimer);
    this._commitTimer = setTimeout(() => this.commit(), 250);
  };
}

define('ss-fader', SSFader);
