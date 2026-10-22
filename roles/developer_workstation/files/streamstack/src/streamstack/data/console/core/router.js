// core/router.js — hash routing over the page registry (pages/index.js).
//
//   currentRoute()        -> the registry entry of the page on screen ({id, title, load, …})
//   navigate('audio')     -> sets location.hash
//   onRoute(cb)           -> cb(entry) on every change (and now); returns unsubscribe
//
// A page is mounted by app.js: `const mod = await entry.load(); cleanup = mod.mount(host, ctx)`.

import { pages, defaultPage } from '../pages/index.js';

const listeners = new Set();
let current = defaultPage;

function resolve() {
  const id = (location.hash.replace(/^#\/?/, '').split(/[/?]/)[0] || defaultPage.id).toLowerCase();
  return pages.find((p) => p.id === id) || defaultPage;
}

function onHash() {
  const next = resolve();
  current = next;
  for (const cb of [...listeners]) cb(next);
}

export const currentRoute = () => current;

export function navigate(id) {
  const target = `#/${id}`;
  if (location.hash === target) onHash();
  else location.hash = target;
}

export function onRoute(cb) {
  listeners.add(cb);
  cb(current);
  return () => listeners.delete(cb);
}

export function startRouter() {
  current = resolve();
  window.addEventListener('hashchange', onHash);
  onHash();
}
