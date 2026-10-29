// A plain-value hold must be released by a snapshot built after the action, never by an older one that happens to equal
// it (issue #169). Drives the real store and actions under Node with a fake clock and a fake server.
// argv[2]: the console data directory. Prints one JSON object: what the page showed at each step, per control.
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const root = process.argv[2];
let clock = 0;
performance.now = () => clock;
globalThis.location = { search: '?token=t' };
globalThis.window = globalThis;
globalThis.dispatchEvent = () => true;
globalThis.CustomEvent = class { constructor(t, o) { this.type = t; Object.assign(this, o); } };

const answers = [];
globalThis.fetch = (url, init) => new Promise((resolve) => {
  answers.push((seq) => resolve({ ok: true, status: 200, json: async () => ({ ok: true, ...(seq === undefined ? {} : { seq }) }) }));
});

const load = (file) => import(pathToFileURL(path.join(root, file)).href);
const { store } = await load('core/store.js');
const { act } = await load('core/actions.js');

const flush = () => new Promise((r) => setImmediate(r));
let answered = 0;
const answerNext = async (seq) => { await flush(); answers[answered++](seq); await flush(); };

// name -> [action, params for the value the person sets, a snapshot with the control at a value, the control's value]
const CASES = {
  mute: ['audio.mute', { on: false }, (seq, v) => ({ seq, audio: { mic: { muted: v } } }), () => store.state.audio.mic.muted, true, false],
  bypass: ['audio.bypass', { on: false }, (seq, v) => ({ seq, audio: { chain: { bypass: v } } }), () => store.state.audio.chain.bypass, true, false],
  gain: ['audio.gain', { db: 0 }, (seq, v) => ({ seq, audio: { mic: { gain_db: v } } }), () => store.state.audio.mic.gain_db, 6, 0],
};

const out = {};
let base = 0;   // snapshot numbers only grow, so each control starts above the last
for (const [name, [action, params, make, shown, before, after]] of Object.entries(CASES)) {
  base += 100;
  const snap = (seq, v) => make(base + seq, v);
  const num = (seq) => base + seq;
  answered = 0;
  answers.length = 0;
  const seen = {};
  // the control is `before` (snapshot 1); the person puts it to `after`
  store.applyServer(snap(1, before));
  act(action, params);
  await flush();
  seen.clicked = shown();
  await answerNext(num(3));                          // answered: the first snapshot built after the action is number 3
  clock += 100;
  // a snapshot built BEFORE the action that equals the new value (the control was `after` earlier too), delivered late
  store.applyServer(snap(2, after));
  seen.staleButEqual = shown();
  store.applyServer(snap(2, before));           // and the next stale one, still showing the old value
  seen.staleOld = shown();
  store.applyServer(snap(3, after));            // the snapshot that carries the change
  seen.carried = shown();
  // a later change by someone else is shown at once: the hold is over
  store.applyServer(snap(4, before));
  seen.later = shown();
  // an unchanged server sends an ack instead of a snapshot: it releases a hold too
  act(action, params);
  await answerNext(num(6));
  store.acknowledge(num(6));
  store.applyServer(snap(6, after));
  seen.acked = shown();
  out[name] = { seen, after, before };
}
console.log(JSON.stringify(out));
process.exit(0);
