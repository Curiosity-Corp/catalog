// pages/placeholder.js — the “coming in this release” page for routes whose real page is not built yet.
// Reads its text from the registry entry (ctx.page.blurb / ctx.page.features), so replacing a page is a
// one-line change in pages/index.js.

import { iconHTML } from '../components/icons.js';
import { esc } from '../core/util.js';

export async function mount(host, ctx) {
  const { page } = ctx;
  await ctx.ensureCss(new URL('./placeholder.css', import.meta.url).href);
  host.innerHTML = `
    <div class="ph">
      <div class="ph-art" aria-hidden="true">${iconHTML(page.icon)}</div>
      <p class="eyebrow">Coming in this release</p>
      <h1>${esc(page.title)}</h1>
      <p class="ph-blurb">${esc(page.blurb)}</p>
      ${page.features ? `<ul>${page.features.map((f) => `<li>${iconHTML('check')}<span>${esc(f)}</span></li>`).join('')}</ul>` : ''}
      <a class="ph-back" href="#/live">${iconHTML('live')}Back to Live</a>
    </div>`;
  document.title = `${page.title} · StreamStack`;
}
