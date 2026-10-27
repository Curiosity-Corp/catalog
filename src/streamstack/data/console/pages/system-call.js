// pages/system-call.js — call awareness, on the System page.
//
// state.call says whether a meeting app is in a call (idle, joining, in call), which one, and whether the
// microphone looks muted there. The panel shows that and the "you are muted" warning settings. The warning
// itself arrives as an ordinary toast from the server; an optional beep plays when its count goes up.

import { h } from '../core/util.js';

const STATE_WORD = { idle: 'No call', joining: 'Joining a call', in_call: 'In a call' };
const MIC_WORD = { open: 'Microphone open in the app', muted: 'Muted in the app', none: 'No microphone use' };

const TEMPLATE = `
<section class="call" aria-labelledby="hCall">
  <h2 class="sec" id="hCall">Call awareness <span class="sec-note">Reads which apps use the camera and microphone; never the audio</span></h2>
  <div class="call-card">
    <div class="call-row">
      <div class="call-text">
        <strong data-ref="state" role="status"></strong>
        <span class="call-sub" data-ref="detail"></span>
      </div>
    </div>
    <div class="call-row">
      <div class="call-text">
        <strong>Warn when I talk while muted</strong>
        <span class="call-sub" data-ref="warnNote"></span>
      </div>
      <button type="button" class="call-switch" role="switch" aria-checked="true" aria-label="Warn when I talk while muted" data-ref="enabled"><span class="call-knob"></span></button>
    </div>
    <div class="call-row call-opts" data-ref="opts">
      <label class="call-field">Sensitivity
        <select data-ref="sensitivity" aria-label="Warning sensitivity"></select>
      </label>
      <label class="call-field">Wait between warnings
        <select data-ref="cooldown" aria-label="Wait between warnings"></select>
      </label>
      <label class="call-field call-check">
        <input type="checkbox" data-ref="sound" aria-label="Play a short beep with the warning"> Beep
      </label>
    </div>
    <p class="call-sub" data-ref="limit">The Console's own Mute silences the microphone itself, so there is nothing left to hear: the warning is for a mute inside the meeting app.</p>
  </div>
</section>`;

const COOLDOWNS = [10, 20, 45, 120];

export async function mountCallPanel(host, ctx) {
  const { store, act } = ctx;
  await ctx.ensureCss(new URL('./system-call.css', import.meta.url).href);
  const frag = document.createElement('template');
  frag.innerHTML = TEMPLATE;
  const sec = frag.content.firstElementChild;
  const anchor = host.querySelector('.diag') || host.querySelector('.stor') || host.querySelector('.hero');
  if (anchor) anchor.after(sec); else host.prepend(sec);
  const R = {};
  sec.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });

  const set = (params) => act('call.set', params, { quiet: false });
  R.enabled.addEventListener('click', () => set({ enabled: R.enabled.getAttribute('aria-checked') !== 'true' }));
  R.sensitivity.addEventListener('change', () => set({ sensitivity: R.sensitivity.value }));
  R.cooldown.addEventListener('change', () => set({ cooldown_s: Number(R.cooldown.value) }));
  R.sound.addEventListener('change', () => set({ sound: R.sound.checked }));

  const render = (c) => {
    sec.hidden = !c.available;
    const w = c.muted_warning;
    R.state.textContent = c.service && c.state !== 'idle' ? `${STATE_WORD[c.state]} (${c.service})` : STATE_WORD[c.state] || c.state;
    sec.dataset.state = c.state;
    R.detail.textContent = c.state === 'idle'
      ? 'Nothing is using the camera or the microphone for a meeting.'
      : `${MIC_WORD[c.mic] || c.mic}${c.camera ? '; camera on' : ''}.`;
    R.enabled.setAttribute('aria-checked', String(w.enabled));
    R.warnNote.textContent = w.count
      ? `Warned ${w.count} time${w.count === 1 ? '' : 's'} this session.`
      : 'A toast appears once each time you start talking into a muted call.';
    R.opts.hidden = !w.enabled;
    if (!R.sensitivity.options.length) {
      R.sensitivity.replaceChildren(...w.sensitivities.map((s) => h('option', { value: s }, s[0].toUpperCase() + s.slice(1))));
      R.cooldown.replaceChildren(...COOLDOWNS.map((s) => h('option', { value: String(s) }, `${s} seconds`)));
    }
    R.sensitivity.value = w.sensitivity;
    if (![...R.cooldown.options].some((o) => Number(o.value) === w.cooldown_s)) {
      R.cooldown.append(h('option', { value: String(w.cooldown_s) }, `${w.cooldown_s} seconds`));
    }
    R.cooldown.value = String(w.cooldown_s);
    R.sound.checked = w.sound;
  };
  const off = store.subscribe((s) => s.call, render);
  return () => { off(); sec.remove(); };
}
