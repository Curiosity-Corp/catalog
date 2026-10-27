// pages/system-hardware.js — the hardware StreamStack found and the rig profile it resolved, on the System page.
//
// state.hardware is the probe's snapshot resolved into a Plan (docs/adr/0003): the rig that was chosen and why, each
// device the rig expects as present, absent or denied, the GPUs with the roles they were given, and the audio
// interface with the rate and format the chain will use. Only the demo rig offers a switch between the recorded
// hosts, so the card can be seen with each studio layout.

import { h } from '../core/util.js';

const TEMPLATE = `
<section class="hw" aria-labelledby="hHardware" data-testid="hardware">
  <h2 class="sec" id="hHardware">Hardware <span class="sec-note" data-ref="note"></span></h2>
  <div class="hw-card">
    <div class="hw-row">
      <div class="hw-text">
        <strong data-ref="rig" data-testid="hw-rig">Probing…</strong>
        <span class="hw-sub" data-ref="why" data-testid="hw-why"></span>
      </div>
      <span class="hw-actions">
        <span class="hw-hosts" data-ref="hosts" role="group" aria-label="Recorded host (demo)" hidden></span>
        <button type="button" class="copy" data-ref="reprobe" aria-label="Probe the hardware again"><span>Probe again</span></button>
      </span>
    </div>
    <p class="hw-error" data-ref="error" role="alert" hidden></p>
    <ul class="hw-devices" data-ref="devices" aria-label="Devices"></ul>
  </div>
  <div class="hw-card" data-testid="hw-video">
    <h3 class="hw-h">Video in use</h3>
    <ul class="hw-jobs" data-ref="jobs" aria-label="Which GPU does what"></ul>
    <ul class="hw-cams" data-ref="cams" aria-label="Cameras and virtual cameras"></ul>
    <p class="hw-sub" data-ref="loop" data-testid="hw-loopback"></p>
  </div>
  <div class="hw-card">
    <h3 class="hw-h">Graphics</h3>
    <ul class="hw-gpus" data-ref="gpus" aria-label="Graphics processors"></ul>
    <p class="hw-sub" data-ref="encoder" data-testid="hw-encoder"></p>
  </div>
  <div class="hw-card">
    <h3 class="hw-h">Audio</h3>
    <p class="hw-line" data-ref="audio" data-testid="hw-audio"></p>
    <p class="hw-sub" data-ref="mic" data-testid="hw-mic"></p>
  </div>
  <ul class="hw-notices" data-ref="notices" aria-label="Hardware notes"></ul>
</section>`;

const STATE_WORD = { present: 'Present', absent: 'Absent', denied: 'No access' };
const VIA_WORD = {
  env: 'set by STREAMSTACK_RIG',
  overlap: 'chosen by the devices found',
  identity: 'chosen by this machine, after a tie',
  fallback: 'no studio rig matched, generic Linux defaults',
};
const NODE_VIA = {
  override: 'set by hand',
  rig: 'by the rig profile',
  profile: 'by the GPU profile',
  generic: 'no profile for this GPU',
  fallback: 'no profile matched, first suitable GPU',
  none: 'no GPU chosen, the encoder trial decides',
};
const ROLE_ORDER = ['grade', 'encode', 'decode', 'blur'];

const codecs = (list) => (list.length ? list.join(', ') : 'none');

