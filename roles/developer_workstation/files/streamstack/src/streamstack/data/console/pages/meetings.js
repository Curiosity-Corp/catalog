// pages/meetings.js — CALENDAR-AWARE SETUP, mounted by the Auto page.
//
//   mountMeetings(el, ctx)  ->  cleanup
//
// Shows the next meetings from Microsoft 365 and calendar links, the steps that get the rig ready a couple of minutes
// before the start, and what the last preparation did. Everything comes from `state.meetings` (docs/meetings.md) and goes
// out as `meetings.*` actions; the sign-in code is shown here, and approved on the Microsoft page.

import { h, renderList } from '../core/util.js';

const TEMPLATE = `
<ss-card class="mt" heading="Meetings" icon="clock" data-ref="meetings">
  <ss-toggle slot="actions" label="Get ready" tone="green" data-ref="enabled"></ss-toggle>
  <p class="hint" data-ref="keyringNote" role="status" hidden></p>
  <div class="mt-next" data-ref="next" role="status"></div>
  <ul class="mt-list" data-ref="list" aria-label="Upcoming meetings"></ul>
  <p class="hint" data-ref="empty">No meetings found yet. Sign in to Microsoft 365 or add a calendar link below.</p>
  <div class="mt-prep" data-ref="prep" role="status" hidden>
    <strong data-ref="prepTitle"></strong>
    <ul class="mt-steps" data-ref="prepSteps"></ul>
  </div>
  <div class="mt-opts">
    <label class="mt-field"><span class="eyebrow">Get ready this many minutes before</span>
      <input class="text-input" data-ref="lead" type="number" min="0" max="30" step="1" aria-label="Minutes before the meeting"></label>
    <label class="mt-check"><input type="checkbox" data-ref="onlyOnline"> Only meetings with a call link</label>
    <fieldset class="mt-steps-set"><legend class="eyebrow">What to prepare</legend><div data-ref="stepBoxes"></div></fieldset>
    <ss-button size="sm" data-ref="prepareNow">Prepare now</ss-button>
  </div>
  <h3 class="eyebrow">Calendars</h3>
  <ul class="mt-sources" data-ref="sources" aria-label="Calendars"></ul>
  <div class="mt-code" data-ref="codeBox" role="status" hidden>
    <p>Open <a data-ref="codeLink" target="_blank" rel="noopener noreferrer"></a> and enter <b class="mono" data-ref="codeText"></b></p>
  </div>
  <div class="mt-add">
    <ss-button size="sm" data-ref="connect">Sign in to Microsoft 365</ss-button>
    <form class="mt-ics" data-ref="icsForm" autocomplete="off">
      <input class="text-input" data-ref="icsLink" type="url" placeholder="Calendar link (https://...ics)" aria-label="Calendar link">
      <input class="text-input" data-ref="icsName" type="text" maxlength="40" placeholder="Name (optional)" aria-label="Calendar name">
      <ss-button size="sm" icon="plus" data-ref="icsAdd">Add link</ss-button>
    </form>
  </div>
  <ol class="mt-events" data-ref="events" aria-label="What the calendar did"></ol>
</ss-card>`;

