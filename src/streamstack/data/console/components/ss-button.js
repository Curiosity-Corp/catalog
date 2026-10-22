// <ss-button> — the one button. Optional hold-to-confirm for dangerous actions.
//
//   <ss-button variant="primary" icon="play">Start studio</ss-button>
//   <ss-button variant="danger" icon="power" hold="900">Stop studio</ss-button>
//
// Attributes:
//   variant   default | primary | danger | ghost
//   size      sm | md (default) | lg
//   icon      icon name from components/icons.js (optional, shown before the label)
//   hold      milliseconds to hold before it fires (hold-to-confirm); a ring fills while holding and
//             `click` is dispatched on completion. Space/Enter work too (hold the key).
//   pressed   toggle state (sets aria-pressed and the lit look)
//   busy      shows a spinner and ignores presses
//   block     full width
//   disabled
// Slots: default = label.
// Events: native `click` (for hold buttons: only after the hold completed).
// Parts: `button`.

import { css, define, SSElement } from './base.js';
import { iconHTML } from './icons.js';
import { toast } from '../core/toast.js';

const styles = css`
  :host { display: inline-block; --btn-bg: var(--surface-2); --btn-fg: var(--text); --btn-bd: var(--line-strong); --btn-hover: var(--surface-3); }
  :host([block]) { display: block; }
  :host([variant="primary"]) { --btn-bg: var(--accent); --btn-fg: var(--accent-ink); --btn-bd: transparent; --btn-hover: var(--accent-strong); }
  :host([variant="danger"]) { --btn-bg: var(--red-soft); --btn-fg: var(--red); --btn-bd: color-mix(in srgb, var(--red) 55%, transparent); --btn-hover: color-mix(in srgb, var(--red) 24%, transparent); }
  :host([variant="danger"][pressed]) { --btn-bg: var(--red-solid); --btn-fg: var(--on-solid); --btn-bd: transparent; }
  :host([variant="ghost"]) { --btn-bg: transparent; --btn-bd: transparent; --btn-fg: var(--text-2); --btn-hover: var(--surface-3); }
  button { position: relative; overflow: hidden; display: inline-flex; align-items: center; justify-content: center; gap: var(--s-2); width: 100%; min-height: 2.5rem; padding: 0 var(--s-4); border-radius: var(--r-2); background: var(--btn-bg); color: var(--btn-fg); border: 1px solid var(--btn-bd); font-weight: 620; font-size: var(--fs-sm); line-height: 1; white-space: nowrap; transition: background var(--dur-1), transform var(--dur-1), border-color var(--dur-1), box-shadow var(--dur-1); box-shadow: var(--shadow-1); user-select: none; touch-action: manipulation; }
  :host([variant="ghost"]) button { box-shadow: none; }
  button:hover:not(:disabled) { background: var(--btn-hover); }
  button:active:not(:disabled) { transform: scale(0.975); }
  :host([size="sm"]) button { min-height: 2rem; padding: 0 var(--s-3); font-size: var(--fs-xs); }
  :host([size="lg"]) button { min-height: 3.4rem; padding: 0 var(--s-5); font-size: var(--fs-md); border-radius: var(--r-3); }
  :host([pressed]:not([variant="danger"])) button { background: var(--accent-soft); border-color: var(--accent); color: var(--accent-strong); box-shadow: 0 0 1.2rem -0.3rem var(--accent-glow); }
  button:disabled { opacity: 0.45; box-shadow: none; }
  .icon { width: 1.15em; height: 1.15em; flex: none; }
  .hold { position: absolute; inset: 0; background: color-mix(in srgb, currentColor 22%, transparent); transform-origin: left; transform: scaleX(var(--hold, 0)); pointer-events: none; }
  .spin { animation: spin 0.9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .label { display: inline-flex; align-items: center; gap: var(--s-2); position: relative; }
`;

export class SSButton extends SSElement {
  static styles = styles;
  static delegatesFocus = true;
  static observedAttributes = ['variant', 'size', 'icon', 'hold', 'pressed', 'busy', 'disabled', 'block'];

  build(root) {
    root.innerHTML = '<button part="button" type="button"><span class="hold"></span><span class="ic"></span><span class="label"><slot></slot></span></button>';
    this.btn = root.querySelector('button');
    this.holdEl = root.querySelector('.hold');
    this.ic = root.querySelector('.ic');
    this.ic.style.display = 'contents';
    this._iconKey = '';
    this.btn.addEventListener('click', this._onClick, true);
    this.btn.addEventListener('pointerdown', this._pdown);
    this.btn.addEventListener('keydown', this._kdown);
    for (const t of ['pointerup', 'pointerleave', 'pointercancel']) this.btn.addEventListener(t, () => this._cancelHold());
    this.btn.addEventListener('keyup', () => this._cancelHold());
    this.btn.addEventListener('blur', () => this._cancelHold());
  }

  update() {
    const busy = this.bool('busy');
    const key = busy ? 'loader!' : this.getAttribute('icon') || '';
    if (key !== this._iconKey) {
      this._iconKey = key;
      this.ic.innerHTML = key ? iconHTML(busy ? 'loader' : key, busy ? 'icon spin' : 'icon') : '';
    }
    this.btn.disabled = this.bool('disabled') || busy;
    if (this.hasAttribute('pressed')) this.btn.setAttribute('aria-pressed', String(this.bool('pressed')));
    else this.btn.removeAttribute('aria-pressed');
    this.btn.setAttribute('aria-busy', String(busy));
    if (this.hold) this.btn.setAttribute('aria-description', 'Press and hold to confirm');
  }

  get hold() { return this.num('hold', 0); }

  _onClick = (ev) => {
    if (!this.hold || this._firing) return;
    ev.stopImmediatePropagation();      // a quick tap never fires a hold button
    ev.preventDefault();
    if (this._completed) { this._completed = false; return; }   // the click that ends a finished hold
    toast('Press and hold to confirm', { level: 'info', timeout: 1800, id: 'hold-hint' });
  };

  _pdown = (ev) => {
    if (!this.hold || ev.button > 0 || this.btn.disabled) return;
    this._startHold();
  };

  _kdown = (ev) => {
    if (!this.hold || ev.repeat || (ev.key !== ' ' && ev.key !== 'Enter')) return;
    this._startHold();
  };

  _startHold() {
    this._cancelHold();
    this._completed = false;
    const t0 = performance.now();
    const step = (now) => {
      const p = Math.min(1, (now - t0) / this.hold);
      this.holdEl.style.setProperty('--hold', p.toFixed(3));
      if (p >= 1) {
        this._raf = 0;
        this._firing = true;
        this._completed = true;
        this.holdEl.style.setProperty('--hold', '0');
        this.click();
        this._firing = false;
        return;
      }
      this._raf = requestAnimationFrame(step);
    };
    this._raf = requestAnimationFrame(step);
  }

  _cancelHold() {
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = 0;
    this.holdEl?.style.setProperty('--hold', '0');
  }
}

define('ss-button', SSButton);
