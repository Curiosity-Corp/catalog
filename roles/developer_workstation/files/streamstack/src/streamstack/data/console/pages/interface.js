// pages/interface.js — THE AUDIO INTERFACE ON THE DESK (Focusrite Scarlett 2i2 or Elgato Wave XLR).
//
// The server detects the interface by USB id and reports a list of widgets (snapshot.interface.controls);
// this page renders whatever it is given, so a new interface needs no change here. Kinds:
//   switch   ss-toggle           choice   segmented buttons (value = index)
//   slider   horizontal ss-fader (sent on release)      action   a button + progress text (Autogain)
// 48V (`danger`) is switched on only by pressing and holding, and the request carries confirm: true.
// The Audio page's software gain stays as the fallback for every interface.
//
// Keys on this page: R rescan.

import { h, renderList } from '../core/util.js';

const TEMPLATE = `
<div class="ifc">
  <section class="ifc-head" data-ref="head" data-state="off" aria-labelledby="ifcTitle">
    <div class="ifc-id">
      <h1 id="ifcTitle" data-ref="title">Looking for your audio interface…</h1>
      <p class="ifc-sub" data-ref="sub" role="status"></p>
    </div>
    <ss-button icon="refresh" data-ref="rescan" title="Look for the interface again (R)">Rescan</ss-button>
  </section>
  <p class="ifc-banner" data-ref="banner" data-state="warn" role="status" hidden></p>
  <div class="ifc-demo" data-ref="demo" hidden>
    <span class="ifc-demo-label">Demo interface</span>
    <div class="seg" role="group" aria-label="Which interface to simulate" data-ref="demoSeg"></div>
  </div>
  <div class="ifc-cols" data-ref="groups"></div>
  <ss-card class="ifc-extra" heading="Not available on this interface" icon="alert" data-ref="unsupportedCard" hidden>
    <ul class="ifc-list" data-ref="unsupported"></ul>
  </ss-card>
  <p class="hint ifc-foot">
    Your Audio page keeps its software gain as the fallback on every interface.
    Hardware changes made here are real: they change the signal for everyone on the call.
  </p>
</div>`;

const MODES = { off: 'Hardware control off', read: 'Read only', write: 'Read and write' };

/** One row of a group card: label + hint on the left, the control on the right. */
function createRow(w, ctx) {
  const row = h('div', { class: 'ifc-row', 'data-kind': w.kind });
  const text = h('div', { class: 'ifc-text' }, h('span', { class: 'ifc-label' }), h('span', { class: 'ifc-hint' }));
  const body = h('div', { class: 'ifc-ctl' });
  row.append(text, body);
  row._parts = { label: text.children[0], hint: text.children[1], body };
  build(row, w, ctx);
  return row;
}

const send = (ctx, w, value, extra = {}) => ctx.act('interface.set', { id: w.id, value, ...extra });

function build(row, w, ctx) {
  const { body } = row._parts;
  if (w.kind === 'switch') {
    const t = h('ss-toggle', { 'data-role': 'toggle' });
    t.addEventListener('change', (e) => {
      const wid = row._w;
      if (wid.danger && e.detail.checked) { t.checked = false; return; }
      send(ctx, wid, e.detail.checked);
    });
    body.append(t);
    if (w.danger) {
      const arm = h('ss-button', { variant: 'danger', icon: 'alert', hold: '900', size: 'sm', 'data-role': 'arm', title: 'Press and hold to switch on' }, 'Hold to switch on');
      arm.addEventListener('click', () => send(ctx, row._w, true, { confirm: true }));
      body.append(arm);
    }
  } else if (w.kind === 'choice') {
    const seg = h('div', { class: 'seg', role: 'group', 'aria-label': w.label, 'data-role': 'seg' });
    w.options.forEach((name, i) => {
      const b = h('button', { type: 'button', 'aria-pressed': 'false', 'data-i': i }, name);
      b.addEventListener('click', () => send(ctx, row._w, i));
      seg.append(b);
    });
    body.append(seg);
  } else if (w.kind === 'slider') {
    const f = h('ss-fader', {
      orientation: 'horizontal', label: '', unit: w.unit, min: w.min, max: w.max, step: w.step, decimals: w.step < 1 ? 1 : 0,
      ticks: 'none', 'tick-labels': 'none', 'data-role': 'fader',
    });
    f.addEventListener('pointerdown', () => { f._drag = true; });
    window.addEventListener('pointerup', () => { f._drag = false; });
    f.addEventListener('change', (e) => send(ctx, row._w, e.detail.value));
    body.append(f);
  } else if (w.kind === 'action') {
    const b = h('ss-button', { variant: 'primary', icon: 'auto', 'data-role': 'go' }, w.label);
    b.addEventListener('click', () => send(ctx, row._w, !row._w.value));
    body.append(b, h('span', { class: 'ifc-text-status', 'data-role': 'status', role: 'status' }));
  } else {
    body.append(h('span', { class: 'ifc-value', 'data-role': 'value' }));
  }
}