export async function mountHardwarePanel(host, ctx) {
  const { store, act, toast } = ctx;
  await ctx.ensureCss(new URL('./system-hardware.css', import.meta.url).href);
  const frag = document.createElement('template');
  frag.innerHTML = TEMPLATE;
  const sec = frag.content.firstElementChild;
  host.append(sec);
  const R = {};
  sec.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });

  R.reprobe.addEventListener('click', async () => {
    const res = await act('hardware.refresh', {});
    if (res && res.message) toast(res.message, { level: 'info', timeout: 2500 });
  });
  R.hosts.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-host]');
    if (b) act('hardware.demo_host', { host: b.dataset.host });
  });

  const renderVideo = (v) => {
    if (!v) {
      R.jobs.replaceChildren();
      R.cams.replaceChildren();
      R.loop.textContent = '';
      return;
    }
    R.jobs.replaceChildren(...v.jobs.map((j) => h('li', {
      class: 'hw-job', 'data-consumer': j.consumer, 'data-role': j.role, 'data-node': j.node || '', 'data-pci': j.pci || '',
    },
    h('span', { class: 'hw-name' }, j.label),
    h('span', { class: 'hw-roles' }, j.role),
    h('code', { class: 'hw-node' }, j.node || 'any'),
    h('span', { class: 'hw-sub' }, `${j.name || 'no GPU'}, ${NODE_VIA[j.via] || j.via}`))));
    R.cams.replaceChildren(...v.cameras.map((c) => h('li', {
      class: 'hw-cam', 'data-camera': c.id, 'data-state': c.state, 'data-usb': c.usb || '', 'data-node': c.node || '',
    },
    h('span', { class: 'hw-badge' }, STATE_WORD[c.state] || c.state),
    h('span', { class: 'hw-name' }, c.label),
    h('code', { class: 'hw-node' }, c.usb || ''),
    h('span', { class: 'hw-sub' }, c.node ? `on ${c.node}` : 'no video node'))));
    const l = v.loopback;
    const at = (x) => `${x.label} on ${x.node || `${x.declared} (not created yet)`}`;
    const options = l.options_match === null ? 'module options not read' : l.options_match ? 'module options match the rig' : 'module options differ from the rig';
    R.loop.textContent = `Virtual cameras: ${at(l.camera)}; ${at(l.program)}; ${options}.`;
    R.loop.dataset.camera = l.camera.node || '';
    R.loop.dataset.program = l.program.node || '';
  };

  const render = (hw) => {
    R.note.textContent = hw.checked
      ? `${hw.hotplug.mode === 'udev' ? 'Watching for plug and unplug' : hw.demo ? 'Recorded hardware' : 'Probed on request'}`
      : '';
    R.rig.textContent = hw.rig ? hw.rig.label : (hw.checked ? 'No rig' : 'Probing…');
    R.rig.dataset.rig = hw.rig ? hw.rig.id : '';
    R.why.textContent = hw.rig ? VIA_WORD[hw.rig.via] || hw.rig.via : '';
    R.error.hidden = !hw.error;
    R.error.textContent = hw.error || '';

    R.hosts.hidden = !(hw.demo && hw.hosts.length > 1);
    R.hosts.replaceChildren(...hw.hosts.map((name) => h('button', {
      type: 'button', class: 'copy', 'data-host': name, 'aria-pressed': String(hw.host === name),
    }, name)));

    R.devices.replaceChildren(...hw.devices.map((d) => h('li', {
      class: 'hw-device', 'data-device': d.id, 'data-state': d.state, 'data-required': String(d.required),
    },
    h('span', { class: 'hw-badge' }, STATE_WORD[d.state] || d.state),
    h('span', { class: 'hw-name' }, d.label),
    h('span', { class: 'hw-sub' }, [d.required ? 'required' : 'optional', d.detail].filter(Boolean).join(', ')))));

    R.gpus.replaceChildren(...hw.gpus.map((g) => {
      const roles = ROLE_ORDER.filter((r) => g.roles.includes(r));
      return h('li', { class: 'hw-gpu', 'data-pci': g.pci_id, 'data-roles': roles.join(' ') },
        h('span', { class: 'hw-name' }, g.name || g.pci_id),
        h('code', { class: 'hw-node' }, g.node),
        h('span', { class: 'hw-roles' }, roles.length ? roles.join(', ') : 'unused'),
        h('span', { class: 'hw-sub' }, g.probed
          ? `encodes ${codecs(g.encode)}; decodes ${codecs(g.decode)}`
          : 'VA-API was not probed'));
    }));
    if (!hw.gpus.length) R.gpus.replaceChildren(h('li', { class: 'hw-sub' }, hw.checked ? 'No render node found; the CPU does the video work.' : ''));
    R.encoder.textContent = hw.encoder
      ? `Encoder: ${hw.encoder.ffmpeg} on ${hw.encoder.hardware ? hw.encoder.node : 'the CPU'}${hw.encoder.reason ? ` (${hw.encoder.reason})` : ''}`
      : '';

    renderVideo(hw.video);

    const a = hw.audio;
    if (a) {
      const iface = a.interface ? a.interface.label : 'No audio interface';
      const fmt = a.format ? `, ${a.format}` : '';
      R.audio.textContent = `${iface}: ${a.rate} Hz${fmt}${a.resamples ? ' (resampled for calls)' : ''}`;
      R.audio.dataset.interface = a.interface ? a.interface.id : '';
      R.audio.dataset.rate = String(a.rate);
      R.audio.dataset.format = a.format || '';
      R.mic.textContent = `Microphone: ${a.microphone || 'none'}; monitor: ${a.monitor || 'none'}`;
    } else {
      R.audio.textContent = '';
      R.mic.textContent = '';
    }

    R.notices.replaceChildren(...hw.notices.map((n) => h('li', { class: 'hw-notice', 'data-level': n.level, 'data-id': n.id }, n.text)));
  };
  const off = store.subscribe((s) => s.hardware, render);
  render(store.state.hardware);
  return () => { off(); sec.remove(); };
}
