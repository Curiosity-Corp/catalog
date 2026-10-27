// pages/system-diagnostics.js — telemetry switch and crash reports, on the System page.
//
// state.diagnostics.telemetry says whether the opt-in Prometheus endpoint is on and where it answers; the scrape
// token never appears on the page (it only reaches the clipboard, inside a ready-made scrape config).
// state.diagnostics.crashes lists the newest crash reports; each links to its redacted JSON.

import { apiUrl } from '../core/api.js';
import { h } from '../core/util.js';

const TEMPLATE = `
<section class="diag" aria-labelledby="hDiag">
  <h2 class="sec" id="hDiag">Telemetry and crash reports <span class="sec-note">Local only; nothing is sent anywhere</span></h2>
  <div class="diag-card">
    <div class="diag-row">
      <div class="diag-text">
        <strong>Prometheus metrics</strong>
        <span class="diag-sub" data-ref="status"></span>
      </div>
      <button type="button" class="diag-switch" role="switch" aria-checked="false" aria-label="Prometheus metrics" data-ref="toggle"><span class="diag-knob"></span></button>
    </div>
    <div class="diag-on" data-ref="on" hidden>
      <div class="diag-row">
        <code class="diag-url" data-ref="url"></code>
        <button type="button" class="copy" data-ref="copyUrl" aria-label="Copy the metrics address"><span>Copy address</span></button>
      </div>
      <div class="diag-row">
        <span class="diag-sub" data-ref="scrapes"></span>
        <span class="diag-actions">
          <button type="button" class="copy" data-ref="copyConfig" aria-label="Copy a Prometheus scrape config with the scrape token"><span>Copy scrape config</span></button>
          <button type="button" class="copy" data-ref="rotate" aria-label="Create a new scrape token"><span>New token</span></button>
        </span>
      </div>
    </div>
  </div>
  <div class="diag-card">
    <div class="diag-row">
      <div class="diag-text">
        <strong>Crash reports</strong>
        <span class="diag-sub" data-ref="crashNote"></span>
      </div>
      <button type="button" class="copy" data-ref="clear" aria-label="Remove all crash reports" hidden><span>Clear</span></button>
    </div>
    <ul class="diag-list" data-ref="crashes"></ul>
  </div>
</section>`;

async function copyText(text) {
  if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return; }
  const ta = h('textarea', { 'aria-hidden': 'true', readonly: '' });
  ta.value = text;
  document.body.append(ta);
  ta.select();
  const ok = document.execCommand('copy');
  ta.remove();
  if (!ok) throw new Error('copy command was refused');
}

const when = (t) => (Number.isFinite(t) ? new Date(t * 1000).toLocaleString() : 'unknown time');

export async function mountDiagnosticsPanel(host, ctx) {
  const { store, act, toast } = ctx;
  await ctx.ensureCss(new URL('./system-diagnostics.css', import.meta.url).href);
  const frag = document.createElement('template');
  frag.innerHTML = TEMPLATE;
  const sec = frag.content.firstElementChild;
  const anchor = host.querySelector('.stor') || host.querySelector('.hero');
  if (anchor) anchor.after(sec); else host.prepend(sec);
  const R = {};
  sec.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });

  const flash = (btn, text) => {
    const label = btn.querySelector('span');
    const before = label.textContent;
    label.textContent = text;
    btn.dataset.done = 'true';
    setTimeout(() => { btn.dataset.done = 'false'; label.textContent = before; }, 1500);
  };
  const copyWith = (btn, getText, done) => btn.addEventListener('click', async () => {
    try {
      const text = await getText();
      if (text === null) return;
      await copyText(text);
      flash(btn, done);
    } catch (err) {
      toast(`Could not copy: ${err.message}`, { level: 'error' });
    }
  });

  R.toggle.addEventListener('click', () => {
    act('telemetry.set', { on: R.toggle.getAttribute('aria-checked') !== 'true' }, { quiet: false });
  });
  copyWith(R.copyUrl, () => R.url.textContent, 'Copied');
  copyWith(R.copyConfig, async () => {
    const res = await act('telemetry.scrape_config', {});
    return res && typeof res.message === 'string' ? res.message : null;
  }, 'Copied');
  R.rotate.addEventListener('click', async () => {
    const res = await act('telemetry.rotate_token', {});
    if (res && res.message) toast(res.message, { level: 'info', timeout: 4000 });
  });
  R.clear.addEventListener('click', async () => {
    const res = await act('crash.clear', {});
    if (res && res.message) toast(res.message, { level: 'info', timeout: 3000 });
  });

  const render = (d) => {
    const t = d.telemetry;
    const on = t.enabled === true;
    R.toggle.setAttribute('aria-checked', String(on));
    R.status.textContent = on
      ? 'On. The Console answers a scraper that presents the token; nothing leaves this computer.'
      : 'Off. Turn on to let your own Prometheus read service health, audio levels and timings.';
    R.on.hidden = !on;
    R.url.textContent = t.url || '';
    R.scrapes.textContent = t.scrapes
      ? `${t.scrapes} scrape${t.scrapes === 1 ? '' : 's'}, last ${when(t.last_scrape)}`
      : 'No scrape yet';
    const reports = d.crashes.reports;
    R.crashNote.textContent = reports.length ? `Kept in ${d.crashes.dir}; tokens and stream keys are removed` : 'None. Nothing has crashed.';
    R.clear.hidden = !reports.length;
    R.crashes.replaceChildren(...reports.map((r) => h('li', { class: 'diag-crash', 'data-kind': r.kind },
      h('div', { class: 'diag-text' },
        h('span', { class: 'diag-what' }, `${r.type || 'Error'}: ${r.message || ''}`),
        h('span', { class: 'diag-sub' }, `${r.component} ${r.kind} on ${when(r.time)}, version ${r.version}`)),
      h('a', { class: 'copy', href: apiUrl(`api/crashes/${encodeURIComponent(r.id)}.json`), target: '_blank', rel: 'noopener', 'aria-label': `Open the report for ${r.type || 'the error'}` }, 'Open'))));
  };
  const off = store.subscribe((s) => s.diagnostics, render);
  render(store.state.diagnostics);
  return () => { off(); sec.remove(); };
}
