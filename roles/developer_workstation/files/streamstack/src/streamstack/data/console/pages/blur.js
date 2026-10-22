// pages/blur.js — BACKGROUND BLUR controls, shared by Camera (full card body) and Live (compact row).
//
//   mountBlur(root, ctx, { variant: 'full' | 'compact', source: () => name | undefined })
//
// The state lives in `video.blur` (docs/console.md): per-camera filter state, the plugin's detection
// result (installed / version / device / install hint) and the cost guard's verdict. The action is
// `video.blur` { on?, strength?, rate?, source? }; a missing `source` means every camera.
//
//   full     toggle, strength fader (1..20 blur passes), smoothness (Light / Balanced / Smooth =
//            how often the mask is recomputed), device line and a cost pill. When the plugin is not
//            installed it shows what to do about it instead of dead controls.
//   compact  one pad ("Blur") and a strength fader for the cockpit; hidden while the plugin is missing,
//            because mid-stream is the wrong time to read an install note.
//
// Returns a function that detaches everything it attached.

import { h, clamp } from '../core/util.js';

const SMOOTHNESS = [
  { key: 'light', label: 'Light', title: 'Recompute the mask every third frame: cheapest, the edge trails fast motion' },
  { key: 'balanced', label: 'Balanced', title: 'Recompute the mask every second frame' },
  { key: 'smooth', label: 'Smooth', title: 'Recompute the mask every frame: sharpest edge, most CPU' },
];

const COST_PILL = {
  off: null,
  measuring: { state: 'info', label: 'Measuring' },
  ok: { state: 'ok', label: 'Within budget' },
  over: { state: 'warn', label: 'Over budget' },
};

const MISSING = 'Background blur needs the obs-backgroundremoval plugin, which is not installed here.';
const NOT_LOADED = 'The plugin is installed, but this OBS has not loaded it. Restart OBS (Start studio) and try again.';

/** What the control should show for one snapshot: one pure function so every state is easy to reason about. */
export function blurView(state, source) {
  const v = state.video;
  const b = v.blur;
  const entry = (source && b.sources[source]) || null;
  const view = {
    installed: !!b.plugin.installed && b.plugin.models_ok !== false,
    on: entry ? !!entry.enabled : !!b.on,
    strength: entry && entry.present ? entry.strength : b.strength,
    rate: entry && entry.present ? entry.rate : b.rate,
    reason: '',
    fix: '',
    cost: b.cost || { state: 'off', detail: '' },
    version: b.plugin.version || '',
    device: (b.plugin.device_labels && b.plugin.device_labels[0]) || 'CPU',
  };
  if (!view.installed) {
    const how = b.plugin.hint ? ` To fix: ${b.plugin.hint}.` : '';
    view.reason = b.plugin.installed ? `The obs-backgroundremoval models are missing.${how}` : `${MISSING}${how}`;
  } else if (v.obs !== 'connected') {
    view.reason = v.obs === 'offline' ? 'OBS is not running' : (v.obs_error || 'OBS is not reachable');
    view.fix = v.obs === 'offline' ? 'Start studio' : '';
  } else if (b.available === false) {
    view.reason = NOT_LOADED;
  } else if (!v.sources.length) {
    view.reason = 'No camera source to blur';
  }
  return view;
}

function rateKey(rate, presets) {
  const found = Object.entries(presets || {}).find(([, r]) => r === rate);
  return found ? found[0] : '';
}

