// pages/deck.js — THE DECK: an Elgato Stream Deck as a control surface for everything in the Console.
//
// A realistic deck (hardware frame, a key grid sized to the connected model, or the model you pick when none is
// connected) with live key pictures: the very PNGs the device shows, drawn by the Console (api/deck/key.png); without
// Pillow the keys are drawn here in the browser from the same views. Two modes:
//   Edit  pick a page, drag any option or preset from the palette onto a key (or click it), drag keys to swap them,
//         shape a key in the inspector (label, icon, action and parameters).
//   Play  the picture follows the page the deck is on; pressing a key does what the real key does.
// The profile, the pages and the keys live in the Console (console/deck.py): every action here is `deck.*`.
// Keys on this page: arrows move between keys, Delete clears the selected key.

import { h, renderList } from '../core/util.js';
import { iconHTML, ICON_NAMES } from '../components/icons.js';
import { apiUrl, getJson } from '../core/api.js';

const TEMPLATE = `
<div class="dk">
  <div class="dk-notice" data-ref="notice" role="status" hidden>
    <span class="dk-notice-ic" aria-hidden="true"></span>
    <p data-ref="noticeText"></p>
    <ss-button size="sm" data-ref="noticeBtn" hidden></ss-button>
  </div>

  <div class="dk-grid">
    <section class="dk-main" aria-label="Deck">
      <div class="dk-bar">
        <ss-status-chip data-ref="chip" state="off" label="No deck" size="lg"></ss-status-chip>
        <div class="seg" role="group" aria-label="Mode" data-ref="mode">
          <button type="button" data-mode="edit" aria-pressed="true">Edit</button>
          <button type="button" data-mode="play" aria-pressed="false">Play</button>
        </div>
        <div class="dk-bar-gap"></div>
        <div class="seg" role="group" aria-label="Deck model" data-ref="models" hidden></div>
      </div>

      <div class="dk-tabs-row" data-ref="tabsRow">
        <div class="dk-tabs" role="tablist" aria-label="Pages" data-ref="tabs"></div>
        <ss-button size="sm" variant="ghost" icon="plus" data-ref="addPage">Page</ss-button>
      </div>
      <div class="dk-play-row" data-ref="playRow" hidden>
        <span class="dk-playing">On the deck: <strong data-ref="playPage"></strong></span>
        <ss-button size="sm" variant="ghost" icon="undo" data-ref="playBack">Back</ss-button>
        <ss-button size="sm" variant="ghost" icon="deck" data-ref="playHome">Home</ss-button>
      </div>

      <div class="dk-stage">
        <div class="dk-body" data-ref="body">
          <div class="dk-keys" role="grid" aria-label="Keys" data-ref="keys"></div>
          <div class="dk-brand" aria-hidden="true">StreamStack</div>
        </div>
        <div class="dk-trash" data-ref="trash" hidden>
          <span aria-hidden="true" data-ref="trashIcon"></span> Drop here to clear the key
        </div>
      </div>

      <div class="dk-pagebar" data-ref="pageBar">
        <label class="dk-field">
          <span class="eyebrow">Page name</span>
          <input class="text-input" data-ref="pageName" type="text" maxlength="24" aria-label="Page name">
        </label>
        <div class="dk-pagebtns">
          <ss-button size="sm" variant="ghost" icon="chevron" data-ref="pageLeft" class="flip" title="Move the page earlier"><span class="sr-only">Move the page earlier</span></ss-button>
          <ss-button size="sm" variant="ghost" icon="chevron" data-ref="pageRight" title="Move the page later"><span class="sr-only">Move the page later</span></ss-button>
          <ss-button size="sm" variant="ghost" icon="trash" data-ref="pageDelete">Delete page</ss-button>
        </div>
      </div>

      <ss-card class="dk-settings" heading="Deck" icon="sliders">
        <div class="dk-set">
          <ss-fader data-ref="brightness" orientation="horizontal" label="Brightness" unit="%" min="5" max="100" step="5" default="70" ticks="5,25,50,75,100"></ss-fader>
          <div class="dk-field">
            <span class="eyebrow">Sleep after</span>
            <div class="seg" role="group" aria-label="Sleep after" data-ref="sleep"></div>
          </div>
        </div>
        <div class="dk-io">
          <ss-button size="sm" icon="external" data-ref="export">Export profile</ss-button>
          <ss-button size="sm" icon="plus" data-ref="importBtn">Import profile</ss-button>
          <input type="file" accept="application/json,.json" data-ref="importFile" hidden>
          <ss-button size="sm" variant="danger" icon="refresh" hold="900" data-ref="reset" title="Hold to reset the deck to the default profile">Reset</ss-button>
          <p class="hint" data-ref="ioNote" role="status"></p>
        </div>
      </ss-card>
    </section>

    <aside class="dk-side">
      <ss-card class="dk-inspector" heading="Key" icon="sliders" data-ref="inspector">
        <p class="dk-hint" data-ref="inspHint">Select a key to shape it, or drag something onto it.</p>
        <div class="dk-insp" data-ref="inspBody" hidden>
          <div class="dk-insp-head">
            <span class="dk-insp-ic" data-ref="inspIcon" aria-hidden="true"></span>
            <div class="dk-insp-t"><strong data-ref="inspTitle"></strong><code data-ref="inspAction"></code></div>
          </div>
          <label class="dk-field">
            <span class="eyebrow">Label</span>
            <input class="text-input" data-ref="label" type="text" maxlength="24" aria-label="Key label">
          </label>
          <div class="dk-field">
            <span class="eyebrow">Icon</span>
            <div class="dk-icons" data-ref="icons" role="listbox" aria-label="Key icon"></div>
          </div>
          <details class="dk-adv" data-ref="adv">
            <summary>Action and parameters</summary>
            <label class="dk-field">
              <span class="eyebrow">Action</span>
              <input class="text-input mono" data-ref="actionName" type="text" maxlength="40" spellcheck="false" placeholder="video.scene" aria-label="Console action">
            </label>
            <label class="dk-field">
              <span class="eyebrow">Parameters (JSON)</span>
              <textarea class="text-input mono" data-ref="actionParams" rows="3" spellcheck="false" aria-label="Action parameters as JSON"></textarea>
            </label>
            <div class="dk-row">
              <ss-button size="sm" variant="primary" icon="check" data-ref="applyAction">Apply</ss-button>
              <p class="hint" data-ref="advNote" role="status"></p>
            </div>
          </details>
          <div class="dk-row">
            <ss-button size="sm" variant="ghost" icon="trash" data-ref="clearKey">Clear key</ss-button>
          </div>
        </div>
      </ss-card>

      <ss-card class="dk-palette" heading="Options" icon="command" data-ref="paletteCard">
        <div class="dk-search">
          <span aria-hidden="true" data-ref="searchIcon"></span>
          <input class="text-input" data-ref="search" type="search" placeholder="Find an option, scene or preset" aria-label="Find an option">
        </div>
        <div class="dk-pal" data-ref="palette" role="group" aria-label="Options to put on a key"></div>
      </ss-card>
    </aside>
  </div>
</div>`;

