// <ss-toggle> — a large on/off switch with a label.
//
//   <ss-toggle label="Auto director" checked></ss-toggle>
//
// Attributes: checked, label (or default slot), sub (small helper text), disabled, tone (""|red|amber|green: colour when on)
// Properties: checked (get/set; setting it never emits)
// Events: change {checked} — only for user actions (click, Space, Enter). The page decides what to do;
//   it should call act(...) and let the store write `checked` back.
// Role/ARIA: role="switch" with aria-checked on the inner button.

import { css, define, SSElement } from './base.js';

const styles = css`
  :host { display: inline-block; --on: var(--accent); }
  :host([tone="red"]) { --on: var(--red-solid); }
  :host([tone="amber"]) { --on: var(--amber); }
  :host([tone="green"]) { --on: var(--green); }
  button { display: inline-flex; align-items: center; gap: var(--s-3); min-height: var(--touch); padding: 0; text-align: left; }
  .track { position: relative; width: 3.1rem; height: 1.75rem; border-radius: var(--r-pill); background: var(--surface-inset); border: 1px solid var(--line-strong); box-shadow: inset 0 1px 3px rgba(0,0,0,0.4); transition: background var(--dur-2) var(--ease), border-color var(--dur-2); flex: none; }
  .thumb { position: absolute; top: 50%; left: 0.2rem; width: 1.3rem; height: 1.3rem; border-radius: 50%; background: var(--text-2); transform: translateY(-50%); transition: transform var(--dur-2) var(--ease), background var(--dur-2); box-shadow: 0 1px 3px rgba(0,0,0,0.5); display: grid; place-items: center; }
  :host([checked]) .track { background: color-mix(in srgb, var(--on) 28%, var(--surface-inset)); border-color: var(--on); box-shadow: 0 0 1rem -0.2rem var(--on), inset 0 1px 3px rgba(0,0,0,0.25); }
  :host([checked]) .thumb { transform: translate(1.34rem, -50%); background: var(--on); }
  :host([disabled]) button { opacity: 0.45; }
  .text { display: flex; flex-direction: column; min-width: 0; }
  .label { font-weight: 650; font-size: var(--fs-sm); color: var(--text); }
  .sub { font-size: var(--fs-xs); color: var(--text-3); }
  .state { font-family: var(--font-mono); font-size: var(--fs-2xs); font-weight: 700; color: var(--text-3); letter-spacing: 0.06em; }
  :host([checked]) .state { color: var(--on); }
`;

export class SSToggle extends SSElement {
  static styles = styles;
  static delegatesFocus = true;
  static observedAttributes = ['checked', 'label', 'sub', 'disabled', 'tone'];

  get checked() { return this.hasAttribute('checked'); }
  set checked(v) { this.setBool('checked', !!v); }

  build(root) {
    root.innerHTML = `<button type="button" role="switch" part="button"><span class="track"><span class="thumb"></span></span>
      <span class="text"><span class="label"></span><span class="sub"></span></span><span class="state"></span></button>`;
    this.btn = root.querySelector('button');
    this.$ = { label: root.querySelector('.label'), sub: root.querySelector('.sub'), state: root.querySelector('.state') };
    this.btn.addEventListener('click', () => {
      if (this.bool('disabled')) return;
      const next = !this.checked;
      this.checked = next;
      this.emit('change', { checked: next });
    });
  }

  update() {
    const label = this.getAttribute('label') || '';
    this.$.label.textContent = label;
    this.$.label.hidden = !label;
    const sub = this.getAttribute('sub') || '';
    this.$.sub.textContent = sub;
    this.$.sub.hidden = !sub;
    this.$.state.textContent = this.checked ? 'ON' : 'OFF';
    this.btn.setAttribute('aria-checked', String(this.checked));
    this.btn.setAttribute('aria-label', label || 'toggle');
    this.btn.disabled = this.bool('disabled');
  }
}

define('ss-toggle', SSToggle);