export function mountBlur(root, ctx, { variant = 'full', source = () => undefined } = {}) {
  const { store, act, actStream, flushStream } = ctx;
  const off = [];
  const compact = variant === 'compact';
  const params = (extra) => {
    const src = source();
    return src ? { ...extra, source: src } : extra;
  };

  const strength = h('ss-fader', {
    orientation: 'horizontal', label: 'Blur strength', min: '1', max: '20', step: '1', default: '8', ticks: 'none',
    class: 'bl-strength', 'data-ref': 'blurStrength',
  });
  strength.formatter = (v) => String(Math.round(v));
  strength.addEventListener('input', (e) => actStream('video.blur', params({ strength: Math.round(e.detail.value) }), { key: 'video.blur.strength' }));
  strength.addEventListener('change', () => flushStream('video.blur.strength'));

  let pad = null;
  let toggle = null;
  let seg = null;
  let costPill = null;
  let info = null;

  if (compact) {
    pad = h('ss-pad', { compact: true, label: 'Blur', icon: 'camera', 'data-ref': 'blurPad', title: 'Blur the background of the camera (needs the obs-backgroundremoval plugin)' });
    pad.addEventListener('click', () => act('video.blur', params({ on: !blurView(store.state, source()).on })));
    root.append(pad, strength);
  } else {
    toggle = h('ss-toggle', { label: 'Blur background', 'data-ref': 'blurOn' });
    toggle.addEventListener('change', (e) => act('video.blur', params({ on: !!e.detail.checked })));
    seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Mask smoothness', 'data-ref': 'blurSmooth' },
      SMOOTHNESS.map((o) => {
        const b = h('button', { type: 'button', 'data-smooth': o.key, 'aria-pressed': 'false', title: o.title }, h('span', {}, o.label));
        b.addEventListener('click', () => act('video.blur', params({ rate: store.state.video.blur.presets[o.key] })));
        return b;
      }));
    costPill = h('span', { class: 'pill bl-pill', 'data-ref': 'blurCost', hidden: true });
    info = h('p', { class: 'hint bl-info', 'data-ref': 'blurInfo' });
    root.append(
      h('div', { class: 'bl-top' }, toggle, costPill),
      h('div', { class: 'bl-ctl' }, strength, h('div', { class: 'bl-smooth' }, h('span', { class: 'eyebrow' }, 'Smoothness'), seg)),
      info,
    );
  }

  const render = () => {
    const state = store.state;
    const view = blurView(state, source());
    const [lo, hi] = state.video.blur.limits.strength;
    strength.setAttribute('min', lo);
    strength.setAttribute('max', hi);
    strength.value = clamp(view.strength, lo, hi);
    const blocked = !!view.reason;
    strength.toggleAttribute('disabled', blocked);
    if (compact) {
      root.hidden = !view.installed;    // not installed: nothing to offer mid-stream
      pad.toggleAttribute('active', view.on && !blocked);
      pad.toggleAttribute('disabled', blocked);
      pad.setAttribute('sub', view.on ? `On, strength ${view.strength}` : 'Off');
      strength.hidden = !view.on;
      return;
    }
    toggle.toggleAttribute('checked', view.on);
    toggle.toggleAttribute('disabled', blocked);
    const presets = state.video.blur.presets;
    const current = rateKey(view.rate, presets);
    for (const b of seg.querySelectorAll('button')) {
      b.setAttribute('aria-pressed', String(b.dataset.smooth === current));
      b.disabled = blocked;
    }
    const pill = view.on && !blocked ? COST_PILL[view.cost.state] : null;
    costPill.hidden = !pill;
    costPill.textContent = pill ? pill.label : '';
    if (pill) costPill.dataset.state = pill.state;
    let text = '';
    let tone = '';
    if (blocked) {
      text = view.reason;
      tone = view.installed ? '' : 'warn';
    } else if (pill) {
      text = view.cost.detail;
      tone = view.cost.state === 'over' ? 'warn' : '';
    } else if (view.installed) {
      text = `Runs on the ${view.device}${view.version ? `, obs-backgroundremoval ${view.version}` : ''}. Budget: under ${view.cost.limits?.p95_ms ?? 8} ms per frame.`;
    }
    info.textContent = text;
    if (tone) info.dataset.state = tone; else delete info.dataset.state;
  };

  off.push(store.subscribe((s) => [s.video.blur, s.video.obs, s.video.obs_error, s.video.sources, source()], render));
  return () => { off.forEach((fn) => fn()); };
}

/** Command-palette entries for the blur control (the page passes its own `source`). */
export function blurCommands(ctx, source = () => undefined) {
  const { store, act } = ctx;
  const view = blurView(store.state, source());
  if (!view.installed) return [];
  const src = source();
  return [{
    id: 'blur:toggle',
    group: 'Camera',
    title: view.on ? 'Background blur off' : 'Background blur on',
    icon: 'camera',
    keywords: 'blur background bokeh privacy',
    run: () => act('video.blur', src ? { on: !view.on, source: src } : { on: !view.on }),
  }];
}
