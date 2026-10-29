// Drives the Console's real store and actions under Node with a fake clock and a fake server (issue #157).
// A control that is edited must stay as edited until a snapshot that began AFTER the action arrives (the answer's `seq`).
// argv[2]: the console data directory. Prints one JSON object: what the page showed at each step.
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const root = process.argv[2];
let clock = 0;
performance.now = () => clock;
globalThis.location = { search: '?token=t' };
globalThis.window = globalThis;
globalThis.dispatchEvent = () => true;
globalThis.CustomEvent = class { constructor(t, o) { this.type = t; Object.assign(this, o); } };

const posts = [];
const answers = [];
globalThis.fetch = (url, init) => new Promise((resolve) => {
  posts.push(JSON.parse(init.body));
  answers.push((seq) => resolve({ ok: true, status: 200, json: async () => ({ ok: true, ...(seq === undefined ? {} : { seq }) }) }));
});

const load = (file) => import(pathToFileURL(path.join(root, file)).href);
const { store } = await load('core/store.js');
const { act, OPTIMISTIC } = await load('core/actions.js');

const flush = () => new Promise((r) => setImmediate(r));
let answered = 0;
const answerNext = async (seq) => { await flush(); answers[answered++](seq); await flush(); };

let n = 0;
/** A snapshot numbered `seq` (the number of the build that made it) around `parts`. */
const snap = (seq, parts) => ({ seq, ...parts });

const out = { optimistic: Object.keys(OPTIMISTIC) };

// -- deck -------------------------------------------------------------------------------------------------------
const key = (i, label) => (label === null ? { i, empty: true, rev: '0' } : { i, empty: false, label, icon: 'command', action: 'x', raw: { action: 'x', params: {}, label } });
const deckSnap = (seq, { pages, model = 'virtual-4x4', home = 'main' }) => snap(seq, { deck: { profile: { model, home, layout: { rows: 1, cols: 2 }, pages }, models: [{ id: 'virtual-4x4' }, { id: 'sd-15' }] } });
const pg = (id, name, ...keys) => ({ id, name, keys });
const profile = () => store.state.deck.profile;
const d = {};

// key_set on an empty key: a snapshot built before the action arrives after the answer
store.applyServer(deckSnap(1, { pages: [pg('main', 'Main', key(0, null), key(1, 'B'))] }));
act('deck.key_set', { page: 'main', key: 0, spec: { action: 'audio.mute', params: {} } });
await flush();
d.keySetClicked = profile().pages[0].keys[0].empty;
store.applyServer(deckSnap(2, { pages: [pg('main', 'Main', key(0, null), key(1, 'B'))] }));   // built before the action
await answerNext(3);                                                                            // the answer: first snapshot after is 3
clock += 3000;
store.applyServer(deckSnap(2, { pages: [pg('main', 'Main', key(0, null), key(1, 'B'))] }));   // late, still before
d.keySetStale = profile().pages[0].keys[0].empty;
store.applyServer(deckSnap(3, { pages: [pg('main', 'Main', key(0, 'Mute'), key(1, 'B'))] })); // the one that carries it
d.keySetServer = profile().pages[0].keys[0].label;

// label edit on a filled key sticks
store.applyServer(deckSnap(4, { pages: [pg('main', 'Main', key(0, 'Mute'), key(1, 'B'))] }));
act('deck.key_set', { page: 'main', key: 1, spec: { action: 'x', params: {}, label: 'Renamed' } });
await answerNext(6);
store.applyServer(deckSnap(5, { pages: [pg('main', 'Main', key(0, 'Mute'), key(1, 'B'))] }));
d.labelStale = profile().pages[0].keys[1].raw.label;
store.applyServer(deckSnap(6, { pages: [pg('main', 'Main', key(0, 'Mute'), key(1, 'Renamed'))] }));
d.labelServer = profile().pages[0].keys[1].label;

// key_clear, then an echo that does not change anything: the ack releases the hold
act('deck.key_clear', { page: 'main', key: 0 });
d.clearClicked = profile().pages[0].keys[0].empty;
await answerNext(8);
store.applyServer(deckSnap(7, { pages: [pg('main', 'Main', key(0, 'Mute'), key(1, 'Renamed'))] }));
d.clearStale = profile().pages[0].keys[0].empty;
store.applyServer(deckSnap(8, { pages: [pg('main', 'Main', key(0, null), key(1, 'Renamed'))] }));
d.clearServer = profile().pages[0].keys[0].empty;

// key_move between pages swaps the two
store.applyServer(deckSnap(9, { pages: [pg('main', 'Main', key(0, 'A'), key(1, null)), pg('two', 'Two', key(0, null), key(1, 'Z'))] }));
act('deck.key_move', { page: 'main', key: 0, to_page: 'two', to: 1 });
d.moved = [profile().pages[0].keys[0].empty, profile().pages[1].keys[1].label];
await answerNext(11);
store.applyServer(deckSnap(10, { pages: [pg('main', 'Main', key(0, 'A'), key(1, null)), pg('two', 'Two', key(0, null), key(1, 'Z'))] }));
d.movedStale = [profile().pages[0].keys[0].label, profile().pages[1].keys[1].label];