const SLEEP = [[0, 'Never'], [5, '5 min'], [15, '15 min'], [30, '30 min'], [60, '1 hour']];
const TONE_CLASS = new Set(['red', 'amber', 'green', 'accent']);

/** The DOM face of a key (used when the Console cannot draw PNGs, and while one loads). */
function paintFace(face, view) {
  face.dataset.lit = view.lit ? 'on' : 'off';
  face.dataset.tone = view.lit && TONE_CLASS.has(view.tone) ? view.tone : '';
  face.dataset.dim = view.dim ? 'on' : 'off';
  face.innerHTML = view.empty ? '' : `<span class="dk-f-ic">${iconHTML(view.icon || 'command')}</span><span class="dk-f-l"></span>${view.value ? '<span class="dk-f-v"></span>' : ''}`;
  if (!view.empty) {
    face.querySelector('.dk-f-l').textContent = view.label || '';
    const v = face.querySelector('.dk-f-v');
    if (v) v.textContent = view.value;
  }
}

export async function mount(host, ctx) {
  const { store, act, actStream, flushStream, toast, registerCommands } = ctx;
  await ctx.ensureCss(new URL('./common.css', import.meta.url).href);
  await ctx.ensureCss(new URL('./deck.css', import.meta.url).href);
  document.title = 'Deck · StreamStack';
  host.innerHTML = TEMPLATE;
  const R = {};
  host.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  const off = [];
  const deck = () => store.state.deck;

  // Local UI state: what this window is looking at (never sent anywhere).
  const ui = { mode: 'edit', page: null, selected: null, search: '', adopt: null, focusName: false, palette: [], paletteRev: null, dragging: null };

  R.searchIcon.innerHTML = iconHTML('search');
  R.trashIcon.innerHTML = iconHTML('trash');
  for (const [m, label] of SLEEP) {
    R.sleep.append(h('button', { type: 'button', dataset: { minutes: m }, 'aria-pressed': 'false' }, label));
  }

  // ---------- helpers ----------
  const profile = () => deck().profile;
  const pages = () => profile().pages;
  const editPage = () => pages().find((p) => p.id === ui.page) || pages().find((p) => p.id === profile().home) || pages()[0];
  const shown = () => (ui.mode === 'play'
    ? { rows: deck().surface.rows, cols: deck().surface.cols, id: null, keys: deck().surface.keys }
    : (() => { const p = editPage(); return { rows: profile().layout.rows, cols: profile().layout.cols, id: p ? p.id : null, keys: p ? p.keys : [] }; })());
  const selectedView = () => (ui.selected === null ? null : shown().keys[ui.selected] || null);
  const setNote = (el, text, state = '') => { el.textContent = text; if (state) el.dataset.state = state; else delete el.dataset.state; };

  // ---------- keys ----------
  const keyImage = (view, index, pageId) => apiUrl('api/deck/key.png', { page: pageId || undefined, key: index, size: 120, rev: view.rev });

  const createCap = () => {
    const face = h('span', { class: 'dk-face', 'aria-hidden': 'true' });
    const img = h('img', { class: 'dk-img', alt: '', draggable: 'false', hidden: true });
    const cap = h('button', { type: 'button', class: 'dk-cap', role: 'gridcell', draggable: 'true' }, img, face);
    cap.refs = { face, img };
    cap.addEventListener('click', () => onCap(Number(cap.dataset.key)));
    cap.addEventListener('dragstart', (e) => {
      const i = Number(cap.dataset.key);
      const v = shown().keys[i];
      if (ui.mode !== 'edit' || !v || v.empty) { e.preventDefault(); return; }
      ui.dragging = { kind: 'key', page: shown().id, index: i };
      e.dataTransfer.setData('application/x-deck-key', `${shown().id}:${i}`);
      e.dataTransfer.effectAllowed = 'move';
      document.body.dataset.dragging = 'key';
      R.trash.hidden = false;
    });
    cap.addEventListener('dragend', endDrag);
    cap.addEventListener('dragover', (e) => {
      if (ui.mode !== 'edit' || !ui.dragging) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = ui.dragging.kind === 'key' ? 'move' : 'copy';
      cap.dataset.over = 'on';
    });
    cap.addEventListener('dragleave', () => { delete cap.dataset.over; });
    cap.addEventListener('drop', (e) => {
      e.preventDefault();
      delete cap.dataset.over;
      dropOn(Number(cap.dataset.key));
    });
    return cap;
  };

  const updateCap = (cap, view) => {
    const i = Number(cap.dataset.key);
    const { face, img } = cap.refs;
    cap.dataset.empty = view.empty ? 'on' : 'off';
    cap.dataset.lit = view.lit ? 'on' : 'off';
    cap.dataset.armed = view.armed ? 'on' : 'off';
    cap.setAttribute('aria-label', view.empty ? `Key ${i + 1}, empty` : `Key ${i + 1}: ${view.label}${view.lit ? ', on' : ''}`);
    if (ui.mode === 'edit') cap.setAttribute('aria-pressed', String(ui.selected === i));
    else cap.removeAttribute('aria-pressed');
    cap.dataset.selected = ui.mode === 'edit' && ui.selected === i ? 'on' : 'off';
    cap.draggable = ui.mode === 'edit' && !view.empty;
    if (deck().render) {
      const src = keyImage(view, i, shown().id);
      if (img.dataset.rev !== `${shown().id}:${view.rev}`) {
        img.dataset.rev = `${shown().id}:${view.rev}`;
        img.src = src;
      }
      img.hidden = false;
      face.hidden = true;
    } else {
      img.hidden = true;
      face.hidden = false;
      paintFace(face, view);
    }
  };

  const renderKeys = () => {
    const { rows, cols, keys } = shown();
    R.body.style.setProperty('--cols', String(cols));
    R.body.style.setProperty('--rows', String(rows));
    R.keys.setAttribute('aria-rowcount', String(rows));
    R.keys.setAttribute('aria-colcount', String(cols));
    R.body.dataset.cols = String(cols);
    renderList(R.keys, keys.map((v, i) => ({ v, i })), (x) => x.i, createCap, (cap, x) => updateCap(cap, x.v));
    R.body.style.setProperty('--dim', String(0.25 + 0.75 * ((profile().settings.brightness ?? 70) / 100)));
  };

  // ---------- selection, assigning, dragging ----------
  const select = (i) => {
    ui.selected = i;
    renderKeys();
    renderInspector();
  };

  function onCap(i) {
    if (ui.mode === 'play') {
      const v = shown().keys[i];
      if (v && !v.empty) act('deck.press', { key: i });
      return;
    }
    select(ui.selected === i ? null : i);
  }

  function endDrag() {
    ui.dragging = null;
    delete document.body.dataset.dragging;
    R.trash.hidden = true;
    R.palette.dataset.over = 'off';
    R.keys.querySelectorAll('[data-over]').forEach((el) => delete el.dataset.over);
  }

  const assign = async (index, entry) => {
    const page = editPage();
    if (!page) return;
    await act('deck.key_set', { page: page.id, key: index, spec: { action: entry.action, params: entry.params || {} } });
    ui.selected = index;
    if (entry.custom) R.adv.open = true;
  };

  function dropOn(index) {
    const drag = ui.dragging;
    endDrag();
    if (!drag || ui.mode !== 'edit') return;
    const page = editPage();
    if (drag.kind === 'palette') assign(index, drag.entry);
    else if (drag.kind === 'key' && drag.index !== index) {
      act('deck.key_move', { page: drag.page, key: drag.index, to_page: page.id, to: index }).then(() => select(index));
    }
  }

  R.trash.addEventListener('dragover', (e) => { if (ui.dragging && ui.dragging.kind === 'key') { e.preventDefault(); R.trash.dataset.over = 'on'; } });
  R.trash.addEventListener('dragleave', () => { delete R.trash.dataset.over; });
  R.trash.addEventListener('drop', (e) => {
    e.preventDefault();
    const drag = ui.dragging;
    delete R.trash.dataset.over;
    endDrag();
    if (drag && drag.kind === 'key') act('deck.key_clear', { page: drag.page, key: drag.index });
  });
  // dragging a key back onto the options list also clears it
  R.palette.addEventListener('dragover', (e) => { if (ui.dragging && ui.dragging.kind === 'key') { e.preventDefault(); R.palette.dataset.over = 'on'; } });
  R.palette.addEventListener('dragleave', () => { R.palette.dataset.over = 'off'; });
  R.palette.addEventListener('drop', (e) => {
    e.preventDefault();
    const drag = ui.dragging;
    endDrag();
    if (drag && drag.kind === 'key') act('deck.key_clear', { page: drag.page, key: drag.index });
  });

  // keyboard: arrows between keys, Delete clears
  R.keys.addEventListener('keydown', (e) => {
    if (ui.mode !== 'edit' || ui.selected === null) return;
    const { rows, cols } = shown();
    const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -cols, ArrowDown: cols }[e.key];
    if (step !== undefined) {
      const next = ui.selected + step;
      if (next >= 0 && next < rows * cols && !(Math.abs(step) === 1 && Math.floor(next / cols) !== Math.floor(ui.selected / cols))) {
        e.preventDefault();
        select(next);
        R.keys.querySelector(`[data-key="${next}"]`)?.focus();
      }
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault();
      const page = editPage();
      if (page) act('deck.key_clear', { page: page.id, key: ui.selected });
    }
  });

  // ---------- palette ----------
  const loadPalette = async () => {
    try {
      const res = await getJson(apiUrl('api/deck/palette'));
      ui.palette = res.palette || [];
      ui.paletteRev = deck().palette_rev;
      renderPalette();
    } catch (err) {
      toast.error(err);
    }
  };

  const createItem = (entry) => {
    const item = h('button', { type: 'button', class: 'dk-item', draggable: 'true' });
    item.addEventListener('click', () => {
      const e = ui.palette.find((x) => x.id === item.dataset.key);
      if (!e || ui.mode !== 'edit') { if (ui.mode !== 'edit') toast('Switch to Edit to put options on keys.'); return; }
      const page = editPage();
      let target = ui.selected;
      if (target === null) target = page.keys.findIndex((k) => k.empty);
      if (target < 0) { toast('This page is full. Clear a key or add a page.'); return; }
      assign(target, e);
    });
    item.addEventListener('dragstart', (e) => {
      const found = ui.palette.find((x) => x.id === item.dataset.key);
      if (!found || ui.mode !== 'edit') { e.preventDefault(); return; }
      ui.dragging = { kind: 'palette', entry: found };
      e.dataTransfer.setData('application/x-deck-option', found.id);
      e.dataTransfer.setData('text/plain', found.label);
      e.dataTransfer.effectAllowed = 'copy';
      document.body.dataset.dragging = 'option';
    });
    item.addEventListener('dragend', endDrag);
    return item;
  };

  const updateItem = (item, entry) => {
    item.innerHTML = `<span class="dk-item-ic">${iconHTML(entry.icon || 'command')}</span><span class="dk-item-t"></span>`;
    item.querySelector('.dk-item-t').textContent = entry.label;
    item.title = entry.hint || entry.action;
    item.setAttribute('aria-label', `${entry.label}${entry.hint ? `: ${entry.hint}` : ''}`);
    item.dataset.group = entry.group;
  };

  const renderPalette = () => {
    const q = ui.search.trim().toLowerCase();
    const match = (e) => !q || `${e.label} ${e.group} ${e.action} ${e.hint}`.toLowerCase().includes(q);
    const groups = [];
    for (const e of ui.palette) {
      if (!match(e)) continue;
      let g = groups.find((x) => x.name === e.group);
      if (!g) { g = { name: e.group, items: [] }; groups.push(g); }
      g.items.push(e);
    }
    renderList(R.palette, groups, (g) => g.name, () => h('section', { class: 'dk-group' }, h('h3', { class: 'eyebrow' }), h('div', { class: 'dk-items' })), (wrap, g) => {
      wrap.querySelector('h3').textContent = g.name;
      renderList(wrap.querySelector('.dk-items'), g.items, (e) => e.id, createItem, updateItem);
    });
    R.palette.toggleAttribute('data-none', !groups.length);
    R.paletteCard.setAttribute('heading', q ? `Options (${groups.reduce((n, g) => n + g.items.length, 0)})` : 'Options');
  };
  R.search.addEventListener('input', () => { ui.search = R.search.value; renderPalette(); });

  // ---------- pages ----------
  const renderTabs = () => {
    const current = editPage();
    renderList(R.tabs, pages(), (p) => p.id, (p) => {
      const tab = h('button', { type: 'button', class: 'dk-tab', role: 'tab' });
      tab.addEventListener('click', () => { ui.page = tab.dataset.key; ui.selected = null; renderAll(); });
      return tab;
    }, (tab, p) => {
      tab.textContent = p.name;
      tab.setAttribute('aria-selected', String(current && current.id === p.id));
      tab.title = p.id === profile().home ? `${p.name} (first page of the deck)` : p.name;
    });
    if (current && document.activeElement !== R.pageName) R.pageName.value = current.name;
    R.pageDelete.toggleAttribute('disabled', pages().length < 2);
    const at = pages().findIndex((p) => current && p.id === current.id);
    R.pageLeft.toggleAttribute('disabled', at <= 0);
    R.pageRight.toggleAttribute('disabled', at < 0 || at >= pages().length - 1);
    R.addPage.toggleAttribute('disabled', pages().length >= 24);
  };

  R.addPage.addEventListener('click', async () => {
    // The new page arrives with the next state push: remember what exists now and adopt whatever is new (see renderAll).
    ui.adopt = new Set(pages().map((p) => p.id));
    if (!(await act('deck.page_add', { name: `Page ${pages().length + 1}` }))) ui.adopt = null;
  });
  const renamePage = () => {
    const p = editPage();
    const name = R.pageName.value.trim();
    if (p && name && name !== p.name) act('deck.page_rename', { page: p.id, name });
  };
  R.pageName.addEventListener('change', renamePage);
  R.pageName.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); renamePage(); R.pageName.blur(); } });
  const movePage = (delta) => {
    const p = editPage();
    const at = pages().findIndex((x) => p && x.id === p.id);
    if (at >= 0) act('deck.page_move', { page: p.id, to: at + delta });
  };
  R.pageLeft.addEventListener('click', () => movePage(-1));
  R.pageRight.addEventListener('click', () => movePage(1));
  R.pageDelete.addEventListener('click', async () => {
    const p = editPage();
    if (!p) return;
    ui.page = null;
    ui.selected = null;
    await act('deck.page_delete', { page: p.id });
  });

  // ---------- inspector ----------
  const renderIcons = (current, fallback) => {
    renderList(R.icons, ['', ...ICON_NAMES], (n) => n || 'auto', (n) => {
      const b = h('button', { type: 'button', class: 'dk-ico', role: 'option' });
      b.addEventListener('click', () => setKey({ icon: b.dataset.name }));
      return b;
    }, (b, n) => {
      b.dataset.name = n;
      b.innerHTML = n ? iconHTML(n) : '<span>Auto</span>';
      b.title = n || `Automatic (${fallback})`;
      b.setAttribute('aria-label', n || 'Automatic icon');
      b.setAttribute('aria-selected', String((current || '') === n));
    });
  };

  const rawOf = (view) => view.raw || { action: '', params: {} };

  function setKey(patch) {
    const page = editPage();
    const view = selectedView();
    if (!page || !view || view.empty) return;
    const raw = { ...rawOf(view), ...patch };
    for (const k of ['label', 'icon']) if (!raw[k]) delete raw[k];
    act('deck.key_set', { page: page.id, key: ui.selected, spec: raw });
  }

  const renderInspector = () => {
    const view = ui.mode === 'edit' ? selectedView() : null;
    const has = !!view && !view.empty;
    R.inspHint.hidden = has;
    R.inspBody.hidden = !has;
    R.inspector.setAttribute('heading', has ? `Key ${ui.selected + 1}` : 'Key');
    if (view && view.empty) R.inspHint.textContent = `Key ${ui.selected + 1} is empty. Drag an option onto it, or click one in the list.`;
    else R.inspHint.textContent = ui.mode === 'play' ? 'Play mode: press the keys. Switch to Edit to change them.' : 'Select a key to shape it, or drag something onto it.';
    if (!has) return;
    const raw = rawOf(view);
    R.inspIcon.innerHTML = iconHTML(view.icon || 'command');
    R.inspTitle.textContent = view.label;
    R.inspAction.textContent = raw.action || 'no action yet';
    if (document.activeElement !== R.label) R.label.value = raw.label || '';
    R.label.placeholder = view.label;
    renderIcons(raw.icon || '', view.icon);
    if (document.activeElement !== R.actionName && document.activeElement !== R.actionParams) {
      R.actionName.value = raw.action || '';
      R.actionParams.value = JSON.stringify(raw.params || {}, null, 1);
    }
    if (!raw.action) R.adv.open = true;
  };

  R.label.addEventListener('change', () => setKey({ label: R.label.value.trim() }));
  R.label.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); R.label.blur(); } });
  R.clearKey.addEventListener('click', () => {
    const page = editPage();
    if (page && ui.selected !== null) act('deck.key_clear', { page: page.id, key: ui.selected });
  });
  R.applyAction.addEventListener('click', () => {
    let params;
    try {
      params = R.actionParams.value.trim() ? JSON.parse(R.actionParams.value) : {};
    } catch (err) {
      setNote(R.advNote, `The parameters are not valid JSON: ${err.message}`, 'error');
      return;
    }
    if (params === null || typeof params !== 'object' || Array.isArray(params)) {
      setNote(R.advNote, 'The parameters must be a JSON object, like {"name": "Canon"}.', 'error');
      return;
    }
    setNote(R.advNote, 'Applied.');
    setKey({ action: R.actionName.value.trim(), params });
  });

  // ---------- header: chip, mode, models, banner ----------
  const renderChip = () => {
    const d = deck().device;
    const chip = R.chip;
    if (d.connected) {
      const demo = d.source === 'demo';
      chip.setAttribute('state', demo ? 'info' : 'ok');
      chip.setAttribute('label', d.name);
      chip.setAttribute('value', `${d.cols}×${d.rows} keys · ${demo ? 'simulated' : 'USB'}`);
      chip.setAttribute('icon', demo ? 'deck' : 'check');
    } else if (d.error) {
      chip.setAttribute('state', 'fail');
      chip.setAttribute('label', 'Deck problem');
      chip.setAttribute('value', 'see below');
      chip.setAttribute('icon', 'alert');
    } else {
      chip.setAttribute('state', 'off');
      chip.setAttribute('label', 'No deck connected');
      chip.setAttribute('value', 'editing offline');
      chip.setAttribute('icon', 'deck');
    }
  };

  const renderModels = () => {
    const connected = deck().device.connected;
    R.models.hidden = connected || !deck().models.length;
    if (R.models.hidden) return;
    R.models.innerHTML = '';
    for (const m of deck().models) {
      const b = h('button', { type: 'button', dataset: { model: m.id }, 'aria-pressed': String(profile().model === m.id), title: `${m.name}, ${m.cols}×${m.rows} keys` }, m.id === 'virtual-4x4' ? '4×4' : `${m.name.replace('Stream Deck ', '')} ${m.cols}×${m.rows}`);
      R.models.append(b);
    }
  };
  R.models.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-model]');
    if (b) { ui.page = null; ui.selected = null; act('deck.layout', { model: b.dataset.model }); }
  });

  R.mode.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-mode]');
    if (!b || b.dataset.mode === ui.mode) return;
    ui.mode = b.dataset.mode;
    ui.selected = null;
    renderAll();
  });

  const renderBanner = () => {
    const d = deck();
    let text = '';
    let button = null;
    let state = 'info';
    if (d.device.error && !d.device.connected) {
      text = d.device.error;
      state = 'fail';
    } else if (d.profile.mismatch && d.device.connected) {
      text = `This profile is laid out for ${d.profile.layout.cols}×${d.profile.layout.rows} keys; the connected deck has ${d.device.cols}×${d.device.rows}, so it shows a re-flowed copy with Back and More keys.`;
      button = { label: `Use ${d.device.cols}×${d.device.rows} layout`, run: () => act('deck.layout', { model: d.device.model, rows: d.device.rows, cols: d.device.cols }) };
      state = 'warn';
    } else if (!d.device.connected) {
      text = 'No Stream Deck is connected. Plug one in and run `streamstack deck enable`; until then you can build the profile here and play it with the mouse.';
    } else if (!d.render) {
      text = "Key pictures need Pillow on the Console's machine: pip install 'streamstack[deck]'. The keys here are drawn in the browser.";
      state = 'warn';
    }
    R.notice.hidden = !text;
    R.notice.dataset.state = state;
    R.noticeText.textContent = text;
    R.notice.querySelector('.dk-notice-ic').innerHTML = iconHTML(state === 'info' ? 'info' : 'alert');
    R.noticeBtn.hidden = !button;
    R.noticeBtn.onclick = button ? button.run : null;
    if (button) R.noticeBtn.textContent = button.label;
  };

  // ---------- settings, import / export ----------
  const renderSettings = () => {
    const s = profile().settings;
    if (document.activeElement !== R.brightness) R.brightness.value = s.brightness;
    R.sleep.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(Number(b.dataset.minutes) === s.sleep_minutes)));
  };
  R.brightness.addEventListener('input', (e) => {
    R.body.style.setProperty('--dim', String(0.25 + 0.75 * (e.detail.value / 100)));
    actStream('deck.settings', { brightness: Math.round(e.detail.value) });
  });
  R.brightness.addEventListener('change', () => flushStream('deck.settings'));
  R.sleep.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-minutes]');
    if (b) act('deck.settings', { sleep_minutes: Number(b.dataset.minutes) });
  });

  R.export.addEventListener('click', async () => {
    try {
      const data = await getJson(apiUrl('api/deck/profile.json'));
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
      const a = h('a', { href: url, download: 'streamstack-deck.json' });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      setNote(R.ioNote, 'Exported streamstack-deck.json.');
    } catch (err) {
      setNote(R.ioNote, err.message, 'error');
    }
  });
  R.importBtn.addEventListener('click', () => R.importFile.click());
  R.importFile.addEventListener('change', async () => {
    const file = R.importFile.files[0];
    R.importFile.value = '';
    if (!file) return;
    let data;
    try {
      data = JSON.parse(await file.text());
    } catch (err) {
      setNote(R.ioNote, `${file.name} is not valid JSON.`, 'error');
      return;
    }
    const res = await act('deck.import', { profile: data });
    if (res) { ui.page = null; ui.selected = null; setNote(R.ioNote, `Imported ${file.name}.`); } else setNote(R.ioNote, 'That file is not a deck profile (see the message).', 'error');
  });
  R.reset.addEventListener('click', async () => {
    ui.page = null;
    ui.selected = null;
    if (await act('deck.reset')) setNote(R.ioNote, 'Back to the default profile.');
  });

  // ---------- play mode ----------
  R.playBack.addEventListener('click', () => act('deck.back'));
  R.playHome.addEventListener('click', () => act('deck.home'));

  // ---------- render everything ----------
  const renderAll = () => {
    if (!pages().length) { renderChip(); renderBanner(); return; }
    const added = ui.adopt && pages().find((p) => !ui.adopt.has(p.id));
    if (added) {
      ui.adopt = null;
      ui.page = added.id;
      ui.selected = null;
      ui.focusName = true;
    }
    if (!pages().some((p) => p.id === ui.page)) ui.page = profile().home;
    const total = shown().keys.length;
    if (ui.selected !== null && ui.selected >= total) ui.selected = null;
    R.mode.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === ui.mode)));
    host.dataset.mode = ui.mode;
    R.tabsRow.hidden = ui.mode !== 'edit';
    R.pageBar.hidden = ui.mode !== 'edit';
    R.playRow.hidden = ui.mode !== 'play';
    R.playPage.textContent = deck().surface.page.name;
    R.playBack.toggleAttribute('disabled', !deck().surface.can_back);
    renderChip();
    renderBanner();
    renderModels();
    renderTabs();
    renderKeys();
    renderInspector();
    renderSettings();
    if (ui.paletteRev !== deck().palette_rev) loadPalette();
    if (ui.focusName) {
      ui.focusName = false;
      R.pageName.focus();
      R.pageName.select();
    }
  };

  off.push(store.subscribe((s) => s.deck, renderAll));
  window.addEventListener('dragend', endDrag);
  off.push(() => window.removeEventListener('dragend', endDrag));
  off.push(() => { delete document.body.dataset.dragging; });
  off.push(registerCommands(() => [
    { id: 'Deck:play', group: 'Deck', title: 'Deck: play the keys with the mouse', icon: 'deck', keywords: 'stream deck keys play', run: () => { ui.mode = 'play'; renderAll(); } },
    { id: 'Deck:edit', group: 'Deck', title: 'Deck: edit the keys', icon: 'deck', keywords: 'stream deck keys edit profile', run: () => { ui.mode = 'edit'; renderAll(); } },
  ]));
  return () => off.forEach((fn) => (typeof fn === 'function' ? fn() : undefined));
}
