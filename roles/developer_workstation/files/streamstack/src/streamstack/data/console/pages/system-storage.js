// pages/system-storage.js — files StreamStack had to repair, on the System page.
//
// state.storage.warnings lists every file the crash-safe store set aside (damaged), restored from its last good
// copy, or read with lines ignored. Each notice says what happened and can be dismissed. The section is hidden
// while there is nothing to report.

import { h } from '../core/util.js';

const ACTION_TEXT = {
  'restored-backup': 'Was damaged. The last good copy was restored.',
  defaults: 'Was damaged. It was set aside and the defaults are used.',
  'ignored-lines': 'Some lines could not be read and were ignored.',
  unreadable: 'Could not be read.',
};

const TEMPLATE = `
<section class="stor" aria-labelledby="hStorage" hidden>
  <h2 class="sec" id="hStorage">Repaired files <span class="sec-note" data-ref="note"></span></h2>
  <ul class="stor-list" data-ref="list"></ul>
</section>`;

export async function mountStoragePanel(host, ctx) {
  const { store, act } = ctx;
  await ctx.ensureCss(new URL('./system-storage.css', import.meta.url).href);
  const frag = document.createElement('template');
  frag.innerHTML = TEMPLATE;
  const sec = frag.content.firstElementChild;
  const hero = host.querySelector('.hero');
  if (hero) hero.after(sec); else host.prepend(sec);
  const R = {};
  sec.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });

  const render = (warnings) => {
    sec.hidden = !warnings.length;
    R.note.textContent = warnings.length ? 'Nothing was lost silently; damaged copies are kept next to the file' : '';
    R.list.replaceChildren(...warnings.map((w) => {
      const dismiss = h('button', { type: 'button', class: 'copy', 'aria-label': `Dismiss the notice about ${w.path}` }, 'Dismiss');
      dismiss.addEventListener('click', () => act('storage.dismiss', { id: w.id }));
      return h('li', { class: 'stor-item', 'data-action': w.action },
        h('div', { class: 'stor-text' },
          h('span', { class: 'stor-path' }, w.path),
          h('span', { class: 'stor-what' }, `${ACTION_TEXT[w.action] || w.problem}${w.quarantined_to ? ` Kept as ${w.quarantined_to}.` : ''}`)),
        dismiss);
    }));
  };
  const off = store.subscribe((s) => s.storage.warnings, render);
  render(store.state.storage.warnings);
  return () => { off(); sec.remove(); };
}
