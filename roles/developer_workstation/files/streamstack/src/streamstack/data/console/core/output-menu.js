// core/output-menu.js — THE OUTPUT MENU in the top bar, on every page.
//
//   mountOutputMenu(slot, { store })   adds a button showing where sound plays now (the default output) and a menu
//                                      of every output: choose one to make it the default. Returns a cleanup function.
//
// The list is the Console's own `audio.outputs` state (console/outputs.py): every PipeWire sink, so a TV's HDMI sound,
// a USB interface, a Bluetooth headset and the machine's own speakers all show up, and a plugged or unplugged device
// appears or goes within a second while the menu is open. Volume, mute and the sound test live on the Audio page's
// Output section; the menu links there. The same switch is a command in the palette (Ctrl K) from every page.
//
// Keyboard: Enter, Space or the Down arrow open it on the current output; Up, Down, Home and End move; Enter picks;
// Escape closes and returns to the button.

import { h, renderList } from './util.js';
import { icon } from '../components/icons.js';
import { setDefaultOutput } from './ops.js';
import { setUi } from './ui.js';
import { navigate, currentRoute } from './router.js';
import { registerCommands } from './keys.js';
import { KIND_ICON, outputDetail, ROUTE_NOTE } from './outputs-meta.js';

/** Every output the menu offers: devices first, then routes (a route is not a device, but it can be the default). */
export function menuEntries(o) {
  return [
    ...o.devices.map((d) => ({ name: d.name, label: d.label, detail: outputDetail(d), kind: d.kind, default: d.default, route: false })),
    ...o.routes.map((r) => ({ name: r.name, label: r.label, detail: ROUTE_NOTE, kind: 'route', default: r.default, route: true })),
  ];
}

/** The button's words: the default output's name, or why there is none. */
export function menuSummary(o) {
  const entries = menuEntries(o);
  const current = entries.find((e) => e.default);
  if (current) return { label: current.label, kind: current.kind, tone: o.warning ? 'warn' : 'ok' };
  if (entries.length) return { label: 'No output selected', kind: 'other', tone: 'warn' };
  return { label: 'No output', kind: 'other', tone: 'warn' };
}

export function mountOutputMenu(slot, { store }) {
  const outputs = () => store.state.audio.outputs;
  const btnIcon = h('span', { class: 'out-ic' });
  const btnText = h('span', { class: 'out-name' });
  const btn = h('button', { class: 'out-btn', type: 'button', 'aria-haspopup': 'menu', 'aria-expanded': 'false' }, btnIcon, btnText, icon('chevron'));
  const head = h('p', { class: 'out-head' }, 'Play sound on');
  const warn = h('p', { class: 'out-warn' });
  const list = h('div', { class: 'out-list' });
  const more = h('button', { class: 'out-more', type: 'button', role: 'menuitem' }, icon('sliders'), h('span', {}, 'Volume, mute and sound test'));
  const empty = h('p', { class: 'out-empty' });
  const pop = h('div', { class: 'out-pop', role: 'menu', 'aria-label': 'Sound output', hidden: true }, head, warn, list, empty, more);
  const wrap = h('div', { class: 'out-menu' }, btn, pop);
  slot.prepend(wrap);

  const items = () => [...pop.querySelectorAll('[role^="menuitem"]:not([hidden])')];
  const isOpen = () => !pop.hidden;

  const close = (restoreFocus = false) => {
    if (!isOpen()) return;
    pop.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
    if (restoreFocus) btn.focus();
  };
  const open = () => {
    if (isOpen()) return;
    pop.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    (pop.querySelector('[aria-checked="true"]') || items()[0])?.focus();
  };
  const toggle = () => (isOpen() ? close(true) : open());
  const pick = (name) => {
    close(true);
    if (!menuEntries(outputs()).find((e) => e.name === name)?.default) setDefaultOutput(name);
  };
  const openSettings = () => {
    close();
    setUi('audioSection', 'output');   // an Audio page already on screen listens; one that mounts next reads it
    if (currentRoute().id !== 'audio') navigate('audio');
  };

  btn.addEventListener('click', toggle);
  btn.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' && !isOpen()) { e.preventDefault(); open(); }
  });
  more.addEventListener('click', openSettings);
  list.addEventListener('click', (e) => {
    const item = e.target.closest('[role="menuitemradio"]');
    if (item) pick(item.dataset.key);
  });
  pop.addEventListener('keydown', (e) => {
    const all = items();
    const at = all.indexOf(document.activeElement);
    const go = (i) => { e.preventDefault(); all[(i + all.length) % all.length]?.focus(); };
    if (e.key === 'ArrowDown') go(at + 1);
    else if (e.key === 'ArrowUp') go(at < 0 ? -1 : at - 1);
    else if (e.key === 'Home') go(0);
    else if (e.key === 'End') go(-1);
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(true); }
    else if (e.key === 'Tab') close();
  });
  const outside = (e) => { if (isOpen() && !wrap.contains(e.target)) close(); };
  document.addEventListener('pointerdown', outside, true);

  const entryEl = () => {
    const el = h('button', { class: 'out-item', type: 'button', role: 'menuitemradio', 'aria-checked': 'false' },
      h('span', { class: 'out-kind' }),
      h('span', { class: 'out-id' }, h('b', { class: 'out-label' }), h('small', { class: 'out-detail' })),
      h('span', { class: 'out-check', 'aria-hidden': 'true' }, icon('check')));
    return el;
  };
  const paintEntry = (el, e) => {
    el.dataset.default = String(e.default);
    el.setAttribute('aria-checked', String(e.default));
    el.querySelector('.out-kind').replaceChildren(icon(KIND_ICON[e.kind] || 'volume'));
    el.querySelector('.out-label').textContent = e.label;
    el.querySelector('.out-detail').textContent = e.detail;
  };

  const paint = (o) => {
    const entries = menuEntries(o);
    const sum = menuSummary(o);
    // before the first snapshot there is nothing to say: no output list and no problem yet
    wrap.hidden = !entries.length && !o.problem;
    wrap.dataset.tone = sum.tone;
    btnIcon.replaceChildren(icon(KIND_ICON[sum.kind] || 'volume'));
    btnText.textContent = sum.label;
    btn.setAttribute('aria-label', `Sound output: ${sum.label}. Choose another`);
    btn.title = `Sound plays on ${sum.label}. Click to switch between ${entries.length === 1 ? 'outputs' : `${entries.length} outputs`}.`;
    renderList(list, entries, (e) => e.name, entryEl, paintEntry);
    warn.hidden = !o.warning;
    warn.textContent = o.warning?.message || '';
    empty.hidden = entries.length > 0;
    empty.textContent = o.problem || '';
    if (wrap.hidden) close();
  };
  const off = [store.subscribe((s) => s.audio.outputs, paint)];

  off.push(registerCommands(() => {
    const o = outputs();
    return [
      { id: 'ao:menu', group: 'Audio', title: 'Switch sound output', icon: 'volume', keywords: 'output speakers headphones tv hdmi usb sound switch device sink', run: open },
      ...menuEntries(o).filter((e) => !e.default).map((e) => ({ id: `ao:default:${e.name}`, group: 'Audio', title: `Play sound on ${e.label}${e.route ? ' (route)' : ''}`, icon: KIND_ICON[e.kind] || 'volume', keywords: `output speakers headphones default sink device switch ${e.detail}`, run: () => setDefaultOutput(e.name) })),
    ];
  }));

  return () => {
    off.forEach((fn) => fn());
    document.removeEventListener('pointerdown', outside, true);
    wrap.remove();
  };
}
