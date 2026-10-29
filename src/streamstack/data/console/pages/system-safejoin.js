// pages/system-safejoin.js — Safe join, on the System page: what happens when a call app takes the camera.
//
// state.safejoin: the setting (off, warn, safe), the optional auto-release timer, whether something is held right now
// and the last call starts with what was done for each (the event list).

import { h } from '../core/util.js';
import { relTime } from '../core/time.js';

const MODE_WORD = {
  off: 'Off: do nothing',
  warn: 'Warn: tell me the camera is on air',
  safe: 'Safe join: privacy on and muted until I press Go live',
};
const TIMERS = [0, 15, 30, 60, 120];

const TEMPLATE = `
<section class="sj-card" aria-labelledby="hSj">
  <h2 class="sec" id="hSj">On call start <span class="sec-note">What StreamStack does when Teams, Zoom or Meet takes the camera</span></h2>
  <div class="sjc">
    <div class="sjc-row">
      <label class="sjc-field">When a call starts
        <select data-ref="mode" aria-label="On call start"></select>
      </label>
      <label class="sjc-field" data-ref="timerRow">Go live by itself after
        <select data-ref="timer" aria-label="Go live automatically after"></select>
      </label>
    </div>
    <p class="sjc-note" data-ref="note"></p>
    <div data-ref="eventsBox" hidden>
      <h3 class="sjc-h">Recent call starts</h3>
      <ul class="sjc-events" data-ref="events" aria-label="Recent call starts"></ul>
    </div>
  </div>
</section>`;

export async function mountSafeJoinPanel(host, ctx) {
  const { store, act } = ctx;
  await ctx.ensureCss(new URL('./system-safejoin.css', import.meta.url).href);
  const frag = document.createElement('template');
  frag.innerHTML = TEMPLATE;
  const sec = frag.content.firstElementChild;
  const anchor = host.querySelector('.call:not(.call-card)') || host.querySelector('.diag') || host.querySelector('.hero');
  if (anchor) anchor.after(sec); else host.prepend(sec);
  const R = {};
  sec.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });
  R.timer.replaceChildren(...TIMERS.map((s) => h('option', { value: String(s) }, s ? `${s} seconds` : 'Never')));
  R.mode.addEventListener('change', () => act('safejoin.set', { mode: R.mode.value }, { quiet: false }));
  R.timer.addEventListener('change', () => act('safejoin.set', { auto_release_s: Number(R.timer.value) }, { quiet: false }));

  const render = (sj) => {
    sec.hidden = !sj.available;
    if (!R.mode.options.length) R.mode.replaceChildren(...sj.modes.map((m) => h('option', { value: m }, MODE_WORD[m] || m)));
    R.mode.value = sj.mode;
    R.timerRow.hidden = sj.mode !== 'safe';
    if (![...R.timer.options].some((o) => Number(o.value) === sj.auto_release_s)) {
      R.timer.append(h('option', { value: String(sj.auto_release_s) }, `${sj.auto_release_s} seconds`));
    }
    R.timer.value = String(sj.auto_release_s);
    R.note.textContent = sj.mode === 'safe'
      ? 'Only a call app that opens the camera counts. What you muted or hid yourself is never touched, and it never takes anything back once you go live.'
      : sj.mode === 'warn' ? 'A toast appears when a call app takes the camera.' : 'Nothing happens when a call starts.';
    R.eventsBox.hidden = !sj.events.length;
    R.events.replaceChildren(...sj.events.slice(0, 6).map((e) => {
      const did = e.did.length ? e.did.join(', ') : e.mode === 'warn' ? 'told you' : 'nothing to hold';
      const kept = e.kept.length ? ` (${e.kept.join('; ')})` : '';
      return h('li', {}, h('strong', {}, `${e.service} `), h('span', {}, `${did}${kept}`), h('time', {}, relTime(e.at)),
        ...e.notes.map((n) => h('small', {}, n)));
    }));
  };
  const off = store.subscribe((s) => s.safejoin, render);
  return () => { off(); sec.remove(); };
}
