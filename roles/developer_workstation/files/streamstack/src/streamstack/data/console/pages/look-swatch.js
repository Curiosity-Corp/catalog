// pages/look-swatch.js — a tiny test chart with a look's values applied, so every look card shows what it does.
//
//   drawSwatch(canvas, values)      canvas 64×40 (any size works); values = a look ({brightness, …, lut_enabled})
//   sameLook(a, b)                  true when two looks are the same to the precision the faders show
//
// The transform mirrors the engine's colour grade closely enough for a thumbnail: brightness adds,
// contrast scales around mid grey, gamma bends the mid-tones, saturation scales the colour distance from
// luma, warmth pushes red up and blue down. LUT adds a teal/orange split-tone, sharpen draws a crisp edge.

const clamp01 = (v) => Math.min(1, Math.max(0, v));

const BARS = [[1, 1, 1], [1, 0.9, 0.15], [0.1, 0.85, 0.9], [0.15, 0.8, 0.25], [0.85, 0.2, 0.8], [0.9, 0.18, 0.15], [0.2, 0.3, 0.9]];
const SKIN = [0.86, 0.62, 0.5];

function grade([r, g, b], v) {
  const bright = Number(v.brightness) || 0;
  const contrast = Number(v.contrast) || 0;
  const gamma = Number(v.gamma) || 0;
  const sat = Number(v.saturation) || 0;
  const warm = Number(v.warmth) || 0;
  const lutOn = !!v.lut_enabled;
  const lut = lutOn ? Number(v.lut_amount) || 0 : 0;
  let c = [r, g, b].map((x) => clamp01(x ** (1 / (1 + gamma * 0.9))));
  c = c.map((x) => x + bright);
  c = c.map((x) => (x - 0.5) * (1 + contrast * 1.1) + 0.5);
  const l = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  c = c.map((x) => l + (x - l) * (1 + sat));
  c[0] += warm * 0.09;
  c[2] -= warm * 0.11;
  if (lut) {
    const lum = clamp01(0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]);
    c[0] += (lum - 0.5) * 0.18 * lut;       // highlights lean orange
    c[1] += (0.5 - Math.abs(lum - 0.5)) * 0.04 * lut;
    c[2] += (0.5 - lum) * 0.2 * lut;        // shadows lean teal
  }
  return c.map((x) => Math.round(clamp01(x) * 255));
}

export function drawSwatch(canvas, values) {
  const w = canvas.width;
  const h = canvas.height;
  const g = canvas.getContext('2d');
  const img = g.createImageData(w, h);
  const barsH = Math.round(h * 0.58);
  const rampW = Math.round(w * 0.64);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      let base;
      if (y < barsH) base = BARS[Math.min(BARS.length - 1, Math.floor((x / w) * BARS.length))];
      else if (x < rampW) { const t = x / (rampW - 1); base = [t, t, t]; }
      else base = SKIN;
      const [r, gg, b] = grade(base, values);
      const p = (y * w + x) * 4;
      img.data[p] = r; img.data[p + 1] = gg; img.data[p + 2] = b; img.data[p + 3] = 255;
    }
  }
  g.putImageData(img, 0, 0);
  if (values.sharpen_enabled) {                        // a crisp white tick on the bar edge, scaled by the amount
    g.fillStyle = `rgba(255,255,255,${(0.25 + 0.7 * (Number(values.sharpen_amount) || 0)).toFixed(2)})`;
    for (let i = 1; i < BARS.length; i++) g.fillRect(Math.round((i / BARS.length) * w), 0, 1, barsH);
  }
}

const NUMBERS = ['brightness', 'contrast', 'gamma', 'saturation', 'warmth'];

export function sameLook(a, b) {
  if (!a || !b) return false;
  for (const k of NUMBERS) if (Math.abs((a[k] ?? 0) - (b[k] ?? 0)) > 0.006) return false;
  for (const [flag, amount] of [['sharpen_enabled', 'sharpen_amount'], ['lut_enabled', 'lut_amount']]) {
    if (!!a[flag] !== !!b[flag]) return false;
    if (a[flag] && Math.abs((a[amount] ?? 0) - (b[amount] ?? 0)) > 0.011) return false;
  }
  return true;
}
