// pages/system-bridge.js — the Canon bridge on the System page: which decode path is running (GPU or CPU), the
// picture size in and out, frames per second, how long a frame spends in the bridge, CPU use of each process, and
// every restart. All numbers come from state.video.canon, measured by the bridge itself.

import { h } from '../core/util.js';

const STATE_PILL = {
  running: { state: 'ok', text: 'Running' },
  paused: { state: 'info', text: 'Changing a setting' },
  starting: { state: 'info', text: 'Starting' },
  stalled: { state: 'warn', text: 'Waiting for the camera' },
  offline: { state: 'fail', text: 'Offline' },
  stopped: { state: 'fail', text: 'Stopped' },
};

const TEMPLATE = `
<section class="cbr" aria-labelledby="hCanonBridge" hidden>
  <h2 class="sec" id="hCanonBridge">Canon bridge <span class="pill" data-ref="pill"></span><span class="sec-note" data-ref="note"></span></h2>
  <dl class="cbr-stats" data-ref="stats">
    <div class="cbr-stat" data-ref="sPath"><dt>Decoding on</dt><dd data-ref="path">–</dd></div>
    <div class="cbr-stat"><dt>Picture in</dt><dd data-ref="pin">–</dd></div>
    <div class="cbr-stat"><dt>Picture out</dt><dd data-ref="pout">–</dd></div>
    <div class="cbr-stat"><dt>Frames/s in → out</dt><dd data-ref="fps">–</dd></div>
    <div class="cbr-stat"><dt>Camera grab</dt><dd data-ref="grab" title="Time between frames from the camera">–</dd></div>
    <div class="cbr-stat"><dt>Hand-off to ffmpeg</dt><dd data-ref="pipe" title="Time to write one frame to ffmpeg; it grows when ffmpeg falls behind">–</dd></div>
    <div class="cbr-stat"><dt>CPU, bridge + ffmpeg</dt><dd data-ref="cpu">–</dd></div>
    <div class="cbr-stat"><dt>Restarts</dt><dd data-ref="restarts">–</dd></div>
  </dl>
  <ul class="cbr-tried" data-ref="tried" aria-label="Decode paths tried"></ul>
  <p class="hint cbr-err" data-ref="err" data-state="warn" hidden></p>
</section>`;

const pct = (v) => (v === null || v === undefined ? '–' : `${v.toFixed(0)}%`);

export async function mountBridgePanel(host, ctx) {
  const { store } = ctx;
  await ctx.ensureCss(new URL('./system-bridge.css', import.meta.url).href);
  const frag = document.createElement('template');
  frag.innerHTML = TEMPLATE;
  const sec = frag.content.firstElementChild;
  const hero = host.querySelector('.hero');
  if (hero) hero.after(sec); else host.prepend(sec);
  const R = {};
  sec.querySelectorAll('[data-ref]').forEach((el) => { R[el.dataset.ref] = el; });

  const render = () => {
    const c = store.state.video.canon;
    sec.hidden = c.state === 'disabled';
    const pill = STATE_PILL[c.state] || { state: '', text: c.state };
    R.pill.dataset.state = pill.state;
    R.pill.textContent = pill.text;
    R.note.textContent = c.state === 'offline' || c.state === 'stalled' ? (c.message || '') : (c.session === 'shared' ? 'one shared camera session' : c.session === 'pause' ? 'gphoto2 command (pauses for changes)' : '');
    const d = c.decode;
    R.path.textContent = d.label || 'not running';
    R.sPath.dataset.kind = d.path === 'cpu' || d.path === 'legacy' ? 'cpu' : d.path ? 'gpu' : '';
    R.pin.textContent = c.input ? `${c.input.width}×${c.input.height} ${c.input.subsampling}` : '–';
    R.pout.textContent = c.output ? `${c.output.width}×${c.output.height} (${c.output.mode === 'native' ? 'native aspect' : 'cropped to fill'})` : '–';
    R.fps.textContent = c.state === 'offline' ? '–' : `${c.fps_in.toFixed(1)} → ${c.fps_out.toFixed(1)}`;
    R.grab.textContent = c.grab_ms ? `${c.grab_ms.toFixed(0)} ms` : '–';
    R.pipe.textContent = c.pipe_ms ? `${c.pipe_ms.toFixed(1)} ms` : '–';
    const cpu = c.cpu || {};
    R.cpu.textContent = cpu.total === undefined || cpu.total === null ? '–' : `${pct(cpu.bridge ?? 0)} + ${pct(cpu.ffmpeg ?? 0)}`;
    R.cpu.title = `of one core. gphoto2 ${pct(cpu.gphoto2)}, total ${pct(cpu.total)}`;
    R.restarts.textContent = `${c.restarts.source} camera, ${c.restarts.encoder} ffmpeg, ${c.stalls} stalls`;
    R.tried.replaceChildren(...d.tried.map((t) => h('li', { 'data-ok': String(t.ok) }, `${t.name}: ${t.ok ? 'works' : t.reason || 'failed'}`)));
    R.err.hidden = !c.last_error;
    R.err.textContent = c.last_error ? `Last problem: ${c.last_error}` : '';
  };
  const off = store.subscribe((s) => s.video.canon, render);
  render();
  return () => { off(); sec.remove(); };
}
