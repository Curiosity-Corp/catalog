// <ss-palette> — Ctrl+K command palette, and <ss-shortcuts> — the “?” keyboard help.
//
// Both are native <dialog> modals (focus trap, Esc to close, backdrop). One of each lives in index.html;
// app.js wires Ctrl+K / ? to `palette.open()` / `shortcuts.open()`.
//
// The palette lists everything returned by the command providers registered with registerCommands()
// (core/keys.js): pages, scenes, presets, looks, toggles, studio and auto controls, apps.
//   command = { id, title, group, icon?, hint?, keywords?, danger?, run() }
// Typing filters with a forgiving subsequence match (title, group, keywords); ↑/↓ move, Enter runs, Esc closes.
// Methods: open(), close(), toggle().  ss-shortcuts: open(), close().

import { css, define, SSElement } from './base.js';
import { iconHTML } from './icons.js';
import { allCommands, allKeys, prettyKey } from '../core/keys.js';
import { esc } from '../core/util.js';

const dialogStyles = css`
  dialog { padding: 0; border: 0; background: transparent; color: var(--text); max-width: none; max-height: none; width: 100vw; height: 100vh; overflow: visible; }
  dialog::backdrop { background: var(--scrim); backdrop-filter: blur(3px); }
  dialog:not([open]) { display: none; }
  .wrap { position: absolute; left: 50%; top: 12vh; transform: translateX(-50%); width: min(40rem, calc(100vw - 1.5rem)); max-height: 72vh; display: flex; flex-direction: column; background: var(--surface-1); border-radius: var(--r-4); box-shadow: var(--shadow-pop); overflow: hidden; animation: pop var(--dur-2) var(--ease); }
  @keyframes pop { from { opacity: 0; transform: translate(-50%, 0.6rem) scale(0.985); } }
`;

const styles = css`
  ${dialogStyles}
  .search { display: flex; align-items: center; gap: var(--s-3); padding: var(--s-3) var(--s-4); border-bottom: 1px solid var(--line); }
  .search .icon { width: 1.2rem; height: 1.2rem; color: var(--text-3); }
  input { flex: 1; background: none; border: 0; color: var(--text); font-size: var(--fs-lg); padding: var(--s-2) 0; outline: none; min-width: 0; }
  input::placeholder { color: var(--text-3); }
  ul { list-style: none; margin: 0; padding: var(--s-2); overflow: auto; }
  .grp { padding: var(--s-3) var(--s-3) var(--s-1); font-size: var(--fs-2xs); font-weight: 700; letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--text-3); }
  li[role="option"] { display: flex; align-items: center; gap: var(--s-3); padding: 0.6rem var(--s-3); border-radius: var(--r-2); cursor: pointer; color: var(--text-2); }
  li[role="option"] .icon { width: 1.2rem; height: 1.2rem; color: var(--text-3); flex: none; }
  li[role="option"] .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); font-weight: 560; }
  li[aria-selected="true"] { background: var(--accent-soft); }
  li[aria-selected="true"] .icon { color: var(--accent-strong); }
  li.danger .t { color: var(--red); }
  kbd { font-family: var(--font-mono); font-size: var(--fs-xs); padding: 0.1rem 0.45rem; border-radius: 0.3rem; background: var(--surface-3); border: 1px solid var(--line-strong); color: var(--text-2); }
  .none { padding: var(--s-5); text-align: center; color: var(--text-3); font-size: var(--fs-sm); }
  .foot { display: flex; gap: var(--s-4); padding: var(--s-2) var(--s-4); border-top: 1px solid var(--line); font-size: var(--fs-xs); color: var(--text-3); }
`;

/** Forgiving subsequence match; lower score = better. null = no match. */
function score(query, text) {
  if (!query) return 0;
  const t = text.toLowerCase();
  const q = query.toLowerCase();
  const idx = t.indexOf(q);
  if (idx >= 0) return idx === 0 ? 0 : 1 + idx / 100;
  let ti = 0; let gaps = 0;
  for (const ch of q) {
    const f = t.indexOf(ch, ti);
    if (f < 0) return null;
    gaps += f - ti;
    ti = f + 1;
  }
  return 5 + gaps / 10;
}

