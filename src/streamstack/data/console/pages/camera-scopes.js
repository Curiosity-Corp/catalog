// pages/camera-scopes.js — video scopes computed in the browser from the preview frames.
//
//   const scopes = new Scopes({ getImage, hist, scope, zebra });
//   scopes.configure({ scopes: true, zebra: false, mode: 'parade' | 'waveform' });
//   scopes.onStats = ({ clipHigh, clipLow, mean, ok }) => …      called after every sample
//   scopes.start(); … scopes.stop();
//
// How it works: every 200 ms (5 fps) the current frame of the preview <img> is drawn to a 256×144
// offscreen canvas (the stream is same-origin, so the canvas is never tainted), read back once, and
// reduced to: a luma histogram (plus R, G, B), an RGB parade or a luma waveform, and a zebra mask of
// pixels at or above ZEBRA_LUMA. Nothing is sampled while the tab is hidden, the preview has no
// picture, or neither the scopes nor the zebra are switched on.
//
// Canvases are plain <canvas> elements owned by the page; this module only draws into them.
// Colours are read from the design tokens (theme.css) so both themes stay correct.

const W = 256;
const H = 144;
const LEVELS = 128;                // vertical resolution of parade / waveform
const SAMPLE_MS = 200;
export const ZEBRA_LUMA = 240;     // ≈ 94 % IRE: highlights that are about to clip
const LOW_LUMA = 15;               // crushed shadows

const PARADE_RGB = [[255, 84, 84], [64, 226, 112], [92, 150, 255]];
const WAVE_RGB = [170, 255, 205];

function tokens(el) {
  const cs = getComputedStyle(el);
  const get = (name, fallback) => cs.getPropertyValue(name).trim() || fallback;
  return {
    text2: get('--text-2', '#a9b4c5'),
    text3: get('--text-3', '#8591a4'),
    line: get('--line-strong', 'rgba(255,255,255,0.16)'),
    inset: get('--surface-inset', '#0b0e13'),
    red: get('--red', '#ff4a5c'),
    amber: get('--amber', '#ffb83d'),
  };
}

export class Scopes {
  constructor({ getImage, hist, scope, zebra }) {
    this.getImage = getImage;
    this.hist = hist;
    this.scope = scope;
    this.zebra = zebra;
    this.opts = { scopes: true, zebra: false, mode: 'parade' };
    this.onStats = null;
    this.timer = 0;
    this.running = false;
    this.warned = false;

    this.sample = document.createElement('canvas');
    this.sample.width = W;
    this.sample.height = H;
    this.sctx = this.sample.getContext('2d', { willReadFrequently: true });

    this.mask = document.createElement('canvas');
    this.mask.width = W;
    this.mask.height = H;
    this.mctx = this.mask.getContext('2d');
    this.maskData = this.mctx.createImageData(W, H);

    this.trace = document.createElement('canvas');           // parade/waveform before scaling to the display
    this.tctx = this.trace.getContext('2d');

    this.stripes = this.makeStripes();
    this.tk = tokens(this.hist);
    this._theme = () => { this.tk = tokens(this.hist); this.redrawLast(); };
    this.last = null;
  }

  configure(opts) {
    Object.assign(this.opts, opts);
    if (!this.opts.zebra) this.clearZebra();
    this.redrawLast();
  }

  start() {
    if (this.running) return;
    this.running = true;
    window.addEventListener('ss:theme', this._theme);
    const tick = () => {
      if (!this.running) return;
      try { this.step(); } catch (err) {
        if (!this.warned) { this.warned = true; console.error('scopes sample failed', err); }
      }
      this.timer = setTimeout(tick, SAMPLE_MS);
    };
    tick();
  }

  stop() {
    this.running = false;
    clearTimeout(this.timer);
    window.removeEventListener('ss:theme', this._theme);
  }

  makeStripes() {
    const c = document.createElement('canvas');
    c.width = 16;
    c.height = 16;
    const g = c.getContext('2d');
    g.fillStyle = 'rgba(0,0,0,0.5)';
    g.fillRect(0, 0, 16, 16);
    g.strokeStyle = 'rgba(255,255,255,0.95)';
    g.lineWidth = 5;
    g.beginPath();
    for (let i = -16; i <= 32; i += 16) { g.moveTo(i, 16); g.lineTo(i + 16, 0); }
    g.stroke();
    return c;
  }

