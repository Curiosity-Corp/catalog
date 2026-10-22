// core/util.js — small, dependency-free helpers shared by core, components and pages.

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
export const lerp = (a, b, t) => a + (b - a) * t;

/** Deep structural equality for JSON-like values (what the store compares). */
export function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) {
    return Number.isNaN(a) && Number.isNaN(b);
  }
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (!deepEqual(a[i], b[i])) return false;
    return true;
  }
  const ka = Object.keys(a);
  if (ka.length !== Object.keys(b).length) return false;
  for (const k of ka) if (!(k in b) || !deepEqual(a[k], b[k])) return false;
  return true;
}

/** Read `a.b.c` from an object; undefined when any step is missing. */
export function getPath(obj, path) {
  let cur = obj;
  for (const k of path.split('.')) {
    if (cur === null || cur === undefined) return undefined;
    cur = cur[k];
  }
  return cur;
}

/** Write `a.b.c` into an object, creating missing objects. */
export function setPath(obj, path, value) {
  const keys = path.split('.');
  let cur = obj;
  for (let i = 0; i < keys.length - 1; i++) {
    if (typeof cur[keys[i]] !== 'object' || cur[keys[i]] === null) cur[keys[i]] = {};
    cur = cur[keys[i]];
  }
  cur[keys[keys.length - 1]] = value;
}

/**
 * Leading + trailing throttle. The returned function forwards at most one call per `ms`
 * and always delivers the final arguments. `.flush()` fires a pending call now,
 * `.cancel()` drops it.
 */
export function throttle(fn, ms) {
  let last = 0;
  let timer = 0;
  let pending = null;
  const run = () => {
    timer = 0;
    last = performance.now();
    const args = pending;
    pending = null;
    if (args) fn(...args);
  };
  const t = (...args) => {
    pending = args;
    const wait = ms - (performance.now() - last);
    if (wait <= 0) {
      clearTimeout(timer);
      run();
    } else if (!timer) {
      timer = setTimeout(run, wait);
    }
  };
  t.flush = () => { clearTimeout(timer); if (pending) run(); };
  t.cancel = () => { clearTimeout(timer); timer = 0; pending = null; };
  return t;
}

/** Create an element: `h('div', {class:'x', 'aria-label':'y', onclick}, child, 'text')`. */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v === null || v === undefined) continue;
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** Escape text for use inside an innerHTML template. */
export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** Parse an HTML string into a DocumentFragment (no inline styles, per the CSP). */
export function fragment(html) {
  const t = document.createElement('template');
  t.innerHTML = html;
  return t.content;
}

/**
 * Keyed list reconciliation: keeps existing nodes (and their focus) when only data changes.
 * `create(item)` builds a node, `update(node, item)` refreshes it.
 */
export function renderList(container, items, keyOf, create, update) {
  const existing = new Map();
  for (const child of Array.from(container.children)) {
    if (child.dataset && child.dataset.key !== undefined) existing.set(child.dataset.key, child);
  }
  let prev = null;
  for (const item of items) {
    const key = String(keyOf(item));
    let node = existing.get(key);
    if (node) existing.delete(key);
    else {
      node = create(item);
      node.dataset.key = key;
    }
    update(node, item);
    const want = prev ? prev.nextSibling : container.firstChild;
    if (want !== node) container.insertBefore(node, want);
    prev = node;
  }
  for (const stale of existing.values()) stale.remove();
}

/** "+3.5" / "-4.0" / "0.0" with a real minus sign. */
export function signed(v, decimals = 1) {
  if (!Number.isFinite(v)) return '–';
  const s = Math.abs(v).toFixed(decimals);
  if (Number(s) === 0) return s;
  return (v < 0 ? '\u2212' : '+') + s;
}

export const fmtDb = (v, decimals = 1) => (Number.isFinite(v) ? `${signed(v, decimals)} dB` : '–');

/** Time of day ("14:03") from an ISO-8601 local string. */
export function clockTime(iso) {
  if (!iso) return '';
  const m = /T(\d\d:\d\d)/.exec(iso);
  return m ? m[1] : '';
}

/** Run `fn` once the document is visible; returns a disposer. Used by pausing media. */
export function onVisibility(fn) {
  const h2 = () => fn(!document.hidden);
  document.addEventListener('visibilitychange', h2);
  return () => document.removeEventListener('visibilitychange', h2);
}

export const prefersReducedMotion = () =>
  window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