// page rename, page move, page delete, layout
const three = (names = ['Main', 'Two', 'Three']) => [pg('main', names[0]), pg('two', names[1]), pg('three', names[2])];
store.applyServer(deckSnap(12, { pages: three() }));
act('deck.page_rename', { page: 'two', name: 'Mixer' });
await answerNext(14);
store.applyServer(deckSnap(13, { pages: three() }));
d.renameStale = profile().pages[1].name;
store.applyServer(deckSnap(14, { pages: three(['Main', 'Mixer', 'Three']) }));
d.renameServer = profile().pages[1].name;

store.applyServer(deckSnap(15, { pages: three() }));
act('deck.page_move', { page: 'three', to: 0 });
await answerNext(17);
store.applyServer(deckSnap(16, { pages: three() }));
d.moveStale = profile().pages.map((p) => p.id);
store.applyServer(deckSnap(17, { pages: [pg('three', 'Three'), pg('main', 'Main'), pg('two', 'Two')] }));
d.moveServer = profile().pages.map((p) => p.id);

store.applyServer(deckSnap(18, { pages: three() }));
act('deck.page_delete', { page: 'main' });
d.deleteClicked = [profile().pages.map((p) => p.id), profile().home];
await answerNext(20);
store.applyServer(deckSnap(19, { pages: three() }));
d.deleteStale = profile().pages.map((p) => p.id);
store.applyServer(deckSnap(20, { pages: [pg('two', 'Two'), pg('three', 'Three')], home: 'two' }));
d.deleteServer = profile().pages.map((p) => p.id);

store.applyServer(deckSnap(21, { pages: three() }));
act('deck.layout', { model: 'sd-15' });
await answerNext(23);
store.applyServer(deckSnap(22, { pages: three() }));
d.layoutStale = profile().model;
store.applyServer(deckSnap(23, { pages: three(), model: 'sd-15' }));
d.layoutServer = profile().model;
out.deck = d;

// -- other controls ----------------------------------------------------------------------------------------------
const other = {};
const full = (seq, parts) => snap(seq, {
  presets: { platform: 'twitch', tier: null }, slots: { fade_seconds: 4, slots: [{ id: 'morning', profile: { look: 'a' }, empty: false }] },
  lighting: { automation: { settings: { schedule: { slots: { morning: { brightness: 50, kelvin: 4000 } } }, lights: { 'k:1': { role: 'key' } } }, hours: Array.from({ length: 24 }, () => ({ brightness: 40, kelvin: 3000 })) } },
  ...parts,
});

store.applyServer(full(30, {}));
act('presets.platform', { id: 'youtube' });
other.platformClicked = [store.state.presets.platform, store.state.presets.tier];
await answerNext(32);
store.applyServer(full(31, {}));
other.platformStale = store.state.presets.platform;
store.applyServer(full(32, { presets: { platform: 'youtube', tier: 'balanced' } }));
other.platformServer = [store.state.presets.platform, store.state.presets.tier];

store.applyServer(full(33, {}));
act('slots.profile', { slot: 'morning', profile: { look: 'b', voice: 'v' } });
await answerNext(35);
store.applyServer(full(34, {}));
other.profileStale = store.state.slots.slots[0].profile;
store.applyServer(full(35, { slots: { fade_seconds: 4, slots: [{ id: 'morning', profile: { look: 'b', voice: 'v' }, empty: false }] } }));
other.profileServer = store.state.slots.slots[0].profile;

store.applyServer(full(36, {}));
act('slots.fade', { seconds: 9 });
await answerNext(38);
store.applyServer(full(37, {}));
other.fadeStale = store.state.slots.fade_seconds;

store.applyServer(full(39, {}));
act('lighting.light.settings', { id: 'k:1', role: 'fill', min_brightness: 10 });
await answerNext(41);
store.applyServer(full(40, {}));
other.lightStale = store.state.lighting.automation.settings.lights['k:1'];

store.applyServer(full(42, {}));
act('lighting.auto.slot', { slot: 'morning', brightness: 80 });
act('lighting.auto.hour', { hour: 5, kelvin: 2700 });
await answerNext(44);
await answerNext(44);
store.applyServer(full(43, {}));
other.slotStale = store.state.lighting.automation.settings.schedule.slots.morning.brightness;
other.hourStale = store.state.lighting.automation.hours[5].kelvin;
store.applyServer(full(44, {}));    // the server refused or clamped: its answer wins once a snapshot after the action arrives
other.slotAfter = store.state.lighting.automation.settings.schedule.slots.morning.brightness;

// An answer without a `seq` (an older server) falls back to agreement or the ceiling.
store.applyServer(full(45, {}));
act('slots.clear', { slot: 'morning' });
await answerNext(undefined);
store.applyServer(full(46, {}));
other.noSeqInside = store.state.slots.slots[0].empty;
clock += 6000;
store.applyServer(full(47, {}));
other.noSeqAfter = store.state.slots.slots[0].empty;

// A hold released by a failed action: nothing stays on screen.
store.applyServer(full(48, {}));
globalThis.fetch = () => Promise.resolve({ ok: false, status: 409, json: async () => ({ ok: false, error: 'no' }) });
await act('presets.tier', { id: 'high' });
other.failed = store.state.presets.tier;

// The rule from #155 holds: opening a metrics endpoint, starting a stream or a recording show only what the server did.
other.serverTruth = ['telemetry.set', 'stream.start', 'stream.stop', 'record.start', 'record.stop', 'echo.check'].filter((a) => a in OPTIMISTIC);
out.other = other;

console.log(JSON.stringify(out));
process.exit(0);
