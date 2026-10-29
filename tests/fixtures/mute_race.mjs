// Drives the Console's real store, actions and ops modules under Node with a fake clock and a fake server (issue #155).
// argv[2]: the console data directory. Prints one JSON object: what was posted and what the page showed in each case.
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
const { act } = await load('core/actions.js');
const { toggleMute } = await load('core/ops.js');

const snapshot = (muted, gain = 0) => ({ audio: { mic: { muted, gain_db: gain } } });
const muted = () => store.state.audio.mic.muted;
const gain = () => store.state.audio.mic.gain_db;
const flush = () => new Promise((r) => setImmediate(r));
let answered = 0;
const answerNext = async () => { await flush(); answers[answered++](); await flush(); };

// 1. Two M presses; the snapshot with the first mute arrives 2 s after the server answered the post.
const late = [];
store.applyServer(snapshot(false));
toggleMute();                                  // first M: mute
late.push(['after first M', muted()]);
clock += 1000;
store.applyServer(snapshot(false));            // a snapshot still showing the old state arrives
late.push(['stale snapshot, post unanswered', muted()]);
await answerNext();                            // the server answers the first post
clock += 2000;
store.applyServer(snapshot(false));            // a snapshot built before the mute, 2 s after the answer
late.push(['stale snapshot 2 s after the answer', muted()]);
store.applyServer(snapshot(true));             // the snapshot with the mute
late.push(['server snapshot', muted()]);
toggleMute();                                  // second M: must unmute
await flush();
await answerNext();
store.applyServer(snapshot(false));
late.push(['after second M', muted()]);

// 2. A snapshot that agrees releases the hold at once: the next different snapshot is shown.
const agree = [];
store.applyServer(snapshot(false, 0));
act('audio.gain', { db: 5 });
await answerNext();
store.applyServer(snapshot(false, 5));
agree.push(['agreeing snapshot', gain()]);
store.applyServer(snapshot(false, 9));         // someone else changed it right after
agree.push(['next snapshot', gain()]);

// 3. A server that never agrees: the page follows the server again after the ceiling.
const ceiling = [];
store.applyServer(snapshot(false, 0));
act('audio.gain', { db: 7 });
await answerNext();
clock += 4900;
store.applyServer(snapshot(false, 0));
ceiling.push(['before the ceiling', gain()]);
clock += 200;
store.applyServer(snapshot(false, 0));
ceiling.push(['after the ceiling', gain()]);

// 4. A drag: older echoed values never pull the control back while a newer write is out.
const drag = [];
store.applyServer(snapshot(false, 0));
act('audio.gain', { db: 1 });
act('audio.gain', { db: 2 });
act('audio.gain', { db: 3 });
drag.push(['dragged to 3', gain()]);
await answerNext();                            // post 1 answered
store.applyServer(snapshot(false, 1));
drag.push(['echo of 1', gain()]);
await answerNext();                            // post 2 answered
store.applyServer(snapshot(false, 2));
drag.push(['echo of 2', gain()]);
await answerNext();                            // post 3 answered
store.applyServer(snapshot(false, 2));
drag.push(['older echo after the last answer', gain()]);
store.applyServer(snapshot(false, 3));
drag.push(['echo of 3', gain()]);
store.applyServer(snapshot(false, 4));
drag.push(['then the server moves on', gain()]);

// 5. call.set (System page, "beep when muted"): an unrelated `call` snapshot (the call state changed) arrives before the
//    echo of the click. The checkbox is drawn from every change of `call`, so it must still show the click.
const callSet = [];
const callSnap = (state, sound) => ({ ...snapshot(false), call: { available: true, state, muted_warning: { sound } } });
const sound = () => store.state.call.muted_warning.sound;
store.applyServer(callSnap('idle', false));
act('call.set', { sound: true });
await flush();
callSet.push(['clicked', sound()]);
store.applyServer(callSnap('in_call', false));   // a call started; the setting is not saved yet
callSet.push(['unrelated call snapshot', sound()]);
await answerNext();
clock += 2000;
store.applyServer(callSnap('in_call', false));   // still before the echo
callSet.push(['unrelated snapshot after the answer', sound()]);
store.applyServer(callSnap('in_call', true));    // the echo
callSet.push(['echo', sound()]);

// 6. Values that never compare equal to the server's copy (a whole control list, a state that passes) keep the short
//    timer after the answer: they must not hold the old picture for the whole ceiling.
const brief = [];
const ifc = (value, meter) => ({ ...snapshot(false), interface: { controls: [{ id: 'autogain', value, meter }] } });
const autogain = () => store.state.interface.controls[0].value;
store.applyServer(ifc(false, 1));
act('interface.set', { id: 'autogain', value: true });
await answerNext();
store.applyServer(ifc(false, 2));
brief.push(['inside the short hold', autogain()]);
clock += 700;
store.applyServer(ifc(false, 3));
brief.push(['after the short hold', autogain()]);
store.applyServer({ ...snapshot(false), studio: { busy: false } });
act('studio.start');
await answerNext();
clock += 700;
store.applyServer({ ...snapshot(false), studio: { busy: false } });
brief.push(['studio busy after start answered', store.state.studio.busy]);

console.log(JSON.stringify({ posts: posts.filter((p) => p.action === 'audio.mute'), late, agree, ceiling, drag, callSet, brief }));
process.exit(0);
