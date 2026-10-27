// pages/hotkeys.js — GLOBAL HOTKEYS AND MIDI, mounted by the Deck page under the keys.
//
//   mountHotkeys(el, ctx)  ->  cleanup
//
// Bindings tie a keyboard shortcut or a MIDI control to any Console action (the same options as a Stream Deck key). The
// list shows each binding with its live status (listening, another program has the combo, no MIDI device yet ...); the
// form below adds one: press the keys (or "Learn" a MIDI control), pick the option, add. Everything comes from
// `state.hotkeys` (docs/hotkeys.md) and goes out as `hotkeys.*` actions; the browser only turns a key press into the
// combo text the Console understands.

import { h, renderList } from '../core/util.js';
import { apiUrl, getJson } from '../core/api.js';

const TEMPLATE = `
<ss-card class="hk" heading="Hotkeys and MIDI" icon="command" data-ref="hotkeys">
  <ss-toggle slot="actions" label="Active" tone="green" data-ref="enabled"></ss-toggle>
  <p class="hk-where" data-ref="keysLine" role="status"></p>
  <p class="hk-where" data-ref="midiLine" role="status"></p>
  <ul class="hk-list" data-ref="list" aria-label="Bindings"></ul>
  <p class="hint" data-ref="empty">Nothing is bound yet. Add a shortcut below, or Learn a control on your MIDI device.</p>
  <form class="hk-form" data-ref="form" autocomplete="off">
    <div class="hk-trigger">
      <label class="dk-field">
        <span class="eyebrow">Trigger</span>
        <select class="text-input" data-ref="kind" aria-label="Trigger type">
          <option value="key">Keyboard shortcut</option>
          <option value="note">MIDI note</option>
          <option value="cc">MIDI control (button or fader)</option>
          <option value="program">MIDI program change</option>
        </select>
      </label>
      <label class="dk-field" data-ref="comboField">
        <span class="eyebrow">Keys</span>
        <input class="text-input mono" data-ref="combo" type="text" spellcheck="false" placeholder="Click here, then press the keys" aria-label="Shortcut keys">
      </label>
      <div class="hk-midi" data-ref="midiFields" hidden>
        <label class="dk-field"><span class="eyebrow">Channel</span>
          <input class="text-input" data-ref="channel" type="number" min="0" max="16" value="0" aria-label="MIDI channel (0 for any)"></label>
        <label class="dk-field"><span class="eyebrow">Number</span>
          <input class="text-input" data-ref="number" type="number" min="0" max="127" value="60" aria-label="MIDI number"></label>
        <ss-button size="sm" icon="record" data-ref="learn">Learn</ss-button>
        <span class="hint" data-ref="learnNote" role="status"></span>
      </div>
    </div>
    <label class="dk-field">
      <span class="eyebrow">Does</span>
      <select class="text-input" data-ref="option" aria-label="What the trigger does"></select>
    </label>
    <div class="hk-scale" data-ref="scaleFields" hidden>
      <label class="dk-field"><span class="eyebrow">Sets (parameter)</span>
        <input class="text-input mono" data-ref="scaleParam" type="text" spellcheck="false" placeholder="level (leave empty for a button)" aria-label="Parameter the control sets"></label>
      <label class="dk-field"><span class="eyebrow">From</span>
        <input class="text-input" data-ref="scaleMin" type="number" value="0" aria-label="Value at the bottom"></label>
      <label class="dk-field"><span class="eyebrow">To</span>
        <input class="text-input" data-ref="scaleMax" type="number" value="100" aria-label="Value at the top"></label>
    </div>
    <label class="dk-field">
      <span class="eyebrow">Name (optional)</span>
      <input class="text-input" data-ref="label" type="text" maxlength="60" aria-label="Binding name">
    </label>
    <div class="dk-row">
      <ss-button size="sm" variant="primary" icon="plus" data-ref="add">Add</ss-button>
      <p class="hint" data-ref="formNote" role="status"></p>
    </div>
  </form>
  <ol class="hk-events" data-ref="events" aria-label="What hotkeys did"></ol>
  <div class="dk-row">
    <a class="hk-link" data-ref="hkExportLink" href="api/hotkeys/bindings.json" download="streamstack-hotkeys.json">Export</a>
    <label class="hk-link" data-ref="hkImportLabel">Import<input type="file" accept="application/json,.json" data-ref="hkImportFile" hidden></label>
  </div>
</ss-card>`;

