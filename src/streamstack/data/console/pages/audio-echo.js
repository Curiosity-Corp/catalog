// pages/audio-echo.js — THE ECHO CHECK on the Audio page's Output section.
//
//   mountEchoPanel(root, ctx)   one card: pick an output, "Check for bleed" plays a quiet sweep on that output only and
//                               listens on the raw microphone; the answer names the output ("No bleed from Dell Monitor",
//                               "Bleed at -34 dB from ...: use headphones or lower the volume", or a feedback loop).
//
// A verdict taken while the network is degraded or a stream is stalling names that link in its text (the card gets
// data-link="caveat" and the remembered row says "link not clean"): it is never blamed on, or cleared by, the link.
//
// The check never plays during a call: while the call state is not idle the button is off and says why, and the server
// refuses anyway. Returns a cleanup function.

import { h, renderList } from '../core/util.js';
import { icon } from '../components/icons.js';
import { relTime } from '../core/time.js';

const TONE = { none: 'ok', slight: 'ok', moderate: 'warn', strong: 'warn', feedback: 'bad', unmeasured: 'ask' };
const LEVEL_WORD = { none: 'No bleed', slight: 'Slight bleed', moderate: 'Moderate bleed', strong: 'Strong bleed', feedback: 'Feedback loop', unmeasured: 'Not measured' };

export function mountEchoPanel(root, ctx) {
  const { store, act } = ctx;
  root.classList.add('ae');
  const sel = h('select', { class: 'text-input ae-sel', 'aria-label': 'Output to check for bleed' });
  const btn = h('ss-button', { class: 'ae-go', variant: 'primary', icon: 'mic' }, 'Check for bleed');
  const why = h('p', { class: 'ae-why hint', role: 'status' });
  const answer = h('p', { class: 'ae-answer', role: 'status', 'aria-live': 'polite' });
  const list = h('ul', { class: 'ae-list', 'aria-label': 'Echo check results' });
  root.replaceChildren(
    h('header', { class: 'ae-head' },
      h('span', { class: 'ae-ic' }, icon('wave')),
      h('div', { class: 'ae-id' }, h('b', {}, 'Echo check'),
        h('small', {}, 'Plays a quiet sweep on one output and listens on your microphone. Stay quiet for a few seconds.'))),
    h('div', { class: 'ae-row' }, sel, btn),
    why, answer, list);

  let chosen = '';
  sel.addEventListener('change', () => { chosen = sel.value; });
  btn.addEventListener('click', () => { if (sel.value) act('echo.check', { name: sel.value }); });

  const paint = () => {
    const s = store.state;
    const o = s.audio.outputs;
    const e = s.echo;
    root.hidden = !e.available;
    if (!e.available) return;
    const sinks = o.devices;
    renderList(sel, sinks, (d) => d.name, (d) => h('option', { value: d.name }), (opt, d) => { opt.textContent = d.label; });
    if (!chosen || !sinks.some((d) => d.name === chosen)) chosen = o.default || sinks[0]?.name || '';
    sel.value = chosen;
    const d = sinks.find((x) => x.name === chosen);
    const running = e.state === 'running';
    const mic = s.audio.mic;
    const reason = e.blocked
      || (!d ? 'No output to check.'
        : d.muted || d.volume === 0 ? `${d.label} is muted or at zero volume.`
        : o.test.state === 'playing' ? 'An output test is playing.'
        : !mic.present ? 'No microphone found to listen with.'
        : mic.muted ? 'The microphone is muted, so it cannot hear the sound.'
        : mic.stalled ? 'The microphone is stalled.' : '');
    btn.toggleAttribute('disabled', running || !!reason);
    btn.toggleAttribute('busy', running);
    btn.textContent = running ? 'Listening…' : 'Check for bleed';
    why.textContent = running ? '' : reason;
    why.hidden = running || !reason;

    const r = e.result;
    root.dataset.tone = running ? 'busy' : e.state === 'error' ? 'bad' : r ? TONE[r.level] || 'ask' : 'none';
    answer.textContent = running ? `Playing on ${d?.label || 'the output'} and listening…`
      : e.state === 'error' ? e.error || 'The echo check failed.'
      : r ? r.text : '';
    answer.hidden = !answer.textContent;
    root.dataset.link = !running && r && r.link && r.link.level === 'caveat' ? 'caveat' : 'ok';

    const rows = Object.entries(e.results).map(([name, v]) => ({ name, ...v })).sort((a, b) => (a.at < b.at ? 1 : -1));
    renderList(list, rows, (v) => v.name, () => h('li', { class: 'ae-item' }, h('i'), h('span', { class: 'ae-n' }), h('span', { class: 'ae-w' }), h('time', {})), (li, v) => {
      li.dataset.tone = TONE[v.level] || 'ask';
      li.querySelector('.ae-n').textContent = v.label;
      li.querySelector('.ae-w').textContent = v.coupling_db === null || v.level === 'none' || v.level === 'unmeasured'
        ? LEVEL_WORD[v.level] : `${LEVEL_WORD[v.level]}, ${v.coupling_db} dB`;
      if (v.link && v.link.level === 'caveat') li.querySelector('.ae-w').textContent += ' (link not clean)';
      li.querySelector('time').textContent = relTime(v.at);
    });
    list.hidden = !rows.length;
  };

  const off = [store.subscribe((s) => [s.echo, s.audio.outputs, s.audio.mic], paint)];
  paint();
  return () => off.forEach((fn) => fn());
}