  /** One sample. Returns without drawing when there is nothing to show. */
  step() {
    const wantScopes = this.opts.scopes;
    const wantZebra = this.opts.zebra;
    if (document.hidden || (!wantScopes && !wantZebra)) return;
    const img = this.getImage();
    if (!img || img.hidden || !img.complete || !img.naturalWidth) {
      this.last = null;
      this.onStats?.({ ok: false });
      if (wantScopes) { this.drawEmpty(this.hist); this.drawEmpty(this.scope); }
      if (wantZebra) this.clearZebra();
      return;
    }
    this.sctx.drawImage(img, 0, 0, W, H);
    const { data } = this.sctx.getImageData(0, 0, W, H);

    const hist = new Uint32Array(256 * 4);                  // luma, r, g, b
    const parade = this.opts.mode === 'parade';
    const cells = parade ? new Uint16Array(3 * W * LEVELS) : new Uint16Array(W * LEVELS);
    const md = this.maskData.data;
    let high = 0;
    let low = 0;
    let sum = 0;
    for (let y = 0, p = 0; y < H; y++) {
      for (let x = 0; x < W; x++, p += 4) {
        const r = data[p];
        const g = data[p + 1];
        const b = data[p + 2];
        const l = (0.2126 * r + 0.7152 * g + 0.0722 * b + 0.5) | 0;
        hist[l]++;
        hist[256 + r]++;
        hist[512 + g]++;
        hist[768 + b]++;
        sum += l;
        if (l >= ZEBRA_LUMA) { high++; md[p + 3] = 255; } else md[p + 3] = 0;
        if (l <= LOW_LUMA) low++;
        if (wantScopes) {
          if (parade) {
            cells[x * LEVELS + (r >> 1)]++;
            cells[(W + x) * LEVELS + (g >> 1)]++;
            cells[(2 * W + x) * LEVELS + (b >> 1)]++;
          } else {
            cells[x * LEVELS + (l >> 1)]++;
          }
        }
      }
    }
    const total = W * H;
    const stats = { ok: true, clipHigh: high / total, clipLow: low / total, mean: sum / total / 255 };
    this.last = { hist, cells, parade, stats };
    this.onStats?.(stats);
    if (wantScopes) { this.drawHist(hist, stats); this.drawScope(cells, parade); }
    if (wantZebra) this.drawZebra();
  }

  redrawLast() {
    if (!this.last) return;
    const { hist, cells, parade, stats } = this.last;
    if (this.opts.scopes && parade === (this.opts.mode === 'parade')) { this.drawHist(hist, stats); this.drawScope(cells, parade); }
  }

  // ---- drawing helpers ----

