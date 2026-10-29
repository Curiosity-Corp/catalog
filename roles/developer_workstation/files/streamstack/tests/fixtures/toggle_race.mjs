// A toggle key pressed three times must alternate, however late the snapshots are (issue #167).
// Drives the real store, actions and ops modules under Node with a fake clock and a fake server.
// argv[2]: the console data directory. Prints one JSON object: per toggle, the values it posted.
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const root = process.argv[2];
let clock = 0;
performance.now = () => clock;
globalThis.location = { search: '?token=t' };
globalThis.window = globalThis;

const posts = [];
const answers = [];
globalThis.fetch = (url, init) => new Promise((resolve) => {
  posts.push(JSON.parse(init.body));
  answers.push(() => resolve({ ok: true, status: 200, json: async () => ({ ok: true }) }));
});

const load = (file) => import(pathToFileURL(path.join(root, file)).href);
const { store } = await load('core/store.js');
const ops = await load('core/ops.js');

const flush = () => new Promise((r) => setImmediate(r));
let answered = 0;
const answerNext = async () => { await flush(); answers[answered++](); await flush(); };

// name -> [the toggle, a snapshot with the control on or off, what an on or off post is called]
const light = (on) => ({ id: 'l1', connected: true, power: on, lumen_range: [0, 100] });
const CASES = {
  toggleMute: [ops.toggleMute, (on) => ({ audio: { mic: { muted: on } } }), (p) => p.on],
  toggleBypass: [ops.toggleBypass, (on) => ({ audio: { chain: { bypass: on } } }), (p) => p.on],
  togglePrivacy: [ops.togglePrivacy, (on) => ({ video: { privacy: on } }), (p) => p.on],
  toggleVirtualcam: [ops.toggleVirtualcam, (on) => ({ video: { virtualcam: on } }), (p) => p.on],
  toggleAuto: [ops.toggleAuto, (on) => ({ auto: { running: on } }), (p) => p.action === 'auto.on'],
  toggleLights: [ops.toggleLights, (on) => ({ lighting: { lights: [light(on)], master: { on } } }), (p) => p.on],
};

const out = {};
for (const [name, [toggle, snap, asked]] of Object.entries(CASES)) {
  posts.length = 0;
  answered = 0;
  answers.length = 0;
  store.applyServer(snap(false));                // S0, built before any press
  toggle();                                      // press 1: on
  await answerNext();
  clock += 100;
  store.applyServer(snap(true));                 // S1: the first press shows up; the hold ends
  clock += 100;
  toggle();                                      // press 2: off
  await answerNext();
  clock += 100;
  // the stream is slow: a snapshot built before press 1 (agrees with press 2) and then the one built between the presses
  store.applyServer(snap(false));                // S0 again, delivered late: it releases the hold of press 2
  store.applyServer(snap(true));                 // S1 again: the page shows "on" while the server says "off"
  toggle();                                      // press 3: must turn it on, not repeat "off"
  await flush();
  out[name] = posts.map(asked);
}
console.log(JSON.stringify(out));
process.exit(0);