export class SSPalette extends SSElement {
  static styles = styles;

  build(root) {
    root.innerHTML = `<dialog aria-label="Command palette"><div class="wrap">
      <div class="search">${iconHTML('search')}<input type="text" role="combobox" aria-expanded="true" aria-controls="list" aria-autocomplete="list" placeholder="Type a command, scene, preset or page…" spellcheck="false" autocomplete="off"></div>
      <ul id="list" role="listbox" aria-label="Commands"></ul>
      <div class="foot"><span><kbd>↑</kbd> <kbd>↓</kbd> move</span><span><kbd>Enter</kbd> run</span><span><kbd>Esc</kbd> close</span></div></div></dialog>`;
    this.$ = { dlg: root.querySelector('dialog'), input: root.querySelector('input'), list: root.querySelector('ul') };
    this.items = [];
    this.sel = 0;
    this.$.input.addEventListener('input', () => { this.sel = 0; this.render(); });
    this.$.input.addEventListener('keydown', (ev) => this.onKey(ev));
    this.$.dlg.addEventListener('click', (ev) => { if (ev.target === this.$.dlg) this.close(); });
    this.$.dlg.addEventListener('close', () => this.emit('close'));
    this.$.dlg.addEventListener('keydown', (ev) => ev.stopPropagation());
    this.$.list.addEventListener('click', (ev) => {
      const li = ev.target.closest('li[role="option"]');
      if (li) this.run(Number(li.dataset.i));
    });
    this.$.list.addEventListener('pointermove', (ev) => {
      const li = ev.target.closest('li[role="option"]');
      if (li && Number(li.dataset.i) !== this.sel) { this.sel = Number(li.dataset.i); this.mark(); }
    });
  }

  get isOpen() { return this.$.dlg.open; }
  open() {
    if (this.isOpen) return;
    this.all = allCommands();
    this.$.input.value = '';
    this.sel = 0;
    this.render();
    this.$.dlg.showModal();
    this.$.input.focus();
  }
  close() { if (this.isOpen) this.$.dlg.close(); }
  toggle() { if (this.isOpen) this.close(); else this.open(); }

  render() {
    const q = this.$.input.value.trim();
    const scored = [];
    for (const c of this.all) {
      const s = Math.min(...[c.title, `${c.group} ${c.title}`, c.keywords || ''].map((t) => score(q, t)).filter((x) => x !== null), Infinity);
      if (s !== Infinity) scored.push({ c, s });
    }
    if (q) scored.sort((a, b) => a.s - b.s);
    this.items = scored.map((x) => x.c);
    let html = '';
    let lastGroup = null;
    this.items.forEach((c, i) => {
      if (!q && c.group !== lastGroup) { html += `<li class="grp" role="presentation">${esc(c.group)}</li>`; lastGroup = c.group; }
      html += `<li role="option" id="opt${i}" data-i="${i}" class="${c.danger ? 'danger' : ''}" aria-selected="${i === this.sel}">${iconHTML(c.icon || 'chevron')}<span class="t">${esc(c.title)}</span>${c.hint ? `<kbd>${esc(c.hint)}</kbd>` : ''}</li>`;
    });
    this.$.list.innerHTML = html || '<li class="none" role="presentation">No matching command</li>';
    this.mark();
  }

  mark() {
    this.$.list.querySelectorAll('li[role="option"]').forEach((li) => {
      const on = Number(li.dataset.i) === this.sel;
      li.setAttribute('aria-selected', String(on));
      if (on) { li.scrollIntoView({ block: 'nearest' }); this.$.input.setAttribute('aria-activedescendant', li.id); }
    });
  }