  fit(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(2, Math.round(canvas.clientWidth * dpr));
    const h = Math.max(2, Math.round(canvas.clientHeight * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    const g = canvas.getContext('2d');
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, w, h);
    return { g, w, h, dpr };
  }

  drawEmpty(canvas) {
    const { g, w, h, dpr } = this.fit(canvas);
    g.fillStyle = this.tk.text3;
    g.font = `${11 * dpr}px ui-sans-serif, system-ui, sans-serif`;
    g.textAlign = 'center';
    g.fillText('No picture', w / 2, h / 2 + 4 * dpr);
  }

  graticule(g, w, h, dpr, labels) {
    g.lineWidth = Math.max(1, dpr * 0.6);
    g.strokeStyle = this.tk.line;
    g.fillStyle = this.tk.text3;
    g.font = `${9 * dpr}px ui-monospace, monospace`;
    g.textAlign = 'left';
    for (const f of [0, 0.25, 0.5, 0.75, 1]) {
      const y = Math.round((1 - f) * (h - 1)) + 0.5;
      g.globalAlpha = f === 0 || f === 1 ? 0.7 : 0.35;
      g.beginPath();
      g.moveTo(0, y);
      g.lineTo(w, y);
      g.stroke();
      g.globalAlpha = 1;
      if (labels) g.fillText(String(Math.round(f * 100)), 3 * dpr, Math.min(h - 2 * dpr, Math.max(9 * dpr, y - 2 * dpr)));
    }
  }

  drawHist(hist, stats) {
    const { g, w, h, dpr } = this.fit(this.hist);
    const bins = 128;
    // shadow / highlight zones: tinted when something is actually clipped
    const zone = 6 / 128;
    g.fillStyle = this.tk.red;
    g.globalAlpha = stats.clipHigh > 0.005 ? 0.22 : 0.07;
    g.fillRect(w * (1 - zone), 0, w * zone, h);
    g.fillStyle = this.tk.amber;
    g.globalAlpha = stats.clipLow > 0.02 ? 0.22 : 0.07;
    g.fillRect(0, 0, w * zone, h);
    g.globalAlpha = 1;

    const fold = (off) => {
      const out = new Float32Array(bins);
      for (let i = 0; i < 256; i++) out[i >> 1] += hist[off + i];
      return out;
    };
    const channels = [fold(0), fold(256), fold(512), fold(768)];
    let peak = 1;
    for (const c of channels) for (const v of c) peak = Math.max(peak, v);
    const yOf = (v) => h - 2 * dpr - (Math.sqrt(v / peak)) * (h - 6 * dpr);
    const path = (arr) => {
      const p = new Path2D();
      p.moveTo(0, h);
      for (let i = 0; i < bins; i++) p.lineTo((i + 0.5) / bins * w, yOf(arr[i]));
      p.lineTo(w, h);
      p.closePath();
      return p;
    };
    // luma as the solid shape
    g.fillStyle = this.tk.text2;
    g.globalAlpha = 0.5;
    g.fill(path(channels[0]));
    g.globalAlpha = 1;
    // R G B as thin outlines
    const cols = ['', ...PARADE_RGB.map(([r, gg, b]) => `rgb(${r},${gg},${b})`)];
    g.lineWidth = Math.max(1, dpr);
    for (let c = 1; c < 4; c++) {
      g.strokeStyle = cols[c];
      g.globalAlpha = 0.85;
      g.beginPath();
      for (let i = 0; i < bins; i++) {
        const x = (i + 0.5) / bins * w;
        if (i === 0) g.moveTo(x, yOf(channels[c][i])); else g.lineTo(x, yOf(channels[c][i]));
      }
      g.stroke();
    }
    g.globalAlpha = 1;
    // quarter lines
    g.strokeStyle = this.tk.line;
    g.globalAlpha = 0.4;
    g.lineWidth = Math.max(1, dpr * 0.6);
    for (const f of [0.25, 0.5, 0.75]) {
      const x = Math.round(f * w) + 0.5;
      g.beginPath();
      g.moveTo(x, 0);
      g.lineTo(x, h);
      g.stroke();
    }
    g.globalAlpha = 1;
  }

  drawScope(cells, parade) {
    const { g, w, h, dpr } = this.fit(this.scope);
    this.graticule(g, w, h, dpr, true);
    const tw = parade ? 3 * W : W;
    if (this.trace.width !== tw || this.trace.height !== LEVELS) { this.trace.width = tw; this.trace.height = LEVELS; }
    const img = this.tctx.createImageData(tw, LEVELS);
    const d = img.data;
    for (let col = 0; col < tw; col++) {
      const rgb = parade ? PARADE_RGB[(col / W) | 0] : WAVE_RGB;
      for (let lv = 0; lv < LEVELS; lv++) {
        const n = cells[col * LEVELS + lv];
        if (!n) continue;
        const a = Math.min(1, n * 0.28) ** 0.65;
        const p = ((LEVELS - 1 - lv) * tw + col) * 4;
        d[p] = rgb[0];
        d[p + 1] = rgb[1];
        d[p + 2] = rgb[2];
        d[p + 3] = (a * 255) | 0;
      }
    }
    this.tctx.putImageData(img, 0, 0);
    g.imageSmoothingEnabled = true;
    g.imageSmoothingQuality = 'medium';
    const left = parade ? 0 : 0;
    g.drawImage(this.trace, left, 0, tw, LEVELS, left, 0, w, h);
    if (parade) {
      g.strokeStyle = this.tk.line;
      g.globalAlpha = 0.7;
      g.lineWidth = Math.max(1, dpr);
      for (const f of [1 / 3, 2 / 3]) {
        const x = Math.round(f * w) + 0.5;
        g.beginPath();
        g.moveTo(x, 0);
        g.lineTo(x, h);
        g.stroke();
      }
      g.globalAlpha = 1;
    }
  }

  drawZebra() {
    const c = this.zebra;
    if (c.width !== 384) { c.width = 384; c.height = 216; }
    const g = c.getContext('2d');
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.globalCompositeOperation = 'source-over';
    g.clearRect(0, 0, c.width, c.height);
    this.mctx.putImageData(this.maskData, 0, 0);
    g.imageSmoothingEnabled = false;
    g.drawImage(this.mask, 0, 0, c.width, c.height);
    g.globalCompositeOperation = 'source-in';
    g.fillStyle = g.createPattern(this.stripes, 'repeat');
    g.fillRect(0, 0, c.width, c.height);
    g.globalCompositeOperation = 'source-over';
  }

  clearZebra() {
    const c = this.zebra;
    c.getContext('2d').clearRect(0, 0, c.width, c.height);
  }
}
