// components/icons.js — the icon set: 24×24, 1.75 stroke, round caps, `currentColor`.
//
//   icon('mic')               -> SVGSVGElement (aria-hidden, class "icon")
//   iconHTML('mic')           -> '<svg …>' string for innerHTML templates
//   ICON_NAMES                -> every available name
//
// Sizing is by CSS (the svg has class "icon"; give it width/height or font-size).

const P = {
  live: '<circle cx="12" cy="12" r="2.2"/><path d="M7.8 7.8a6 6 0 0 0 0 8.4M16.2 7.8a6 6 0 0 1 0 8.4M4.9 4.9a10 10 0 0 0 0 14.2M19.1 4.9a10 10 0 0 1 0 14.2"/>',
  camera: '<path d="M4 8h3l1.5-2h7L17 8h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="3.6"/>',
  audio: '<path d="M6 4v5M6 13v7M12 4v7M12 15v5M18 4v2M18 10v10"/><circle cx="6" cy="11" r="2"/><circle cx="12" cy="13" r="2"/><circle cx="18" cy="8" r="2"/>',
  auto: '<path d="M11 3.5l1.9 5.4 5.4 1.9-5.4 1.9L11 18.1l-1.9-5.4L3.7 10.8l5.4-1.9z"/><path d="M19 15.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"/>',
  system: '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7"/>',
  'mic-off': '<path d="M15 9.3V6a3 3 0 0 0-5.9-.8M9 9v2a3 3 0 0 0 4.6 2.5M5.5 11a6.5 6.5 0 0 0 10.6 5M18.5 11a6.5 6.5 0 0 1-.6 2.7M12 17.5V21M8.5 21h7M3.5 3.5l17 17"/>',
  eye: '<path d="M2.5 12s3.6-6.5 9.5-6.5S21.5 12 21.5 12s-3.6 6.5-9.5 6.5S2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/>',
  'eye-off': '<path d="M9.9 5.8A9 9 0 0 1 12 5.5c5.9 0 9.5 6.5 9.5 6.5a16 16 0 0 1-2.6 3.3M6.4 7.6A15.6 15.6 0 0 0 2.5 12S6.1 18.5 12 18.5a9 9 0 0 0 4.3-1.1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2M3.5 3.5l17 17"/>',
  record: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.4" fill="currentColor" stroke="none"/>',
  dot: '<circle cx="12" cy="12" r="5" fill="currentColor" stroke="none"/>',
  vcam: '<rect x="3" y="5.5" width="13.5" height="13" rx="2.5"/><path d="M16.5 10.5L21 8v8l-4.5-2.5"/>',
  broadcast: '<circle cx="12" cy="12" r="2" fill="currentColor"/><path d="M8.5 8.5a5 5 0 0 0 0 7M15.5 8.5a5 5 0 0 1 0 7M5.7 5.7a9 9 0 0 0 0 12.6M18.3 5.7a9 9 0 0 1 0 12.6"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5M5.3 5.3l1.8 1.8M16.9 16.9l1.8 1.8M5.3 18.7l1.8-1.8M16.9 7.1l1.8-1.8"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  grid: '<rect x="3" y="3" width="18" height="18" rx="2.5"/><path d="M9 3v18M15 3v18M3 9h18M3 15h18"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  alert: '<path d="M12 3.5l9.5 16.5h-19z"/><path d="M12 10v4.5M12 17.3h.01"/>',
  power: '<path d="M12 3v8"/><path d="M6.3 6.8a8 8 0 1 0 11.4 0"/>',
  play: '<path d="M7.5 4.5v15l12-7.5z" fill="currentColor"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2.5" fill="currentColor"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
  keyboard: '<rect x="2.5" y="6" width="19" height="12" rx="2.5"/><path d="M6.5 10h.01M10 10h.01M14 10h.01M17.5 10h.01M7.5 14h9"/>',
  command: '<path d="M9 9V6.5A2.5 2.5 0 1 0 6.5 9zM9 9h6M9 9v6M15 9V6.5A2.5 2.5 0 1 1 17.5 9zM15 9v6M15 15h2.5a2.5 2.5 0 1 1-2.5 2.5zM15 15H9M9 15v2.5A2.5 2.5 0 1 1 6.5 15z"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.5-4M4 13a8 8 0 0 0 14.5 4"/><path d="M5.5 3v4h4M18.5 21v-4h-4"/>',
  scenes: '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M3 9.5h18M8.5 4.5l-2 5M14.5 4.5l-2 5"/>',
  volume: '<path d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a8 8 0 0 1 0 11"/>',
  wave: '<path d="M2.5 12h3l2-6 3.5 12 3-9 2 3h5.5"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  undo: '<path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.8h.01"/>',
  sliders: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  warmth: '<path d="M10 14.5V5a2 2 0 0 1 4 0v9.5a4 4 0 1 1-4 0z"/><path d="M12 9v7"/>',
  contrast: '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5v17a8.5 8.5 0 0 0 0-17z" fill="currentColor"/>',
  saturation: '<path d="M12 3.5s6 6.2 6 10.5a6 6 0 0 1-12 0c0-4.3 6-10.5 6-10.5z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  loader: '<path d="M12 3a9 9 0 0 1 9 9"/>',
  pulse: '<path d="M3 12h4l2.5-6 4 12 2.5-6H21"/>',
  mini: '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M10.5 18.5h3"/>',
  moonsun: '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5v17" />',
  logo: '<rect x="3.5" y="9" width="4" height="11" rx="1.6" fill="currentColor" stroke="none"/><rect x="10" y="3.5" width="4" height="16.5" rx="1.6" fill="currentColor" stroke="none"/><rect x="16.5" y="6.5" width="4" height="13.5" rx="1.6" fill="currentColor" stroke="none"/>',
  lock: '<rect x="5" y="10.5" width="14" height="10" rx="2.5"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5"/>',
  snow: '<path d="M12 3v18M4.2 7.5l15.6 9M19.8 7.5l-15.6 9M9.5 4.5L12 7l2.5-2.5M9.5 19.5L12 17l2.5 2.5"/>',
  trash: '<path d="M4 7h16M10 3.5h4M6.5 7l.8 12a1.5 1.5 0 0 0 1.5 1.4h6.4a1.5 1.5 0 0 0 1.5-1.4l.8-12M10 11v6M14 11v6"/>',
  copy: '<rect x="8.5" y="8.5" width="11.5" height="11.5" rx="2.5"/><path d="M15.5 8.5V6A2.5 2.5 0 0 0 13 3.5H6A2.5 2.5 0 0 0 3.5 6v7A2.5 2.5 0 0 0 6 15.5h2.5"/>',
  save: '<path d="M5 3.5h11l3.5 3.5v11.5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2z"/><path d="M7.5 3.5v5h7v-5M7.5 20.5v-6h9v6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  zebra: '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M8 19.5l9-15M3.5 15l7-10.5M13.5 19.5l7-10.5"/>',
  safe: '<rect x="2.5" y="4.5" width="19" height="15" rx="2"/><rect x="6" y="8" width="12" height="8" rx="1"/>',
  cross: '<circle cx="12" cy="12" r="3"/><path d="M12 3.5v5M12 15.5v5M3.5 12h5M15.5 12h5"/>',
  scope: '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M6 15c2-1 2.5-6 4.5-6s2 5 4 5 1.5-3 3.5-3"/>',
  patch: '<circle cx="5.5" cy="7" r="2.2"/><circle cx="5.5" cy="17" r="2.2"/><circle cx="18.5" cy="12" r="2.2"/><path d="M7.7 7h3.3a3 3 0 0 1 3 3v.3l2.3 1M7.7 17h3.3a3 3 0 0 0 3-3v-.3l2.3-1"/>',
  quote: '<path d="M10 7.5H6.5A2.5 2.5 0 0 0 4 10v3.5h4.5V18H4M20 7.5h-3.5A2.5 2.5 0 0 0 14 10v3.5h4.5V18H14" transform="translate(0 -1)"/>',
  headphones: '<path d="M4 15v-3a8 8 0 0 1 16 0v3"/><rect x="3" y="14.5" width="4.5" height="6" rx="1.8"/><rect x="16.5" y="14.5" width="4.5" height="6" rx="1.8"/>',
  list: '<path d="M8.5 6.5H20M8.5 12H20M8.5 17.5H20"/><circle cx="4.5" cy="6.5" r="1" fill="currentColor"/><circle cx="4.5" cy="12" r="1" fill="currentColor"/><circle cx="4.5" cy="17.5" r="1" fill="currentColor"/>',
  server: '<rect x="3.5" y="4" width="17" height="6.5" rx="2"/><rect x="3.5" y="13.5" width="17" height="6.5" rx="2"/><path d="M7.5 7.3h.01M7.5 16.8h.01"/>',
  speaker: '<rect x="6" y="3" width="12" height="18" rx="2.5"/><circle cx="12" cy="14.5" r="3.2"/><circle cx="12" cy="7.5" r="1"/>',
  pause: '<path d="M8 5.5v13M16 5.5v13"/>',
  lights: '<path d="M9.5 17.5h5M10.5 20.5h3"/><path d="M12 3.5a6 6 0 0 0-3.6 10.8c.7.6 1.1 1.4 1.1 2.2v1h5v-1c0-.8.4-1.6 1.1-2.2A6 6 0 0 0 12 3.5z"/>',
  stethoscope: '<path d="M5 3v6a4 4 0 0 0 8 0V3M9 13v2a5 5 0 0 0 10 0v-1.5"/><circle cx="19" cy="11.5" r="2"/>',
};

export const ICON_NAMES = Object.keys(P);

const ATTRS = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"';

export function iconHTML(name, cls = 'icon') {
  const body = P[name] ?? P.info;
  return `<svg class="${cls}" ${ATTRS}>${body}</svg>`;
}

export function icon(name, cls = 'icon') {
  const t = document.createElement('template');
  t.innerHTML = iconHTML(name, cls);
  return t.content.firstElementChild;
}