const CODE_KEYS = {
  Space: 'space', Enter: 'return', NumpadEnter: 'return', Escape: 'escape', Tab: 'tab', Backspace: 'backspace',
  Insert: 'insert', Delete: 'delete', Home: 'home', End: 'end', PageUp: 'pageup', PageDown: 'pagedown',
  ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right', Pause: 'pause', PrintScreen: 'print',
  ScrollLock: 'scrolllock', Minus: 'minus', Equal: 'equal', Comma: 'comma', Period: 'period', Slash: 'slash',
  Semicolon: 'semicolon', Quote: 'apostrophe', BracketLeft: 'bracketleft', BracketRight: 'bracketright',
  Backslash: 'backslash', Backquote: 'grave',
  AudioVolumeMute: 'xf86audiomute', MediaPlayPause: 'xf86audioplay', MediaTrackNext: 'xf86audionext',
  MediaTrackPrevious: 'xf86audioprev', MediaStop: 'xf86audiostop',
};

/** The key name of a keydown (physical key, so Shift+1 is "shift+1"), or null for a lone modifier. */
export function keyName(e) {
  const code = e.code || '';
  if (/^Key[A-Z]$/.test(code)) return code.slice(3).toLowerCase();
  if (/^Digit[0-9]$/.test(code)) return code.slice(5);
  if (/^F([1-9]|1[0-9]|2[0-4])$/.test(code)) return code.toLowerCase();
  if (/^Numpad[0-9]$/.test(code)) return `kp_${code.slice(6)}`;
  return CODE_KEYS[code] ?? null;
}

/** The combo text of a keydown ("ctrl+alt+m"), or null while only modifiers are down. */
export function comboOf(e) {
  const key = keyName(e);
  if (!key) return null;
  const mods = [e.ctrlKey && 'ctrl', e.altKey && 'alt', e.shiftKey && 'shift', e.metaKey && 'super'].filter(Boolean);
  return [...mods, key].join('+');
}

const STATUS_LABEL = {
  ok: 'Listening', pending: 'Waiting', conflict: 'In use elsewhere', unmapped: 'No such key', refused: 'Refused',
  unavailable: 'Unavailable', waiting: 'No device', off: 'Off',
};

