// <ss-card> — the panel every page is made of.
//
//   <ss-card heading="Voice" icon="mic">
//     <ss-toggle slot="actions" label="Bypass"></ss-toggle>
//     …content…
//   </ss-card>
//
// Attributes:
//   heading   title (not `title`, which would show a native tooltip)
//   icon      icon name shown before the heading
//   reason    when set, the content is inert and dimmed and this text is shown over it:
//             the card's *disabled state with the reason* ("OBS is not running", "No microphone found")
//   fix       label of a one-press fix button shown with the reason ("Start studio")
//   tone      "" | live (red edge) | warn (amber edge)
//   flush     no inner padding (previews, lists)
//   inline-reason  lay the reason out as one centred line (icon, text, fix) for short cards (a row of pads)
//   bare      no header and no background (layout-only wrapper that still supports `reason`)
// Slots: default (body, fills the remaining height), `actions` (right side of the header), `heading` (custom heading content).
// Events: `fix` when the fix button is pressed (the page decides which action to run).

import { css, define, SSElement } from './base.js';
import { iconHTML } from './icons.js';

const styles = css`
  :host { display: flex; flex-direction: column; min-width: 0; min-height: 0; position: relative; background: var(--surface-1); border: 1px solid var(--line); border-radius: var(--r-4); box-shadow: var(--shadow-2); overflow: hidden; }
  :host([tone="live"]) { border-color: color-mix(in srgb, var(--red) 70%, transparent); box-shadow: var(--shadow-2), 0 0 2rem -0.6rem var(--red-glow); }
  :host([tone="warn"]) { border-color: color-mix(in srgb, var(--amber) 60%, transparent); }
  :host([bare]) { background: none; border: 0; box-shadow: none; border-radius: 0; overflow: visible; }
  header { display: flex; align-items: center; gap: var(--s-2); padding: var(--s-3) var(--s-4) 0; min-height: 2.4rem; }
  :host([bare]) header { display: none; }
  .title { display: flex; align-items: center; gap: var(--s-2); font-size: var(--fs-2xs); font-weight: 700; letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--text-3); min-width: 0; }
  .title .icon { width: 1.05rem; height: 1.05rem; color: var(--text-2); }
  .actions { margin-left: auto; display: flex; align-items: center; gap: var(--s-2); }
  .body { flex: 1; min-height: 0; display: flex; flex-direction: column; padding: var(--s-3) var(--s-4) var(--s-4); transition: opacity var(--dur-2), filter var(--dur-2); }
  :host([flush]) .body { padding: 0; }
  :host([bare]) .body { padding: 0; }
  :host([reason]) .body { opacity: 0.28; filter: grayscale(0.7); pointer-events: none; user-select: none; }
  .reason { position: absolute; inset: 0; display: none; flex-direction: column; align-items: center; justify-content: center; gap: var(--s-3); padding: var(--s-4); text-align: center; z-index: 2; }
  :host([reason]) .reason { display: flex; }
  :host([inline-reason]) .reason { flex-direction: row; justify-content: center; gap: var(--s-3); }
  :host([inline-reason]) .reason p { max-width: none; }
  :host([inline-reason]) .reason .icon { width: 1.2rem; height: 1.2rem; }
  .reason p { margin: 0; max-width: 24ch; font-size: var(--fs-sm); font-weight: 600; color: var(--text); text-wrap: balance; }
  .reason .icon { width: 1.5rem; height: 1.5rem; color: var(--amber); }
  .fix { padding: 0.5rem 1.1rem; min-height: 2.4rem; border-radius: var(--r-pill); background: var(--accent); color: var(--accent-ink); font-weight: 700; font-size: var(--fs-sm); box-shadow: 0 0 1.4rem -0.2rem var(--accent-glow); }
  .fix:hover { background: var(--accent-strong); }
  .fix[hidden] { display: none; }
`;

export class SSCard extends SSElement {
  static styles = styles;
  static observedAttributes = ['heading', 'icon', 'reason', 'fix', 'tone', 'flush', 'bare', 'inline-reason'];

  build(root) {
    root.innerHTML = `<header><div class="title"><span class="ic"></span><slot name="heading"><span class="h"></span></slot></div><div class="actions"><slot name="actions"></slot></div></header>
      <div class="body"><slot></slot></div>
      <div class="reason" role="status"><span class="ri"></span><p></p><button class="fix" type="button"></button></div>`;
    this.$ = { ic: root.querySelector('.ic'), h: root.querySelector('.h'), p: root.querySelector('.reason p'), fix: root.querySelector('.fix'), body: root.querySelector('.body'), ri: root.querySelector('.ri') };
    this.$.ic.style.display = 'contents';
    this.$.ri.style.display = 'contents';
    this.$.ri.innerHTML = iconHTML('alert');
    this.$.fix.addEventListener('click', () => this.emit('fix'));
  }

  update() {
    this.$.h.textContent = this.getAttribute('heading') || '';
    const icon = this.getAttribute('icon');
    this.$.ic.innerHTML = icon ? iconHTML(icon) : '';
    const reason = this.getAttribute('reason') || '';
    this.$.p.textContent = reason;
    const fix = this.getAttribute('fix') || '';
    this.$.fix.textContent = fix;
    this.$.fix.hidden = !fix;
    this.$.body.toggleAttribute('inert', !!reason);
  }
}

define('ss-card', SSCard);
