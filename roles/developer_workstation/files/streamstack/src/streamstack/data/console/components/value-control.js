// components/value-control.js — shared behaviour of ss-fader and ss-knob (not an element itself).
//
// Attributes (both controls):
//   value, min, max, step, default   numbers. `default` is where double-click / Backspace reset to.
//   label, unit, decimals            text shown with the readout ("Mic gain", "dB", 1)
//   signed                           show an explicit +/− sign (gain, tone)
//   bipolar                          fill from `default` instead of from `min`
//   disabled                         inert and dimmed
//   readout="none"                   hide the numeric readout
// Properties: value (get/set), min, max, defaultValue, dragging (read-only), busy (read-only),
//   format(v) -> string  (override by assigning a function to `formatter`)
// Events (bubble, composed):
//   input   {value}   while the user moves the control; throttled to ≤ 15/s, last value always delivered
//   change  {value}   once the gesture ends (pointer up, key burst finished, double-click reset)
// The “dirty” rule: after any user input the control ignores programmatic `value` writes
//   for 600 ms (and while dragging) and applies the newest of them afterwards, so incoming
//   server state never yanks a control the person is holding.
// Keyboard: ←/↓ −step, →/↑ +step, Shift = fine (step/10), PageUp/PageDown ×10, Home/End min/max, Backspace/Delete reset.

import { SSElement } from './base.js';
import { clamp, throttle, signed } from '../core/util.js';

export const DIRTY_MS = 600;
export const INPUT_RATE_MS = 1000 / 15;

export class SSValueElement extends SSElement {
  static observedAttributes = ['value', 'min', 'max', 'step', 'default', 'unit', 'decimals', 'label', 'disabled', 'signed', 'bipolar', 'readout'];

  constructor() {
    super();
    this._value = 0;
    this._dragging = false;
    this._busyUntil = 0;
    this._ext = undefined;
    this._extTimer = 0;
    this._commitTimer = 0;
    this._emitInput = throttle((v) => this.emit('input', { value: v }), INPUT_RATE_MS);
    this.formatter = null;
  }

  // ---- numeric model ----
  get min() { return this.num('min', 0); }
  get max() { const m = this.num('max', 100); return m > this.min ? m : this.min + 1; }
  get step() { return this.num('step', (this.max - this.min) / 100) || 1; }
  get defaultValue() { return this.num('default', this.min); }
  get decimals() {
    const d = this.num('decimals', NaN);
    if (Number.isFinite(d)) return d;
    const s = String(this.step);
    return s.includes('.') ? Math.min(3, s.split('.')[1].length) : 0;
  }
  get value() { return this._value; }
  set value(v) { this.external(Number(v)); }
  get dragging() { return this._dragging; }
  get busy() { return this._dragging || performance.now() < this._busyUntil; }
  get disabled() { return this.bool('disabled'); }

  ratio(v = this._value) { return clamp((v - this.min) / (this.max - this.min), 0, 1); }
  fromRatio(r) { return this.snap(this.min + clamp(r, 0, 1) * (this.max - this.min)); }
  snap(v) {
    const st = this.step;
    const snapped = this.min + Math.round((v - this.min) / st) * st;
    return clamp(Number(snapped.toFixed(6)), this.min, this.max);
  }

  format(v = this._value) {
    if (typeof this.formatter === 'function') return this.formatter(v);
    if (!Number.isFinite(v)) return '–';
    return this.bool('signed') ? signed(v, this.decimals) : v.toFixed(this.decimals);
  }
  get unit() { return this.getAttribute('unit') || ''; }
  get label() { return this.getAttribute('label') || ''; }
  valueText() { return `${this.format()}${this.unit ? ' ' + this.unit : ''}`; }

  // ---- lifecycle ----
  attributeChangedCallback(name, oldV, newV) {
    if (oldV === newV) return;
    if (name === 'value') { this.external(newV === null ? this.min : Number(newV)); return; }
    super.attributeChangedCallback(name, oldV, newV);
  }

  connectedCallback() {
    if (!this._ready && this.hasAttribute('value')) this._value = this.snapIfFinite(Number(this.getAttribute('value')));
    if (!this.hasAttribute('tabindex')) this.tabIndex = 0;
    this.setAttribute('role', 'slider');
    super.connectedCallback();
    this.addEventListener('keydown', this._onKey);
    this.addEventListener('dblclick', this._onReset);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this.removeEventListener('keydown', this._onKey);
    this.removeEventListener('dblclick', this._onReset);
    clearTimeout(this._extTimer);
    clearTimeout(this._commitTimer);
    this._emitInput.cancel();
  }

  snapIfFinite(v) { return Number.isFinite(v) ? clamp(v, this.min, this.max) : this.min; }

  /** Programmatic write (state from the server). Deferred while the user is holding the control. */
  external(v) {
    if (!Number.isFinite(v)) return;
    v = clamp(v, this.min, this.max);
    if (this.busy) {
      this._ext = v;
      clearTimeout(this._extTimer);
      this._extTimer = setTimeout(() => this.external(this._ext), Math.max(30, this._busyUntil - performance.now() + 20));
      return;
    }
    this._ext = undefined;
    if (v === this._value) return;
    this._value = v;
    if (this._ready) this.queueUpdate();
  }

  /** The user moved the control. */
  userSet(v) {
    v = this.snap(v);
    this._busyUntil = performance.now() + DIRTY_MS;
    if (v === this._value) return;
    this._value = v;
    this.update();
    this._emitInput(v);
  }

  /** The gesture ended. */
  commit() {
    this._busyUntil = performance.now() + DIRTY_MS;
    this._emitInput.flush();
    this.emit('change', { value: this._value });
  }

  _onKey = (ev) => {
    if (this.disabled || ev.altKey || ev.ctrlKey || ev.metaKey) return;
    const fine = ev.shiftKey;
    const st = fine ? this.step / 10 : this.step;
    let v = this._value;
    switch (ev.key) {
      case 'ArrowLeft': case 'ArrowDown': v -= st; break;
      case 'ArrowRight': case 'ArrowUp': v += st; break;
      case 'PageDown': v -= this.step * 10; break;
      case 'PageUp': v += this.step * 10; break;
      case 'Home': v = this.min; break;
      case 'End': v = this.max; break;
      case 'Backspace': case 'Delete': v = this.defaultValue; break;
      default: return;
    }
    ev.preventDefault();
    // fine steps can be smaller than `step`: bypass snapping for them, but keep range
    if (fine) {
      this._busyUntil = performance.now() + DIRTY_MS;
      const nv = clamp(Number(v.toFixed(6)), this.min, this.max);
      if (nv !== this._value) { this._value = nv; this.update(); this._emitInput(nv); }
    } else {
      this.userSet(v);
    }
    clearTimeout(this._commitTimer);
    this._commitTimer = setTimeout(() => this.commit(), 250);
  };

  _onReset = (ev) => {
    if (this.disabled || !this.hasAttribute('default')) return;
    ev.preventDefault();
    this.userSet(this.defaultValue);
    this.commit();
  };

  /** Keep aria-* in sync (cheap; call from update()). */
  syncAria() {
    this.setAttribute('aria-valuemin', String(this.min));
    this.setAttribute('aria-valuemax', String(this.max));
    this.setAttribute('aria-valuenow', String(Number(this._value.toFixed(4))));
    this.setAttribute('aria-valuetext', this.valueText());
    this.setAttribute('aria-disabled', this.disabled ? 'true' : 'false');
    if (this.label) this.setAttribute('aria-label', this.label);
  }
}