function update(row, w) {
  row._w = w;
  const { label, hint, body } = row._parts;
  row.dataset.state = w.state || '';
  label.textContent = w.label;
  hint.textContent = w.readonly || w.hint || '';
  hint.dataset.locked = w.readonly ? 'true' : 'false';
  const q = (role) => body.querySelector(`[data-role="${role}"]`);
  const locked = !!w.readonly;
  if (w.kind === 'switch') {
    const t = q('toggle');
    t.checked = !!w.value;
    t.toggleAttribute('disabled', locked);
    t.setAttribute('label', w.label);
    if (w.danger) t.setAttribute('tone', 'red');
    t.hidden = w.danger && !w.value; // 48V is switched on only through the hold button
    const arm = q('arm');
    if (arm) { arm.hidden = !!w.value; arm.toggleAttribute('disabled', locked); }
  } else if (w.kind === 'choice') {
    q('seg').querySelectorAll('button').forEach((b) => {
      b.setAttribute('aria-pressed', String(Number(b.dataset.i) === w.value));
      b.disabled = locked;
    });
  } else if (w.kind === 'slider') {
    const f = q('fader');
    f.setAttribute('label', w.label);
    f.toggleAttribute('disabled', locked);
    if (!f._drag && f.value !== w.value) f.value = w.value;
  } else if (w.kind === 'action') {
    const b = q('go');
    b.textContent = w.value ? 'Stop' : w.label;
    b.setAttribute('variant', w.value ? 'danger' : 'primary');
    b.toggleAttribute('busy', !!w.value);
    b.toggleAttribute('disabled', locked && !w.value);
    q('status').textContent = w.text || '';
    q('status').dataset.state = w.state || '';
  } else {
    q('value').textContent = w.value;
  }
}

function createGroup(name) {
  const card = h('ss-card', { heading: name, icon: 'sliders', class: 'ifc-group' });
  const list = h('div', { class: 'ifc-rows' });
  card.append(list);
  card._list = list;
  return card;
}

export async function mount(host, ctx) {
  const { store, act, registerKeys } = ctx;
  await ctx.ensureCss(new URL('./common.css', import.meta.url).href);
  await ctx.ensureCss(new URL('./interface.css', import.meta.url).href);
  document.title = 'Interface · StreamStack';
  host.innerHTML = TEMPLATE;
  const R = {};
  host.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  const off = [];

  R.rescan.addEventListener('click', () => act('interface.rescan', {}, { quiet: true }));
  off.push(registerKeys([{ key: 'r', desc: 'Rescan the audio interface', group: 'Interface', run: () => act('interface.rescan', {}, { quiet: true }) }]));

  off.push(store.subscribe((s) => s.interface, (ifc) => {
    const present = ifc.present;
    R.head.dataset.state = present ? (ifc.error ? 'warn' : 'ok') : 'off';
    R.title.textContent = present ? ifc.label : (ifc.checked ? 'No supported audio interface found' : 'Looking for your audio interface…');
    const bits = [];
    if (present) {
      bits.push(`USB ${ifc.usb}`);
      if (ifc.card !== null) bits.push(`sound card ${ifc.card}`);
      if (ifc.mode) bits.push(MODES[ifc.mode] || ifc.mode);
    } else if (ifc.devices.length) {
      bits.push(`Sound devices: ${ifc.devices.map((d) => d.name || d.usb).join(', ')}`);
    } else if (ifc.checked) {
      bits.push('Plug in a Focusrite Scarlett 2i2 or an Elgato Wave XLR. It appears here by itself.');
    }
    R.sub.textContent = bits.join(' · ');

    const message = ifc.error || ifc.notice || '';
    R.banner.hidden = !message;
    R.banner.textContent = message;
    R.banner.dataset.state = ifc.error ? 'error' : 'warn';

    R.demo.hidden = !ifc.demo_profiles.length;
    renderList(R.demoSeg, ifc.demo_profiles, (p) => p.id,
      (p) => {
        const b = h('button', { type: 'button', 'aria-pressed': 'false', 'data-demo': p.id }, p.label);
        b.addEventListener('click', () => act('interface.demo_profile', { profile: p.id }, { quiet: true }));
        return b;
      },
      (b, p) => { b.setAttribute('aria-pressed', String((p.id === 'none' && !present) || p.id === ifc.profile)); });

    const groups = [];
    for (const w of ifc.controls) {
      let g = groups.find((x) => x.name === w.group);
      if (!g) { g = { name: w.group, items: [] }; groups.push(g); }
      g.items.push(w);
    }
    if (ifc.info.length) groups.push({ name: 'Device', items: ifc.info.filter((w) => !ifc.controls.some((c) => c.id === w.id)) });
    const merged = [];
    for (const g of groups) {
      const into = merged.find((x) => x.name === g.name);
      if (into) into.items.push(...g.items); else merged.push({ name: g.name, items: [...g.items] });
    }
    renderList(R.groups, merged, (g) => g.name, (g) => createGroup(g.name), (card, g) => {
      // keyed by kind too: the same id is a switch on one interface and a choice on another (Air, Gen 3 vs Gen 4)
      renderList(card._list, g.items, (w) => `${w.id}:${w.kind}`, (w) => createRow(w, ctx), update);
    });

    R.unsupportedCard.hidden = !ifc.unsupported.length;
    renderList(R.unsupported, ifc.unsupported, (u) => u.label,
      (u) => h('li', {}, h('b', {}, u.label), ' ', h('span', {})),
      (li, u) => { li.lastChild.textContent = u.reason; });
  }));

  return () => off.forEach((fn) => fn && fn());
}