const hhmm = (epoch) => new Date(epoch * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const PREP_LABEL = { waiting: 'Waiting', due: 'Due now', done: 'Ready' };
const STATUS_LABEL = {
  connected: 'Connected', pending: 'Waiting for sign-in', 'signed-out': 'Not signed in', error: 'Problem', waiting: 'Not read yet',
};

export function mountMeetings(el, ctx) {
  const { store, act } = ctx;
  el.insertAdjacentHTML('afterbegin', TEMPLATE);
  const root = el.querySelector('[data-ref="meetings"]');
  const R = { meetings: root };
  root.querySelectorAll('[data-ref]').forEach((n) => { R[n.dataset.ref] = n; });
  const off = [];
  const state = () => store.state.meetings;

  R.enabled.addEventListener('change', (e) => act('meetings.enable', { on: e.detail.checked }));
  R.lead.addEventListener('change', () => act('meetings.settings', { lead_minutes: Number(R.lead.value) }));
  R.onlyOnline.addEventListener('change', () => act('meetings.settings', { only_online: R.onlyOnline.checked }));
  R.prepareNow.addEventListener('click', () => act('meetings.prepare', {}));
  R.connect.addEventListener('click', () => act('meetings.m365.connect', {}));
  R.icsForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const res = await act('meetings.ics.add', { link: R.icsLink.value.trim(), name: R.icsName.value.trim() });
    if (res) { R.icsLink.value = ''; R.icsName.value = ''; }
  });
  R.icsAdd.addEventListener('click', () => R.icsForm.requestSubmit());

  const stepsOn = () => state().steps.filter((s) => s.on).map((s) => s.id);
  const buildStep = (s) => {
    const box = h('input', { type: 'checkbox', 'data-step': s.id });
    box.addEventListener('change', () => {
      const on = new Set(stepsOn());
      if (box.checked) on.add(s.id); else on.delete(s.id);
      act('meetings.settings', { steps: [...on] });
    });
    const node = h('label', { class: 'mt-check', 'data-id': s.id }, box, ' ', h('span'));
    node._box = box;
    return node;
  };
  const buildSource = (s) => {
    const name = h('b', { class: 'mt-src-name' });
    const status = h('span', { class: 'mt-badge', role: 'status' });
    const detail = h('span', { class: 'mt-src-detail' });
    const action = h('button', { type: 'button', class: 'mt-mini' });
    action.addEventListener('click', () => {
      const s2 = state().sources.find((x) => x.id === s.id);
      if (!s2) return;
      if (s2.kind === 'ics') act('meetings.ics.remove', { id: s2.id });
      else act(s2.status === 'pending' ? 'meetings.m365.cancel' : 'meetings.m365.disconnect', {});
    });
    const node = h('li', { class: 'mt-src', 'data-id': s.id }, name, status, detail, action);
    node._r = { name, status, detail, action };
    return node;
  };
  const buildMeeting = (m) => {
    const when = h('time', { class: 'mono' });
    const title = h('span', { class: 'mt-title' });
    const badge = h('span', { class: 'mt-badge' });
    const node = h('li', { class: 'mt-row', 'data-id': m.id }, when, title, badge);
    node._r = { when, title, badge };
    return node;
  };

  const render = () => {
    const s = state();
    R.meetings.hidden = !s.available;
    R.enabled.checked = s.enabled;
    R.keyringNote.hidden = s.keyring.ok;
    R.keyringNote.textContent = s.keyring.ok ? '' : `Sign-in cannot be kept on this computer: ${s.keyring.reason}`;
    R.next.textContent = s.next
      ? `Next: ${s.next.title} at ${hhmm(s.next.start)}${s.in_call ? '. A call is on now, nothing will change.' : ''}`
      : 'No meeting coming up.';
    R.empty.hidden = s.upcoming.length > 0;
    renderList(R.list, s.upcoming, (m) => m.id, buildMeeting, (node, m) => {
      node._r.when.textContent = hhmm(m.start);
      node._r.title.textContent = m.title;
      node._r.badge.textContent = PREP_LABEL[m.prep] || m.prep;
      node._r.badge.dataset.prep = m.prep;
    });
    if (document.activeElement !== R.lead) R.lead.value = String(s.lead_minutes);
    R.lead.min = String(s.limits.lead_minutes[0]);
    R.lead.max = String(s.limits.lead_minutes[1]);
    R.onlyOnline.checked = s.only_online;
    renderList(R.stepBoxes, s.steps, (x) => x.id, buildStep, (node, x) => {
      node.querySelector('span').textContent = x.label;
      node._box.checked = x.on;
    });
    renderList(R.sources, s.sources, (x) => x.id, buildSource, (node, x) => {
      const r = node._r;
      r.name.textContent = x.name + (x.account ? ` (${x.account})` : '');
      r.status.textContent = STATUS_LABEL[x.status] || x.status;
      r.status.dataset.status = x.status;
      r.detail.textContent = x.detail || (x.count ? `${x.count} meetings` : '');
      r.action.textContent = x.kind === 'ics' ? 'Remove' : x.status === 'pending' ? 'Cancel' : x.status === 'signed-out' || x.status === 'error' ? '' : 'Sign out';
      r.action.hidden = r.action.textContent === '';
    });
    const m365 = s.sources.find((x) => x.kind === 'm365');
    R.connect.hidden = !m365 || m365.status === 'connected' || m365.status === 'pending';
    R.codeBox.hidden = !(m365 && m365.code);
    if (m365 && m365.code) {
      R.codeLink.textContent = m365.code.verification_uri;
      R.codeLink.href = m365.code.verification_uri;
      R.codeText.textContent = m365.code.user_code;
    }
    R.prep.hidden = s.prep.state === 'idle';
    R.prepTitle.textContent = s.prep.state === 'idle' ? '' : `${s.prep.meeting}: ${s.prep.text}`;
    R.prep.dataset.state = s.prep.state;
    R.prepSteps.replaceChildren(...s.prep.steps.map((x) => h('li', { 'data-ok': String(x.ok) }, `${x.label}: ${x.text}`)));
    R.events.replaceChildren(...s.events.slice(0, 5).map((e) => h('li', { 'data-level': e.level }, h('time', {}, e.time), ' ', e.text)));
  };

  off.push(store.subscribe((st) => st.meetings, render));
  render();
  return () => { for (const f of off) f(); root.remove(); };
}
