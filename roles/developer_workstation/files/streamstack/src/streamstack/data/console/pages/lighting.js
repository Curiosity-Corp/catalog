// pages/lighting.js — THE LIGHTING DESK for Logitech Litra lights.
//
// Left: the "All lights" master strip. Right: one card per light with a big power pad, a brightness fader and a
// colour-temperature fader whose track is a warm to cool gradient. Below: presets as pads and "save your own".
// Keys on this page: none of its own; L (every light on / off) is global. Alt+6 comes here.
//
// Data flow: one static DOM for the master and presets, keyed cards for the lights (they come and go with the USB
// bus), fine-grained store subscriptions. Faders stream `lighting.brightness` / `lighting.temperature`; the store
// gives optimistic values back at once (core/actions.js), the server reads the real values back later.

import { h, renderList } from '../core/util.js';
import { iconHTML } from '../components/icons.js';
import { toggleLights, selectLightPreset } from '../core/ops.js';

const WARM_COOL = 'linear-gradient(to top, #ff9d4d, #ffe2bd 30%, #fff4e5 55%, #dbe9ff 80%, #b9d3ff)';

const TEMPLATE = `
<div class="li">
  <div class="li-notice" role="alert" data-ref="notice" hidden>
    <strong data-ref="noticeTitle"></strong>
    <p data-ref="noticeText"></p>
    <p class="hint">Setup steps are in the Lighting guide (docs/lighting.md).</p>
  </div>

  <div class="li-grid">
    <section class="li-master" role="region" aria-label="All lights" data-ref="masterRegion">
      <ss-card heading="All lights" icon="lights" data-ref="masterCard">
        <div class="strip">
          <ss-pad big solid tone="amber" row icon="power" label="All lights" sub="Every light together" hotkey="L" data-ref="masterPower"></ss-pad>
          <div class="faders">
            <ss-fader data-ref="masterBrightness" label="Brightness" unit="%" min="1" max="100" step="1" default="70" accent="amber"></ss-fader>
            <ss-fader data-ref="masterKelvin" class="kelvin" label="Temperature" unit="K" min="2700" max="6500" step="100" default="4500" accent="amber" ticks="2700,4000,5500,6500"></ss-fader>
          </div>
          <p class="hint" data-ref="masterNote">Moves every connected light to the same value.</p>
        </div>
      </ss-card>
    </section>

    <div class="li-lights" data-ref="lights"></div>
  </div>

  <div class="li-empty" data-ref="empty" hidden>
    <div class="li-empty-art" aria-hidden="true" data-ref="emptyArt"></div>
    <h2 data-ref="emptyTitle">Looking for lights…</h2>
    <p data-ref="emptyText">Plug a Logitech Litra Glow, Beam or Beam LX into USB. It appears here by itself.</p>
    <ss-button icon="refresh" data-ref="rescan">Look again</ss-button>
  </div>

  <ss-card class="li-presets" heading="Presets" icon="sliders" data-ref="presetCard">
    <div class="pads" role="group" aria-label="Lighting presets" data-ref="presets"></div>
    <form class="save" data-ref="saveForm" autocomplete="off">
      <label class="save-l" for="li-name">Save the current lights as a preset</label>
      <div class="save-row">
        <input id="li-name" class="text-input" data-ref="saveName" type="text" maxlength="40" placeholder="Name, e.g. Interview" aria-label="Preset name">
        <ss-button data-ref="saveBtn" icon="check" disabled>Save current</ss-button>
        <ss-button data-ref="deleteBtn" variant="ghost" icon="trash" hidden>Delete preset</ss-button>
      </div>
      <p class="hint" data-ref="saveNote" role="status"></p>
    </form>
  </ss-card>
</div>`;

const modelLine = (l) => {
  const bits = [l.model];
  if (l.lumen && l.connected) bits.push(`${l.lumen} lm`);
  return bits.join(' · ');
};