  onKey(ev) {
    const n = this.items.length;
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'k') { ev.preventDefault(); this.close(); return; }
    if (ev.key === 'ArrowDown') { ev.preventDefault(); this.sel = (this.sel + 1) % Math.max(1, n); this.mark(); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); this.sel = (this.sel - 1 + n) % Math.max(1, n); this.mark(); }
    else if (ev.key === 'Home') { ev.preventDefault(); this.sel = 0; this.mark(); }
    else if (ev.key === 'End') { ev.preventDefault(); this.sel = Math.max(0, n - 1); this.mark(); }
    else if (ev.key === 'Enter') { ev.preventDefault(); this.run(this.sel); }
  }

  run(i) {
    const c = this.items[i];
    if (!c) return;
    this.close();
    try {
      c.run();
    } catch (err) {
      console.error('command failed', c.id, err);
    }
  }
}

const helpStyles = css`
  ${dialogStyles}
  .wrap { top: 8vh; width: min(46rem, calc(100vw - 1.5rem)); }
  header { display: flex; align-items: center; justify-content: space-between; padding: var(--s-4) var(--s-5); border-bottom: 1px solid var(--line); }
  h2 { margin: 0; font-size: var(--fs-lg); }
  .x { width: 2.2rem; height: 2.2rem; display: grid; place-items: center; border-radius: 50%; color: var(--text-3); }
  .x:hover { background: var(--surface-3); color: var(--text); }
  .x .icon { width: 1.1rem; height: 1.1rem; }
  .cols { padding: var(--s-4) var(--s-5) var(--s-5); overflow: auto; columns: 2 16rem; column-gap: var(--s-6); }
  section { break-inside: avoid; margin-bottom: var(--s-4); }
  h3 { margin: 0 0 var(--s-2); font-size: var(--fs-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--text-3); }
  .row { display: flex; align-items: center; justify-content: space-between; gap: var(--s-3); padding: 0.28rem 0; font-size: var(--fs-sm); color: var(--text-2); }
  kbd { font-family: var(--font-mono); font-size: var(--fs-xs); padding: 0.1rem 0.45rem; border-radius: 0.3rem; background: var(--surface-3); border: 1px solid var(--line-strong); border-bottom-width: 2px; color: var(--text); white-space: nowrap; }
`;

export class SSShortcuts extends SSElement {
  static styles = helpStyles;

  build(root) {
    root.innerHTML = `<dialog aria-label="Keyboard shortcuts"><div class="wrap"><header><h2>Keyboard shortcuts</h2><button class="x" type="button" aria-label="Close">${iconHTML('x')}</button></header><div class="cols"></div></div></dialog>`;
    this.$ = { dlg: root.querySelector('dialog'), cols: root.querySelector('.cols') };
    root.querySelector('.x').addEventListener('click', () => this.close());
    this.$.dlg.addEventListener('click', (ev) => { if (ev.target === this.$.dlg) this.close(); });
    this.$.dlg.addEventListener('keydown', (ev) => ev.stopPropagation());
    this.$.dlg.addEventListener('keydown', (ev) => { if (ev.key === '?') this.close(); });
  }

  get isOpen() { return this.$.dlg.open; }
  open() {
    if (this.isOpen) return;
    const groups = new Map();
    for (const k of allKeys()) {
      if (!k.group || !k.desc) continue;
      if (!groups.has(k.group)) groups.set(k.group, []);
      groups.get(k.group).push(k);
    }
    this.$.cols.innerHTML = [...groups].map(([g, ks]) => `<section><h3>${esc(g)}</h3>${ks.map((k) => `<div class="row"><span>${esc(k.desc)}</span><kbd>${esc(k.label || prettyKey(k.key))}</kbd></div>`).join('')}</section>`).join('');
    this.$.dlg.showModal();
  }
  close() { if (this.isOpen) this.$.dlg.close(); }
}

define('ss-palette', SSPalette);
define('ss-shortcuts', SSShortcuts);