export function mountHotkeys(el, ctx) {
  const { store, act, toast } = ctx;
  el.insertAdjacentHTML('beforeend', TEMPLATE);
  const root = el.querySelector('[data-ref="hotkeys"]');
  const R = {};
  root.querySelectorAll('[data-ref]').forEach((n) => { R[n.dataset.ref] = n; });
  R.hotkeys = root;
  const off = [];
  const ui = { palette: [], paletteRev: null, optionId: null, learnSeen: null };
  const state = () => store.state.hotkeys;
  const named = (b) => {
    const entry = ui.palette.find((p) => p.action === b.action && JSON.stringify(p.params) === JSON.stringify(b.params));
    return b.label || entry?.label || b.action;
  };

  // ---------- the add form ----------
  const isMidi = () => R.kind.value !== 'key';
  const showKind = () => {
    R.comboField.hidden = isMidi();
    R.midiFields.hidden = !isMidi();
    R.scaleFields.hidden = R.kind.value !== 'cc';
    R.number.max = '127';
  };
  R.kind.addEventListener('change', showKind);
  R.combo.addEventListener('keydown', (e) => {
    if (e.key === 'Tab' && !e.ctrlKey && !e.altKey && !e.metaKey) return;
    const combo = comboOf(e);
    if (e.key === 'Enter' && !combo) return;
    e.preventDefault();
    if (combo) R.combo.value = combo;
  });
  R.learn.addEventListener('click', () => {
    ui.learnSeen = null;
    act('hotkeys.learn', { on: true });
  });

  const trigger = () => {
    if (!isMidi()) return { kind: 'key', combo: R.combo.value.trim() };
    return { kind: 'midi', type: R.kind.value, channel: Number(R.channel.value), number: Number(R.number.value) };
  };
  R.form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const entry = ui.palette.find((p) => p.id === R.option.value);
    if (!entry || !entry.action) { R.formNote.textContent = 'Pick what it should do.'; return; }
    // nested: the request's own "action" key is the action name (hotkeys.add), so the binding travels in "binding"
    const params = { trigger: trigger(), action: entry.action, params: entry.params || {} };
    if (R.label.value.trim()) params.label = R.label.value.trim();
    if (R.kind.value === 'cc' && R.scaleParam.value.trim()) {
      params.scale = { param: R.scaleParam.value.trim(), min: Number(R.scaleMin.value), max: Number(R.scaleMax.value) };
    }
    R.formNote.textContent = '';
    // act() raises its own toast when the Console refuses (a combo already in use, a bad name); it answers null then
    const res = await act('hotkeys.add', { binding: params });
    if (res) {
      R.combo.value = '';
      R.label.value = '';
      R.formNote.textContent = '';
    }
  });
  R.add.addEventListener('click', () => R.form.requestSubmit());
  R.enabled.addEventListener('change', (e) => act('hotkeys.enable', { on: e.detail.checked }));
  R.hkImportFile.addEventListener('change', async () => {
    const file = R.hkImportFile.files[0];
    R.hkImportFile.value = '';
    if (!file) return;
    let document;
    try {
      document = JSON.parse(await file.text());
    } catch (err) {
      toast.error(err);
      return;
    }
    await act('hotkeys.import', { document });
  });

  // ---------- the palette ----------
  const loadPalette = async () => {
    try {
      const res = await getJson(apiUrl('api/deck/palette'));
      ui.palette = (res.palette || []).filter((p) => p.action);
      ui.paletteRev = store.state.deck.palette_rev;
      const groups = new Map();
      for (const p of ui.palette) {
        if (!groups.has(p.group)) groups.set(p.group, h('optgroup', { label: p.group }));
        groups.get(p.group).append(h('option', { value: p.id }, p.label));
      }
      const keep = R.option.value;
      R.option.replaceChildren(...groups.values());
      if (keep && ui.palette.some((p) => p.id === keep)) R.option.value = keep;
      render();
    } catch (err) {
      toast.error(err);
    }
  };

  // ---------- render ----------
  const buildRow = (b) => {
    const where = h('span', { class: 'hk-where-t mono' });
    const what = h('span', { class: 'hk-what' });
    const badge = h('span', { class: 'hk-badge', role: 'status' });
    const last = h('span', { class: 'hk-last' });
    const test = h('button', { type: 'button', class: 'hk-mini', 'data-act': 'test' }, 'Test');
    const remove = h('button', { type: 'button', class: 'hk-mini', 'data-act': 'remove', 'aria-label': 'Remove' }, 'Remove');
    test.addEventListener('click', () => act('hotkeys.fire', { id: b.id }));
    remove.addEventListener('click', () => act('hotkeys.remove', { id: b.id }));
    const node = h('li', { class: 'hk-row', 'data-id': b.id }, where, what, badge, last, h('span', { class: 'hk-acts' }, test, remove));
    node._r = { where, what, badge, last };
    return node;
  };
  const render = () => {
    const s = state();
    R.enabled.checked = s.enabled;
    R.keysLine.textContent = s.keys.ok
      ? `Keys: ${s.keys.backend === 'portal' ? 'through the desktop shortcut portal' : s.keys.backend === 'x11' ? 'grabbed on the X11 session, they work in every window' : 'simulated'}.`
      : `Keys: ${s.keys.reason || 'not available here.'}`;
    R.keysLine.dataset.ok = String(s.keys.ok);
    const dev = s.midi.devices.filter((d) => d.status === 'ok').map((d) => d.name);
    const busy = s.midi.devices.filter((d) => d.status !== 'ok');
    R.midiLine.textContent = !s.midi.ok
      ? `MIDI: ${s.midi.reason || 'not available here.'}`
      : dev.length ? `MIDI: ${dev.join(', ')}.${busy.length ? ` ${busy.map((d) => `${d.name} is ${d.status}`).join(', ')}.` : ''}`
        : busy.length ? `MIDI: ${busy.map((d) => `${d.name} is ${d.status}`).join(', ')}.` : 'MIDI: no controller connected.';
    R.midiLine.dataset.ok = String(s.midi.ok && dev.length > 0);
    R.empty.hidden = s.bindings.length > 0;
    renderList(R.list, s.bindings, (b) => b.id, buildRow, (node, b) => {
      const r = node._r;
      r.where.textContent = b.trigger_text;
      r.what.textContent = named(b) + (b.scale ? ` (${b.scale.param} ${b.scale.min} to ${b.scale.max})` : '');
      r.badge.textContent = STATUS_LABEL[b.status] || b.status;
      r.badge.dataset.status = b.status;
      r.badge.title = b.detail;
      r.last.textContent = b.last ? b.last.text : '';
      r.last.dataset.ok = String(!b.last || b.last.ok);
    });
    R.learn.toggleAttribute('disabled', !s.midi.ok);
    R.learnNote.textContent = s.learn.active ? 'Move or press a control on your MIDI device ...' : (s.learn.event ? `Learned ${s.learn.event.text}` : '');
    if (s.learn.event && JSON.stringify(s.learn.event) !== ui.learnSeen) {
      ui.learnSeen = JSON.stringify(s.learn.event);
      R.kind.value = s.learn.event.type;
      R.channel.value = String(s.learn.event.channel);
      R.number.value = String(s.learn.event.number);
      showKind();
    }
    R.events.replaceChildren(...s.events.slice(0, 6).map((e) => h('li', { 'data-level': e.level }, h('time', {}, e.time), ' ', e.text)));
    if (ui.paletteRev !== store.state.deck.palette_rev && !ui.loading) { ui.loading = true; loadPalette().finally(() => { ui.loading = false; }); }
  };

  showKind();
  off.push(store.subscribe((st) => st.hotkeys, render));
  off.push(store.subscribe((st) => st.deck.palette_rev, render));
  return () => { for (const f of off) f(); root.remove(); };
}
