// core/ui.js — tiny non-server UI state shared by keys, palette and pages (not part of the rig snapshot).
//   ui.grid            thirds overlay on the preview (key G)
//   onUi(cb)           cb(key, value) when something changes; returns unsubscribe
//   setUi(key, value)

export const ui = { grid: false };
const listeners = new Set();

export function setUi(key, value) {
  if (ui[key] === value) return;
  ui[key] = value;
  for (const cb of [...listeners]) cb(key, value);
}

export function onUi(cb) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}