export async function mount(host, ctx) {
  const { store, act, actStream, flushStream, registerCommands } = ctx;
  await ctx.ensureCss(new URL('./common.css', import.meta.url).href);
  await ctx.ensureCss(new URL('./lighting.css', import.meta.url).href);
  document.title = 'Lighting · StreamStack';
  host.innerHTML = TEMPLATE;
  const R = {};
  host.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  const off = [];
  const sub = (selector, cb) => off.push(store.subscribe(selector, cb));
  const lighting = () => store.state.lighting;

  R.masterBrightness.style.setProperty('--fader-w', '3rem');
  for (const f of [R.masterKelvin]) {
    f.style.setProperty('--fader-groove', WARM_COOL);
    f.style.setProperty('--fader-fill-opacity', '0');
    f.style.setProperty('--fader-groove-h', '0.9rem');
  }
  R.emptyArt.innerHTML = iconHTML('lights');

  // ---------- fader wiring (one helper for the master and every card) ----------
  const wire = (fader, action, field, getId, powerOn) => {
    fader.addEventListener('input', (e) => {
      const id = getId();
      actStream(action, { id, [field]: e.detail.value });
    });
    fader.addEventListener('change', () => {
      flushStream(action);
      const id = getId();
      const off_ = id === 'all' ? !lighting().master.on : lighting().lights.find((l) => l.id === id)?.power === false;
      if (powerOn && off_) act('lighting.power', { id, on: true });
    });
  };
  wire(R.masterBrightness, 'lighting.brightness', 'percent', () => 'all', true);
  wire(R.masterKelvin, 'lighting.temperature', 'kelvin', () => 'all', true);
  R.masterPower.addEventListener('click', () => toggleLights());

  // ---------- notice + empty state ----------
  sub((s) => [s.lighting.problem, s.lighting.scanned, s.lighting.lights.length], ([problem, scanned, count]) => {
    R.notice.hidden = !problem;
    if (problem) {
      R.noticeTitle.textContent = problem.kind === 'permission' ? 'The lights are there, but this user may not use them' : 'Lighting problem';
      R.noticeText.textContent = problem.message;
    }
    R.empty.hidden = count > 0;
    R.emptyTitle.textContent = scanned ? 'No lights found' : 'Looking for lights…';
    R.emptyText.textContent = scanned
      ? 'Plug a Logitech Litra Glow, Beam or Beam LX into USB. It appears here by itself, no restart needed.'
      : 'Checking the USB bus.';
  });
  R.rescan.addEventListener('click', () => act('lighting.rescan'));

  // ---------- master ----------
  sub((s) => [s.lighting.master, s.lighting.lights, s.lighting.kelvin_range, s.lighting.kelvin_step], ([m, lights, [lo, hi], step]) => {
    const live = lights.filter((l) => l.connected);
    R.masterCard.toggleAttribute('hidden', lights.length < 2);
    R.masterRegion.hidden = lights.length < 2;
    R.masterPower.toggleAttribute('active', m.on);
    R.masterPower.setAttribute('label', m.on ? 'All lights on' : 'All lights off');
    R.masterPower.setAttribute('sub', m.on ? (m.all_on ? 'Press to turn all off' : 'Some are off. Press to turn all off') : 'Press to turn all on');
    R.masterBrightness.value = m.brightness;
    R.masterKelvin.setAttribute('min', lo);
    R.masterKelvin.setAttribute('max', hi);
    R.masterKelvin.setAttribute('step', step);
    R.masterKelvin.value = m.kelvin;
    for (const el of [R.masterPower, R.masterBrightness, R.masterKelvin]) el.toggleAttribute('disabled', live.length === 0);
  });

  // ---------- one card per light ----------
  const create = (l) => {
    const power = h('ss-pad', { big: true, solid: true, row: true, tone: 'amber', icon: 'power', label: 'Power' });
    const brightness = h('ss-fader', { label: 'Brightness', unit: '%', min: 1, max: 100, step: 1, default: 70, accent: 'amber' });
    const kelvin = h('ss-fader', { label: 'Temperature', unit: 'K', min: 2700, max: 6500, step: 100, default: 4500, accent: 'amber', ticks: '2700,4000,5500,6500', class: 'kelvin' });
    kelvin.style.setProperty('--fader-groove', WARM_COOL);
    kelvin.style.setProperty('--fader-fill-opacity', '0');
    kelvin.style.setProperty('--fader-groove-h', '0.9rem');
    const identify = h('ss-button', { size: 'sm', variant: 'ghost', icon: 'lights', title: 'Pulse this light so you can tell which one it is' }, 'Identify');
    const model = h('span', { class: 'dev' });
    const card = h('ss-card', { icon: 'lights' },
      h('span', { slot: 'actions', class: 'dev' }),
      h('div', { class: 'strip' }, power, h('div', { class: 'faders' }, brightness, kelvin), h('div', { class: 'foot' }, model, identify)));
    const section = h('section', { class: 'li-light', role: 'region' }, card);
    const id = () => section.dataset.key;
    power.addEventListener('click', () => {
      const cur = lighting().lights.find((x) => x.id === id());
      if (cur) act('lighting.power', { id: cur.id, on: !cur.power });
    });
    wire(brightness, 'lighting.brightness', 'percent', id, true);
    wire(kelvin, 'lighting.temperature', 'kelvin', id, true);
    identify.addEventListener('click', () => act('lighting.identify', { id: id() }));
    section.refs = { card, power, brightness, kelvin, identify, model };
    return section;
  };
  const update = (section, l) => {
    const { card, power, brightness, kelvin, identify, model } = section.refs;
    section.setAttribute('aria-label', l.name);
    card.setAttribute('heading', l.name);
    card.querySelector('[slot="actions"]').textContent = l.connected ? (l.identifying ? 'Blinking' : (l.power ? 'On' : 'Off')) : 'Not available';
    if (l.connected) card.removeAttribute('reason');
    else card.setAttribute('reason', l.error || 'This light is not responding');
    power.toggleAttribute('active', !!l.power);
    power.setAttribute('label', l.power ? 'On' : 'Off');
    power.setAttribute('sub', l.power ? 'Press to turn off' : 'Press to turn on');
    brightness.value = l.brightness;
    kelvin.value = l.kelvin;
    const [lo, hi] = lighting().kelvin_range;
    kelvin.setAttribute('min', lo);
    kelvin.setAttribute('max', hi);
    kelvin.setAttribute('step', lighting().kelvin_step);
    model.textContent = modelLine(l);
    identify.toggleAttribute('disabled', !l.connected || l.identifying);
  };
  sub((s) => [s.lighting.lights, s.lighting.kelvin_range], ([lights]) => {
    renderList(R.lights, lights, (l) => l.id, create, update);
    R.lights.dataset.count = String(lights.length);
  });

  // ---------- presets ----------
  sub((s) => [s.lighting.presets, s.lighting.preset, s.lighting.lights], ([presets, active, lights]) => {
    const any = lights.some((l) => l.connected);
    renderList(R.presets, presets, (p) => p.name, () => {
      const pad = h('ss-pad', {});
      pad.addEventListener('click', () => selectLightPreset(pad.dataset.key));
      return pad;
    }, (pad, p) => {
      pad.setAttribute('label', p.name);
      pad.setAttribute('sub', p.blurb || '');
      pad.setAttribute('icon', 'lights');
      pad.toggleAttribute('active', p.name === active);
      pad.toggleAttribute('disabled', !any);
    });
    const cur = presets.find((p) => p.name === active);
    R.deleteBtn.hidden = !(cur && !cur.builtin);
    if (cur && !cur.builtin) R.deleteBtn.setAttribute('aria-label', `Delete preset ${cur.name}`);
  });
  sub((s) => s.lighting.lights, (lights) => {
    const any = lights.some((l) => l.connected);
    R.saveBtn.toggleAttribute('disabled', !any || !R.saveName.value.trim());
    R.presetCard.toggleAttribute('data-empty', !any);
  });
  R.saveName.addEventListener('input', () => {
    const any = lighting().lights.some((l) => l.connected);
    R.saveBtn.toggleAttribute('disabled', !any || !R.saveName.value.trim());
  });
  const save = () => {
    const name = R.saveName.value.trim();
    if (!name) return;
    act('lighting.presets.save', { name });
    R.saveNote.textContent = `Saved "${name}".`;
    R.saveName.value = '';
    R.saveBtn.setAttribute('disabled', '');
  };
  R.saveBtn.addEventListener('click', save);
  R.saveForm.addEventListener('submit', (e) => { e.preventDefault(); save(); });
  R.deleteBtn.addEventListener('click', () => {
    const name = lighting().preset;
    if (name) { act('lighting.presets.delete', { name }); R.saveNote.textContent = `Deleted "${name}".`; }
  });

  off.push(registerCommands(() => [
    { id: 'Lighting:rescan', group: 'Lighting', title: 'Look for lights again', icon: 'refresh', keywords: 'lights litra rescan usb', run: () => act('lighting.rescan') },
  ]));
  return () => off.forEach((fn) => (typeof fn === 'function' ? fn() : undefined));
}
