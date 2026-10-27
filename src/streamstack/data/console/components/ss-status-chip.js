// <ss-status-chip> — a compact status indicator; a link when it has `href`.
//
//   <ss-status-chip state="ok" label="Engine" value="running" icon="check"></ss-status-chip>
//   <ss-status-chip state="live" label="ON AIR" pulse></ss-status-chip>
//   <ss-status-chip state="warn" label="Camera" value="Raw" href="#/camera"></ss-status-chip>
//
// Attributes:
//   state   ok (green) | warn (amber) | fail (red) | live (solid red, white text: on air / rec) | info (blue) | off (grey, default)
//   label   main text (UPPER-CASE styling is up to the caller)
//   value   secondary text, dimmer
//   icon    icon name; defaults to a dot. `pulse` makes the icon/dot breathe (not under reduced motion)
//   href    makes the chip a link (hash routes: "#/audio"); without it the chip is static
//   size    sm | md (default) | lg
//   mono    use the monospace face (timecodes)
// Events: native click. Parts: `chip`.
// State is carried by the icon and the words as well as the colour.

import { css, define, SSElement } from './base.js';
import { iconHTML } from './icons.js';

const styles = css`
  :host { display: inline-flex; max-width: 100%; --c: var(--text-3); --bg: var(--surface-2); --bd: var(--line); }
  :host([state="ok"]) { --c: var(--green); --bg: var(--green-soft); --bd: color-mix(in srgb, var(--green) 35%, transparent); }
  :host([state="warn"]) { --c: var(--amber); --bg: var(--amber-soft); --bd: color-mix(in srgb, var(--amber) 40%, transparent); }
  :host([state="fail"]) { --c: var(--red); --bg: var(--red-soft); --bd: color-mix(in srgb, var(--red) 45%, transparent); }
  :host([state="info"]) { --c: var(--accent-strong); --bg: var(--accent-soft); --bd: color-mix(in srgb, var(--accent) 40%, transparent); }
  :host([state="live"]) { --c: var(--on-solid); --bg: var(--red-solid); --bd: transparent; }
  .chip { display: inline-flex; align-items: center; gap: 0.4rem; max-width: 100%; min-height: 1.7rem; padding: 0.2rem 0.65rem 0.2rem 0.55rem; border-radius: var(--r-pill); background: var(--bg); border: 1px solid var(--bd); color: var(--c); font-size: var(--fs-xs); font-weight: 680; letter-spacing: 0.03em; white-space: nowrap; text-decoration: none; transition: background var(--dur-1), border-color var(--dur-1), filter var(--dur-1); }
  a.chip:hover { filter: brightness(1.18); border-color: var(--c); }
  :host([state="live"]) .chip { box-shadow: 0 0 1.1rem -0.1rem var(--red-glow); }
  :host([size="sm"]) .chip { min-height: 1.4rem; font-size: var(--fs-2xs); padding: 0.1rem 0.5rem 0.1rem 0.4rem; }
  :host([size="lg"]) .chip { min-height: 2.2rem; font-size: var(--fs-sm); padding: 0.3rem 0.9rem 0.3rem 0.75rem; }
  :host([mono]) .chip { font-family: var(--font-mono); letter-spacing: 0; }
  .icon { width: 1.05em; height: 1.05em; flex: none; }
  .value { color: color-mix(in srgb, var(--c) 72%, var(--text-2)); font-weight: 560; overflow: hidden; text-overflow: ellipsis; }
  :host([state="live"]) .value { color: var(--on-solid); opacity: 0.9; }
  :host([pulse]) .ic { animation: breathe 1.4s ease-in-out infinite; }
  @keyframes breathe { 50% { opacity: 0.35; } }
  .ic { display: contents; }
  .label { overflow: hidden; text-overflow: ellipsis; }
`;

export class SSStatusChip extends SSElement {
  static styles = styles;
  static delegatesFocus = true;
  static observedAttributes = ['state', 'label', 'value', 'icon', 'pulse', 'href', 'size', 'mono'];

  build(root) {
    root.innerHTML = '<span class="chip" part="chip"></span>';
    this.host = root.querySelector('.chip');
    this._sig = '';
  }

  update() {
    const href = this.getAttribute('href');
    const tag = href ? 'a' : 'span';
    if (this.host.localName !== tag) {
      const next = document.createElement(tag);
      next.className = 'chip';
      next.setAttribute('part', 'chip');
      this.host.replaceWith(next);
      this.host = next;
      this._sig = '';
    }
    if (href) this.host.setAttribute('href', href); else this.host.removeAttribute('href');
    const icon = this.getAttribute('icon') || 'dot';
    const sig = [icon, this.getAttribute('label'), this.getAttribute('value')].join('\u0000');
    if (sig !== this._sig) {
      this._sig = sig;
      const label = this.getAttribute('label');
      const value = this.getAttribute('value');
      this.host.innerHTML = `<span class="ic">${iconHTML(icon)}</span>${label ? '<span class="label"></span>' : ''}${value ? '<span class="value"></span>' : ''}`;
      if (label) this.host.querySelector('.label').textContent = label;
      if (value) this.host.querySelector('.value').textContent = value;
    }
  }
}

define('ss-status-chip', SSStatusChip);
