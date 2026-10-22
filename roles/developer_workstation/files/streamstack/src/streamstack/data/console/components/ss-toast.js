// <ss-toast> — the toast host. One lives in index.html; everything else calls `toast()` from core/toast.js.
//
// Each toast: {text, level: info|success|warn|error, timeout (ms, 0 = sticky), action: {label, run}, id}.
// A toast with the same `id` replaces the previous one (so a dragged slider cannot flood the screen).
// Errors are announced assertively (role="alert"); the rest politely (role="status").

import { css, define, SSElement } from './base.js';
import { iconHTML } from './icons.js';

const styles = css`
  /* Top right, under the top bar: the bottom of every page holds the pads and outputs people press mid-call. */
  :host { --enter-y: -0.8rem; position: fixed; z-index: 100; top: calc(var(--top-h) + var(--s-3)); right: var(--s-4); display: flex; flex-direction: column; align-items: stretch; gap: var(--s-2); width: min(26rem, calc(100vw - 2rem)); pointer-events: none; }
  /* Narrow: one column with a bottom tab bar, so just above the tabs, centred. */
  @media (max-width: 860px) { :host { --enter-y: 0.8rem; top: auto; right: auto; left: 50%; bottom: calc(var(--tab-h) + var(--s-3)); transform: translateX(-50%); flex-direction: column-reverse; width: min(34rem, calc(100vw - 2rem)); } }
  .t { pointer-events: auto; display: flex; align-items: center; gap: var(--s-3); width: 100%; padding: 0.7rem 0.9rem; border-radius: var(--r-3); background: var(--surface-3); border: 1px solid var(--line-strong); box-shadow: var(--shadow-pop); font-size: var(--fs-sm); font-weight: 560; animation: in var(--dur-2) var(--ease); }
  .t[data-out] { animation: out var(--dur-2) var(--ease) forwards; }
  @keyframes in { from { opacity: 0; transform: translateY(var(--enter-y)) scale(0.97); } }
  @keyframes out { to { opacity: 0; transform: translateY(calc(var(--enter-y) / 2)); } }
  .t .icon { width: 1.2rem; height: 1.2rem; flex: none; }
  .t[data-level="error"] { border-color: var(--red); } .t[data-level="error"] .icon { color: var(--red); }
  .t[data-level="warn"] { border-color: var(--amber); } .t[data-level="warn"] .icon { color: var(--amber); }
  .t[data-level="success"] .icon { color: var(--green); }
  .t[data-level="info"] .icon { color: var(--accent-strong); }
  .msg { flex: 1; min-width: 0; overflow-wrap: anywhere; }
  .act { padding: 0.35rem 0.8rem; border-radius: var(--r-pill); background: var(--accent); color: var(--accent-ink); font-weight: 700; font-size: var(--fs-xs); white-space: nowrap; }
  .x { color: var(--text-3); width: 1.8rem; height: 1.8rem; display: grid; place-items: center; border-radius: 50%; flex: none; }
  .x:hover { color: var(--text); background: var(--surface-2); }
  .x .icon { width: 1rem; height: 1rem; color: inherit; }
`;

const ICONS = { info: 'info', success: 'check', warn: 'alert', error: 'alert' };

export class SSToast extends SSElement {
  static styles = styles;

  build(root) {
    this.items = new Map();
    this._seq = 0;
    this._on = (ev) => this.show(ev.detail);
    window.addEventListener('ss:toast', this._on);
  }

  disconnected() { /* the singleton lives for the page's lifetime */ }

  show({ text, level = 'info', timeout, action, id }) {
    const key = id || `t${++this._seq}`;
    this.dismiss(key, true);
    const ms = timeout === undefined ? (level === 'error' ? 7000 : 4000) : timeout;
    const el = document.createElement('div');
    el.className = 't';
    el.dataset.level = level;
    el.setAttribute('role', level === 'error' ? 'alert' : 'status');
    el.innerHTML = `${iconHTML(ICONS[level] || 'info')}<span class="msg"></span>`;
    el.querySelector('.msg').textContent = text;
    if (action) {
      const b = document.createElement('button');
      b.className = 'act';
      b.type = 'button';
      b.textContent = action.label;
      b.addEventListener('click', () => { this.dismiss(key); action.run(); });
      el.append(b);
    }
    const x = document.createElement('button');
    x.className = 'x';
    x.type = 'button';
    x.setAttribute('aria-label', 'Dismiss');
    x.innerHTML = iconHTML('x');
    x.addEventListener('click', () => this.dismiss(key));
    el.append(x);
    const entry = { el, timer: 0 };
    const arm = () => { if (ms > 0) entry.timer = setTimeout(() => this.dismiss(key), ms); };
    el.addEventListener('pointerenter', () => clearTimeout(entry.timer));
    el.addEventListener('pointerleave', arm);
    arm();
    this.items.set(key, entry);
    this.shadowRoot.append(el);
    while (this.shadowRoot.querySelectorAll('.t').length > 4) this.dismiss(this.items.keys().next().value, true);
  }

  dismiss(key, now = false) {
    const e = this.items.get(key);
    if (!e) return;
    this.items.delete(key);
    clearTimeout(e.timer);
    if (now) { e.el.remove(); return; }
    e.el.dataset.out = '';
    setTimeout(() => e.el.remove(), 200);
  }
}

define('ss-toast', SSToast);
