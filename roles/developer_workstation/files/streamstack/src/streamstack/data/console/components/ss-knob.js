// <ss-knob> — a rotary control (tone, pitch, trims).
//
//   <ss-knob label="Bass" unit="dB" min="-12" max="12" step="0.5" default="0" value="2" signed bipolar></ss-knob>
//
// Attributes: everything in value-control.js plus
//   size    sm | md (default) | lg
//   accent  "" | red | green | amber
// Gestures: drag up/right to increase (full range ≈ 220 px; Shift = 8× finer), mouse wheel over the knob,
//   arrow/Page/Home/End keys, double-click resets to `default`.
// Events: input {value} (≤15/s) and change {value}; see value-control.js.
// CSS hooks: --knob-size (diameter, default 4.6rem).

import { css, define } from './base.js';
import { SSValueElement } from './value-control.js';
import { clamp } from '../core/util.js';

const SWEEP = 270;               // degrees of travel
const START = -135;              // pointing down-left; 0 = straight up
const R = 38;

const styles = css`
  :host { display: inline-flex; flex-direction: column; align-items: center; gap: var(--s-1); user-select: none; touch-action: none; --fill-c: var(--accent); --knob-d: var(--knob-size, 4.6rem); }
  :host([size="sm"]) { --knob-d: 3.4rem; }
  :host([size="lg"]) { --knob-d: 6.2rem; }
  :host([accent="red"]) { --fill-c: var(--red); }
  :host([accent="green"]) { --fill-c: var(--green); }
  :host([accent="amber"]) { --fill-c: var(--amber); }
  :host([disabled]) { opacity: 0.45; pointer-events: none; }
  .dial { position: relative; width: var(--knob-d); height: var(--knob-d); cursor: grab; border-radius: 50%; }
  :host([dragging]) .dial { cursor: grabbing; }
  :host(:focus-visible) .dial { outline: 2px solid var(--focus); outline-offset: 3px; }
  svg { width: 100%; height: 100%; display: block; overflow: visible; }
  .trk { fill: none; stroke: var(--surface-inset); stroke-width: 7; stroke-linecap: round; }
  .val { fill: none; stroke: var(--fill-c); stroke-width: 7; stroke-linecap: round; filter: drop-shadow(0 0 3px var(--fill-c)); }
  .cap { fill: var(--surface-2); stroke: var(--line-strong); stroke-width: 1.5; }
  .cap-hi { fill: none; stroke: rgba(255,255,255,0.08); stroke-width: 1.5; }
  .ptr { stroke: var(--text); stroke-width: 4; stroke-linecap: round; }
  :host(:hover) .cap { fill: var(--surface-3); }
  .readout { font-family: var(--font-mono); font-weight: 600; font-size: var(--fs-sm); color: var(--text); white-space: nowrap; }
  .readout small { color: var(--text-3); font-size: var(--fs-2xs); margin-left: 0.2em; font-weight: 500; }
  .label { font-size: var(--fs-2xs); font-weight: 650; letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--text-3); }
  :host([readout="none"]) .readout { display: none; }
`;

const pt = (deg, r) => {
  const a = (deg - 90) * Math.PI / 180;
  return [50 + r * Math.cos(a), 50 + r * Math.sin(a)];
};
function arc(from, to, r = R) {
  if (Math.abs(to - from) < 0.01) return '';
  const [x1, y1] = pt(from, r);
  const [x2, y2] = pt(to, r);
  const large = Math.abs(to - from) > 180 ? 1 : 0;
  const sweep = to > from ? 1 : 0;
  return `M${x1.toFixed(2)} ${y1.toFixed(2)}A${r} ${r} 0 ${large} ${sweep} ${x2.toFixed(2)} ${y2.toFixed(2)}`;
}

export class SSKnob extends SSValueElement {
  static styles = styles;
  static observedAttributes = [...SSValueElement.observedAttributes, 'size', 'accent'];

  build(root) {
    root.innerHTML = `
      <div class="dial" part="dial">
        <svg viewBox="0 0 100 100" aria-hidden="true">
          <path class="trk"></path><path class="val"></path>
          <circle class="cap" cx="50" cy="50" r="27"></circle><circle class="cap-hi" cx="50" cy="50" r="24.5"></circle>
          <line class="ptr"></line>
        </svg>
      </div>
      <span class="readout"><span class="num"></span><small class="unit"></small></span>
      <span class="label"></span>`;
    this.$ = {
      dial: root.querySelector('.dial'), trk: root.querySelector('.trk'), val: root.querySelector('.val'),
      ptr: root.querySelector('.ptr'), num: root.querySelector('.num'), unit: root.querySelector('.unit'), label: root.querySelector('.label'),
    };
    this.$.trk.setAttribute('d', arc(START, START + SWEEP));
    this.$.dial.addEventListener('pointerdown', this._down);
    this.$.dial.addEventListener('pointermove', this._move);
    this.$.dial.addEventListener('pointerup', this._up);
    this.$.dial.addEventListener('pointercancel', this._up);
    this.addEventListener('wheel', this._wheel, { passive: false });
  }

  update() {
    const $ = this.$;
    const r = this.ratio();
    const angle = START + r * SWEEP;
    const base = this.hasAttribute('bipolar') ? START + this.ratio(this.defaultValue) * SWEEP : START;
    $.val.setAttribute('d', arc(base, angle));
    const [x1, y1] = pt(angle, 11);
    const [x2, y2] = pt(angle, 23);
    $.ptr.setAttribute('x1', x1.toFixed(2)); $.ptr.setAttribute('y1', y1.toFixed(2));
    $.ptr.setAttribute('x2', x2.toFixed(2)); $.ptr.setAttribute('y2', y2.toFixed(2));
    $.num.textContent = this.format();
    $.unit.textContent = this.unit;
    $.label.textContent = this.label;
    this.syncAria();
    this.setAttribute('aria-orientation', 'vertical');
    this.setBool('dragging', this._dragging);
  }

  _down = (ev) => {
    if (this.disabled || ev.button > 0) return;
    ev.preventDefault();
    this.focus({ preventScroll: true });
    this.$.dial.setPointerCapture(ev.pointerId);
    this._dragging = true;
    this._y0 = ev.clientY; this._x0 = ev.clientX; this._r0 = this.ratio();
    this.setBool('dragging', true);
  };

  _move = (ev) => {
    if (!this._dragging) return;
    const dist = (this._y0 - ev.clientY) + (ev.clientX - this._x0);
    const gain = ev.shiftKey ? 8 : 1;
    this.userSet(this.fromRatio(clamp(this._r0 + dist / 220 / gain, 0, 1)));
  };

  _up = (ev) => {
    if (!this._dragging) return;
    this._dragging = false;
    this.setBool('dragging', false);
    if (this.$.dial.hasPointerCapture(ev.pointerId)) this.$.dial.releasePointerCapture(ev.pointerId);
    this.commit();
  };

  _wheel = (ev) => {
    if (this.disabled) return;
    ev.preventDefault();
    const dir = ev.deltaY < 0 ? 1 : -1;
    this.userSet(this._value + dir * (ev.shiftKey ? this.step / 10 : this.step));
    clearTimeout(this._commitTimer);
    this._commitTimer = setTimeout(() => this.commit(), 250);
  };
}

define('ss-knob', SSKnob);
