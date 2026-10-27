// <ss-pad> — a lit performance pad: scenes, voice presets, one-press actions.
//
//   <ss-pad label="Canon" hotkey="1" tally="program" active></ss-pad>
//   <ss-pad label="Warm" sub="more low end for thin rooms" icon="mic" active></ss-pad>
//
// Attributes:
//   label     main text
//   sub       secondary line (preset blurb, scene note); one line, ellipsised
//   hotkey    keycap shown top-right ("1", "B")
//   icon      icon name (top-left)
//   active    lit: this is the selected one (aria-pressed)
//   tally     program (red: it is on air) | preview (green) — only meaningful with `active`
//   tone      "" (blue) | red | amber — colour of the lit state when there is no tally (e.g. privacy = amber)
//   compact   smaller pad
//   fit       label wraps, up to three lines, at spaces and camelCase joins, and shrinks with the pad's width,
//             for a row of narrow pads that must show whole names (Live voice presets)
//   big       one-press control pad (MUTE, PRIVACY): centred large icon and label
//   row       with big: icon beside the text instead of above it (short pads)
//   solid     with big/active: fills solid with the tone colour and white text (the loud state)
//   disabled, busy
// Events: native `click`. Slots: default (extra content, bottom). Parts: `button`, `label`.
// Colour is never the only signal: tally pads also show the word PGM, active pads show a lit LED.

import { css, define, SSElement } from './base.js';
import { iconHTML } from './icons.js';

const styles = css`
  :host { display: block; min-width: 0; --pad-c: var(--accent); --pad-soft: var(--accent-soft); --pad-glow: var(--accent-glow); }
  :host([tone="red"]), :host([tally="program"]) { --pad-c: var(--red); --pad-soft: var(--red-soft); --pad-glow: var(--red-glow); }
  :host([tone="amber"]) { --pad-c: var(--amber); --pad-soft: var(--amber-soft); --pad-glow: color-mix(in srgb, var(--amber) 50%, transparent); }
  :host([tally="preview"]) { --pad-c: var(--green); --pad-soft: var(--green-soft); --pad-glow: color-mix(in srgb, var(--green) 50%, transparent); }
  button { position: relative; display: flex; flex-direction: column; justify-content: space-between; align-items: stretch; gap: var(--s-2); width: 100%; height: 100%; min-height: 4.2rem; padding: var(--s-3); text-align: left; border-radius: var(--r-3); background: linear-gradient(180deg, var(--surface-2), color-mix(in srgb, var(--surface-2) 70%, var(--surface-inset))); border: 1px solid var(--line-strong); box-shadow: var(--shadow-1); color: var(--text-2); transition: background var(--dur-2) var(--ease), border-color var(--dur-2), box-shadow var(--dur-2), transform var(--dur-1), color var(--dur-2); overflow: hidden; touch-action: manipulation; }
  :host([compact]) button { min-height: 3.2rem; padding: var(--s-2) var(--s-3); }
  button:hover:not(:disabled) { background: var(--surface-3); color: var(--text); border-color: var(--line-strong); }
  button:active:not(:disabled) { transform: scale(0.97); }
  .top { display: flex; align-items: center; justify-content: space-between; gap: var(--s-2); min-height: 1.1rem; }
  .led { width: 0.55rem; height: 0.55rem; border-radius: 50%; background: var(--meter-off); box-shadow: inset 0 0 0 1px var(--line-strong); flex: none; transition: background var(--dur-2), box-shadow var(--dur-2); }
  .icon { width: 1.2rem; height: 1.2rem; flex: none; }
  .tag { margin-left: auto; font-size: var(--fs-2xs); font-weight: 750; letter-spacing: var(--tracking-caps); color: var(--pad-c); display: none; }
  kbd { margin-left: auto; font-family: var(--font-mono); font-size: var(--fs-2xs); line-height: 1; padding: 0.18rem 0.38rem; border-radius: 0.3rem; background: var(--surface-inset); color: var(--text-3); border: 1px solid var(--line); }
  .label { font-weight: 680; font-size: var(--fs-md); color: inherit; line-height: 1.15; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sub { font-size: var(--fs-xs); color: var(--text-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 0.1rem; }
  .text { min-width: 0; }
  :host([active]) button { background: linear-gradient(180deg, color-mix(in srgb, var(--pad-c) 22%, var(--surface-2)), color-mix(in srgb, var(--pad-c) 10%, var(--surface-2))); border-color: var(--pad-c); color: var(--text); box-shadow: 0 0 0 1px var(--pad-c) inset, 0 0 1.6rem -0.2rem var(--pad-glow), var(--shadow-1); }
  :host([active]) .led { background: var(--pad-c); box-shadow: 0 0 0.7rem 0.1rem var(--pad-glow); }
  :host([active]) .sub { color: var(--text-2); }
  :host([active][tally]) .tag { display: block; }
  :host([active][tally]) kbd { margin-left: var(--s-2); }
  :host([active][tally]) .tag { margin-left: auto; }
  :host([big]) button { align-items: center; justify-content: center; text-align: center; gap: var(--s-1); min-height: 6rem; }
  :host([big]) .top { flex-direction: column; justify-content: center; gap: var(--s-2); }
  :host([big]) .top .icon { width: 2.6rem; height: 2.6rem; }
  :host([big]) .led { display: none; }
  :host([big]) kbd { position: absolute; top: var(--s-3); right: var(--s-3); margin: 0; }
  :host([big]) .tag { position: absolute; top: var(--s-3); left: var(--s-3); margin: 0; }
  :host([big]) .label { font-size: var(--fs-xl); font-weight: 800; letter-spacing: 0.04em; text-transform: uppercase; }
  :host([big]) .sub { font-size: var(--fs-sm); white-space: normal; }
  :host([big]) .text { text-align: center; }
  :host([big][active][solid]) button { background: linear-gradient(180deg, color-mix(in srgb, var(--pad-c) 100%, #fff 8%), var(--pad-c)); background-color: var(--pad-c); color: var(--on-solid); border-color: transparent; box-shadow: 0 0 2.6rem -0.2rem var(--pad-glow), var(--shadow-2); }
  :host([big][active][solid][tone="red"]) button { background: var(--red-solid); }
  :host([big][active][solid][tone="amber"]) button { background: var(--amber); color: var(--amber-ink); }
  :host([big][active][solid]) .sub, :host([big][active][solid]) kbd { color: inherit; opacity: 0.85; }
  :host([big][active][solid]) kbd { background: rgba(0,0,0,0.18); border-color: rgba(255,255,255,0.3); }
  :host([big][row]) button { flex-direction: row; justify-content: center; gap: var(--s-4); min-height: 4rem; text-align: left; padding: var(--s-3) var(--s-5); }
  :host([big][row]) .top { flex: none; }
  :host([big][row]) .top .icon { width: 2rem; height: 2rem; }
  :host([big][row]) .text { text-align: left; }
  :host([big][row]) .label { font-size: var(--fs-lg); }
  :host([fit]) { container-type: inline-size; }
  :host([fit]) button { padding-inline: var(--s-2); }
  :host([fit]) .label { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3; white-space: normal; overflow-wrap: break-word; font-size: clamp(var(--fs-xs), 12cqi, var(--fs-md)); }
  button:disabled { opacity: 0.45; box-shadow: none; }
  :host([busy]) button { animation: pulse 0.9s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: 0.6; } }
`;

