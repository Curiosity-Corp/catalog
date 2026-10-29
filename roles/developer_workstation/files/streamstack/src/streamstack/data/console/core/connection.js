// core/connection.js — the live link: initial snapshot, Server-Sent Events, reconnect with
// exponential backoff, a stale-stream watchdog and the offline/unauthorised status the banner shows.

import { apiUrl, getJson, hasToken, ApiError } from './api.js';
import { store } from './store.js';
import { toast } from './toast.js';

const BACKOFF = [500, 1000, 2000, 4000, 8000, 10000];
const WATCHDOG_MS = 20000;          // server pings every 15 s and meters 30 times a second

let source = null;
let attempt = 0;
let retryTimer = 0;
let countdownTimer = 0;
let watchdog = 0;
let started = false;

const MUTE_METERS = { t: 0, mic: null, voice: null };

function parse(ev) {
  try {
    return JSON.parse(ev.data);
  } catch (err) {
    console.warn('unreadable event', ev.type, err);
    return null;
  }
}

function arm() {
  clearTimeout(watchdog);
  watchdog = setTimeout(() => {
    console.warn('event stream went quiet; reconnecting');
    drop('The live stream went quiet.');
  }, WATCHDOG_MS);
}

function scheduleRetry(message) {
  clearTimeout(retryTimer);
  clearInterval(countdownTimer);
  const delay = BACKOFF[Math.min(attempt, BACKOFF.length - 1)] * (0.85 + Math.random() * 0.3);
  attempt += 1;
  const at = performance.now() + delay;
  const tick = () => store.setStatus('reconnecting', {
    attempt, message, retryIn: Math.max(0, Math.ceil((at - performance.now()) / 1000)),
  });
  tick();
  countdownTimer = setInterval(tick, 500);
  retryTimer = setTimeout(() => { clearInterval(countdownTimer); open(); }, delay);
}

function drop(message) {
  if (source) { source.close(); source = null; }
  clearTimeout(watchdog);
  store.pushMeters(MUTE_METERS);
  scheduleRetry(message);
}

async function open() {
  clearTimeout(retryTimer);
  clearInterval(countdownTimer);
  if (!hasToken()) {
    store.setStatus('unauthorized', { message: 'No access token in the page address. Open the Console with “streamstack console” or from the StreamStack tray: the access token is new on every launch, so an installed app window needs the launcher to open it.' });
    return;
  }
  try {
    store.applyServer(await getJson('api/state'));
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
      store.setStatus('unauthorized', { message: err.message });
      return;
    }
    scheduleRetry(err.message);
    return;
  }
  const es = new EventSource(apiUrl('api/events'));
  source = es;
  arm();
  es.addEventListener('open', () => {
    attempt = 0;
    store.setStatus('online');
    arm();
  });
  es.addEventListener('state', (ev) => {
    const snap = parse(ev);
    if (snap) store.applyServer(snap);
    store.setStatus('online');
    arm();
  });
  es.addEventListener('ack', (ev) => {
    const ack = parse(ev);
    if (ack) store.acknowledge(ack.seq);
    arm();
  });
  es.addEventListener('meters', (ev) => {
    const frame = parse(ev);
    if (frame) store.pushMeters(frame);
    arm();
  });
  es.addEventListener('toast', (ev) => {
    const t = parse(ev);
    if (t && t.text) toast(t.text, { level: ['success', 'warn', 'error'].includes(t.level) ? t.level : 'info' });
    arm();
  });
  es.addEventListener('error', () => {
    if (source !== es) return;
    drop('Lost the connection to StreamStack.');
  });
}

/** Begin (once). Safe to call again; later calls are ignored. */
export function connect() {
  if (started) return;
  started = true;
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && store.status === 'reconnecting') retryNow();
  });
  window.addEventListener('online', () => { if (store.status === 'reconnecting') retryNow(); });
  open();
}

/** “Retry now” button on the offline banner. */
export function retryNow() {
  attempt = 0;
  if (source) { source.close(); source = null; }
  open();
}
