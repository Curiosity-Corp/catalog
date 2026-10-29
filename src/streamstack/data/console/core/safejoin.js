// core/safejoin.js — the "You are not on air" banner and the Go live controls, shared by Live and Mini.
//
//   mountSafeJoinBanner(host, ctx, { compact })  -> teardown
//       Fills `host` and keeps it in step with state.safejoin. Hidden unless Safe join is holding privacy or mute.
//   registerSafeJoinControls(ctx)                -> teardown
//       Space and the palette command "Go live"; app.js registers them once for every page.
//
// Safe join (src/streamstack/console/safejoin.py) takes privacy and the microphone when a call app opens the camera;
// Go live gives back exactly what it took.

import { iconHTML } from '../components/icons.js';
import { act } from './actions.js';

export const goLive = () => act('safejoin.go_live', {});

const holdingWhat = (held) => [held.privacy ? 'camera off' : '', held.mute ? 'microphone muted' : ''].filter(Boolean).join(' and ');

export function mountSafeJoinBanner(host, ctx, { compact = false } = {}) {
  host.classList.add('sj');
  if (compact) host.classList.add('sj-compact');
  host.setAttribute('role', 'alert');
  host.hidden = true;
  host.innerHTML = `
    <span class="sj-icon">${iconHTML('eye-off')}</span>
    <div class="sj-text"><strong class="sj-title">You are not on air</strong><p class="sj-msg"></p></div>
    <ss-button class="sj-btn" variant="primary" size="lg" icon="broadcast">Go live</ss-button>`;
  const msg = host.querySelector('.sj-msg');
  host.querySelector('.sj-btn').addEventListener('click', () => goLive());
  const off = ctx.store.subscribe((s) => s.safejoin, (sj) => {
    host.hidden = !sj.holding;
    if (!sj.holding) return;
    const who = sj.service ? `${sj.service} has the camera. ` : '';
    const timer = sj.release_in_s != null ? ` Going live in ${Math.ceil(sj.release_in_s)} s.` : '';
    msg.textContent = `${who}Held: ${holdingWhat(sj.held)}. Press Go live when you are ready.${timer}`;
  });
  return () => { off(); host.hidden = true; };
}

export function registerSafeJoinControls(ctx) {
  const needed = () => ctx.store.state.safejoin.holding;
  const offKeys = ctx.registerKeys([
    { key: ' ', desc: 'Go live (release what Safe join is holding)', group: 'Live', label: 'Space', run: () => goLive(), when: needed },
  ]);
  const offCmds = ctx.registerCommands(() => (needed() ? [{
    id: 'Live:Go live', group: 'Live', title: 'Go live', icon: 'broadcast', hint: 'Space',
    keywords: 'go live safe join release privacy mute on air call',
    run: () => goLive(),
  }] : []));
  return () => { offKeys(); offCmds(); };
}