export class SSPad extends SSElement {
  static styles = styles;
  static delegatesFocus = true;
  static observedAttributes = ['label', 'sub', 'hotkey', 'icon', 'active', 'tally', 'tone', 'compact', 'fit', 'big', 'row', 'solid', 'disabled', 'busy'];

  build(root) {
    root.innerHTML = `<button part="button" type="button">
      <span class="top"><span class="led"></span><span class="ic"></span><span class="tag"></span><kbd></kbd></span>
      <span class="text"><span class="label" part="label"></span><div class="sub"></div><slot></slot></span></button>`;
    this.btn = root.querySelector('button');
    this.$ = { ic: root.querySelector('.ic'), tag: root.querySelector('.tag'), kbd: root.querySelector('kbd'), label: root.querySelector('.label'), sub: root.querySelector('.sub') };
    this.$.ic.style.display = 'contents';
    this._icon = '';
  }

  update() {
    const label = this.getAttribute('label') || '';
    const sub = this.getAttribute('sub') || '';
    const icon = this.getAttribute('icon') || '';
    const hot = this.getAttribute('hotkey') || '';
    const tally = this.getAttribute('tally') || '';
    // a zero-width space at each camelCase join lets "DeepFilterNet" wrap as "DeepFilter / Net"
    this.$.label.textContent = this.bool('fit') ? label.replace(/([a-z])(?=[A-Z])/g, '$1\u200b') : label;
    this.$.sub.textContent = sub;
    this.$.sub.hidden = !sub;
    if (icon !== this._icon) { this._icon = icon; this.$.ic.innerHTML = icon ? iconHTML(icon) : ''; }
    this.$.kbd.textContent = hot;
    this.$.kbd.hidden = !hot;
    this.$.tag.textContent = tally === 'program' ? 'PGM' : tally === 'preview' ? 'PVW' : '';
    this.btn.disabled = this.bool('disabled');
    this.btn.setAttribute('aria-pressed', String(this.hasAttribute('active')));
    const name = [label, sub].filter(Boolean).join(', ');
    this.btn.setAttribute('aria-label', name + (tally === 'program' && this.hasAttribute('active') ? ', on air' : '') + (hot ? ` (key ${hot})` : ''));
  }
}

define('ss-pad', SSPad);
