// core/revive.js — the "microphone stalled" banner and the Revive controls, shared by Live and Mini.
//
//   mountReviveBanner(host, ctx, { compact })  -> teardown
//       Fills `host` (an absolutely positioned overlay inside the mic card) and keeps it in step with
//       state.audio.mic.{stalled, revive}. Hidden while the mic is healthy.
//   registerReviveControls(ctx)                -> teardown
//       Shift+R and the palette command "Revive mic"; app.js registers them once for every page.
//
// States: stalled (nobody can hear you, one big button) -> reviving (progress text) -> ok (brief
// confirmation) or failed (what to try next, plus Try again).

import { iconHTML } from '../components/icons.js';
import { reviveMic } from './ops.js';

const TITLES = {
  stalled: 'Microphone stalled — nobody can hear you',
  silent: 'Microphone delivers digital silence — nobody can hear you',
  reviving: 'Reviving the microphone…',
  ok: 'Microphone is back',
  failed: 'Could not revive the microphone',
};
const ICONS = { stalled: 'alert', silent: 'alert', reviving: 'refresh', ok: 'check', failed: 'alert' };
const BUTTONS = { stalled: 'Revive mic', silent: 'Revive mic', reviving: 'Reviving…', failed: 'Try again' };

/** One of 'stalled' | 'silent' (digital silence) | 'reviving' | 'ok' | 'failed' | '' (healthy). */
export function reviveView(mic) {
  const r = (mic && mic.revive) || {};
  if (r.state === 'reviving') return 'reviving';
  if (r.state === 'failed') return 'failed';
  if (mic && mic.stalled) return mic.stall_kind === 'silence' ? 'silent' : 'stalled';
  if (r.state === 'ok') return 'ok';
  return '';
}

export function mountReviveBanner(host, ctx, { compact = false } = {}) {
  host.classList.add('revive');
  if (compact) host.classList.add('revive-compact');
  host.setAttribute('role', 'alert');
  host.hidden = true;
  host.innerHTML = `
    <span class="rv-icon">${iconHTML('alert')}</span>
    <div class="rv-text"><strong class="rv-title"></strong><p class="rv-msg"></p></div>
    <ss-button class="rv-btn" variant="primary" size="lg" icon="refresh"></ss-button>`;
  const q = (sel) => host.querySelector(sel);
  const icon = q('.rv-icon');
  const title = q('.rv-title');
  const msg = q('.rv-msg');
  const btn = q('.rv-btn');
  let shown = '';
  btn.addEventListener('click', () => reviveMic());
  const unsub = ctx.store.subscribe((s) => s.audio.mic, (mic) => {
    const view = reviveView(mic);
    host.hidden = view === '';
    host.dataset.state = view;
    if (!view) { shown = ''; return; }
    if (view !== shown) {
      icon.innerHTML = iconHTML(ICONS[view]);
      title.textContent = TITLES[view];
      shown = view;
    }
    const m = (mic.revive && mic.revive.message) || '';
    const idle = {
      stalled: 'Audio stopped flowing. One press restarts the microphone, nothing else is touched.',
      silent: 'The microphone runs but every sample is zero. One press re-writes its capture switch and volume so the device receives them again.',
    };
    msg.textContent = !m && idle[view] ? idle[view] : m;
    btn.hidden = view === 'ok';
    if (BUTTONS[view]) btn.textContent = BUTTONS[view];
    btn.toggleAttribute('busy', view === 'reviving');
    btn.toggleAttribute('disabled', view === 'reviving');
  });
  return () => { unsub(); host.hidden = true; };
}

export function registerReviveControls(ctx) {
  // Only offered while the watchdog says the mic is stalled (or a revive just failed): never mid-call on a healthy mic.
  const needed = () => {
    const view = reviveView(ctx.store.state.audio.mic);
    return view === 'stalled' || view === 'silent' || view === 'failed';
  };
  const offKeys = ctx.registerKeys([
    { key: 'shift+r', desc: 'Revive the microphone (restart a stalled mic)', group: 'Live', label: 'Shift R', run: () => reviveMic(), when: needed },
  ]);
  const offCmds = ctx.registerCommands(() => {
    if (!needed()) return [];
    return [{
      id: 'Mic:Revive mic', group: 'Mic', title: 'Revive mic', icon: 'refresh', hint: 'Shift R',
      keywords: 'revive restart stalled stuck mic microphone silent nobody hears',
      run: () => reviveMic(),
    }];
  });
  return () => { offKeys(); offCmds(); };
}
