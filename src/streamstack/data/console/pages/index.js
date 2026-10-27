// pages/index.js — the page registry. THE place to add or replace a page.
//
// Each entry:
//   id        route id: "#/<id>"
//   title     rail label and document title
//   icon      icon name (components/icons.js)
//   blurb     one sentence for the placeholder and the palette
//   features  bullet list the placeholder shows (what is coming)
//   nav       show in the rail (default true)
//   chrome    false = render without rail/top bar (the mini window)
//   key       Alt+<n> shortcut digit
//   load      () => import('./<module>.js')   lazy: a page costs nothing until it is opened
//
// A page module is an ES module exporting:
//   export function mount(host, ctx) { …build into `host`…; return cleanup }   // cleanup is optional
// `ctx` (see app.js): { store, act, actStream, flushStream, toast, navigate, page, registerKeys,
//   registerCommands, ui, icon }. Subscribe with ctx.store.subscribe(selector, cb) and return one
// function that calls every unsubscribe. Replace a placeholder by pointing `load` at your module.

export const pages = [
  {
    id: 'live', title: 'Live', icon: 'live', key: '1',
    blurb: 'The cockpit: picture, scenes, mic and voice at a glance.',
    load: () => import('./live.js'),
  },
  {
    id: 'camera', title: 'Camera', icon: 'camera', key: '2',
    blurb: 'Grade the picture: full controls, looks library, scopes and overlays.',
    load: () => import('./camera.js'),
  },
  {
    id: 'audio', title: 'Audio', icon: 'audio', key: '3',
    blurb: 'The channel strip: raw and processed meters, tone and voice presets.',
    load: () => import('./audio.js'),
  },
  {
    id: 'auto', title: 'Auto', icon: 'auto', key: '4',
    blurb: 'The AI director: what Claude changed, and a timeline you can undo.',
    load: () => import('./auto.js'),
  },
  {
    id: 'system', title: 'System', icon: 'system', key: '5',
    blurb: 'Health checks, services and the full tools.',
    load: () => import('./system.js'),
  },
  {
    id: 'lighting', title: 'Lighting', icon: 'lights', key: '6',
    blurb: 'Logitech Litra lights: power, brightness and colour temperature, with presets.',
    load: () => import('./lighting.js'),
  },
  {
    id: 'interface', title: 'Interface', icon: 'sliders', key: '7',
    blurb: 'Your audio interface: hardware gain, Autogain, 48V and Air for a Scarlett; gain and mute for a Wave XLR.',
    load: () => import('./interface.js'),
  },
  {
    id: 'presets', title: 'Presets', icon: 'scenes', key: '8',
    blurb: 'One-click profiles, platform and bandwidth presets, and the looks library.',
    load: () => import('./presets.js'),
  },
  {
    id: 'stream', title: 'Stream', icon: 'broadcast', key: '0',
    blurb: 'Go live and record: destinations, encoder, live bitrate and quality, and what the stream is doing.',
    load: () => import('./stream.js'),
  },
  {
    id: 'deck', title: 'Deck', icon: 'deck', key: '9',
    blurb: 'An Elgato Stream Deck as a control surface: drag any option or preset onto a key.',
    load: () => import('./deck.js'),
  },
  {
    id: 'mini', title: 'Mini', icon: 'mini', nav: false, chrome: false,
    blurb: 'A compact strip for a small window next to the call.',
    load: () => import('./mini.js'),
  },
];

export const defaultPage = pages[0];
