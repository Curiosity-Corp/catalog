// core/whep.js — a WHEP (WebRTC-HTTP egress) client for the program preview (docs/whep-preview.md).
//
//   const session = await whepConnect({ video, source: 'program', width: 640, fps: 12, onState });
//   session.close();
//
// The browser offers one receive-only video track; the Console answers with its own SDP and the session's URL.
// Rejects WhepError (with `status` and the server's reason) when the Console cannot serve a WebRTC preview, so the
// caller falls back to MJPEG. `onState('connected' | 'failed' | 'closed')` reports the peer connection afterwards.
// URLs are relative and carry the per-launch token like every other call (core/api.js).

import { apiUrl } from './api.js';

const GATHER_MS = 1500; // host candidates arrive within milliseconds; do not wait on a slow or odd interface
const POST_MS = 12000;

export class WhepError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = 'WhepError';
    this.status = status;
  }
}

export const whepSupported = () => typeof RTCPeerConnection === 'function';

function gathered(pc) {
  if (pc.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => { pc.removeEventListener('icegatheringstatechange', check); clearTimeout(timer); resolve(); };
    const check = () => { if (pc.iceGatheringState === 'complete') done(); };
    const timer = setTimeout(done, GATHER_MS);
    pc.addEventListener('icegatheringstatechange', check);
  });
}

async function reason(res) {
  try {
    const body = await res.json();
    if (body && body.error) return body.error;
  } catch (err) {
    // not JSON: fall through to the status text
  }
  return `HTTP ${res.status}`;
}

export async function whepConnect({ video, source = 'program', width = 640, fps = 12, onState = () => {} }) {
  if (!whepSupported()) throw new WhepError('This browser has no WebRTC.');
  const pc = new RTCPeerConnection({ iceServers: [] });
  let sessionId = '';
  let closed = false;

  const close = () => {
    if (closed) return;
    closed = true;
    if (sessionId) {
      // keepalive lets the DELETE leave while the page is unloading
      fetch(apiUrl(`api/whep/${sessionId}`), { method: 'DELETE', keepalive: true, cache: 'no-store' }).catch(() => {});
    }
    pc.ontrack = null;
    pc.onconnectionstatechange = null;
    pc.close();
    if (video.srcObject) video.srcObject = null;
    onState('closed');
  };

  try {
    pc.addTransceiver('video', { direction: 'recvonly' });
    pc.ontrack = (event) => {
      const stream = event.streams[0] || new MediaStream([event.track]);
      video.srcObject = stream;
      event.track.addEventListener('ended', () => { if (!closed) onState('failed'); });
    };
    pc.onconnectionstatechange = () => {
      if (closed) return;
      if (pc.connectionState === 'connected') onState('connected');
      else if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected') onState('failed');
    };
    await pc.setLocalDescription(await pc.createOffer());
    await gathered(pc);
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), POST_MS);
    let res;
    try {
      res = await fetch(apiUrl('api/whep', { source, width, fps }), {
        method: 'POST',
        headers: { 'Content-Type': 'application/sdp' },
        body: pc.localDescription.sdp,
        cache: 'no-store',
        signal: ctl.signal,
      });
    } catch (err) {
      throw new WhepError('The Console did not answer the preview request.');
    } finally {
      clearTimeout(timer);
    }
    if (res.status !== 201) throw new WhepError(await reason(res), res.status);
    const location = res.headers.get('Location') || '';
    sessionId = location.split('/').filter(Boolean).pop() || '';
    const answer = await res.text();
    if (closed) {
      // the caller gave up while the answer was on its way: end the session the server just opened
      if (sessionId) fetch(apiUrl(`api/whep/${sessionId}`), { method: 'DELETE', keepalive: true, cache: 'no-store' }).catch(() => {});
      throw new WhepError('The preview was closed before it started.');
    }
    await pc.setRemoteDescription({ type: 'answer', sdp: answer });
  } catch (err) {
    close();
    throw err instanceof WhepError ? err : new WhepError(err && err.message ? err.message : 'WebRTC setup failed.');
  }
  return { pc, close, get id() { return sessionId; } };
}
