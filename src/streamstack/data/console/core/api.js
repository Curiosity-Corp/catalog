// core/api.js — HTTP plumbing. Relative URLs only; the per-launch token is read from the
// page URL once and kept in memory (header for fetch, ?token= for EventSource/img/audio).

const params = new URLSearchParams(location.search);
const TOKEN = params.get('token') || '';

export const hasToken = () => TOKEN !== '';

/** Build a relative API URL with the token appended, e.g. apiUrl('api/events'). */
export function apiUrl(path, query = {}) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null) q.set(k, String(v));
  if (TOKEN) q.set('token', TOKEN);
  const s = q.toString();
  return s ? `${path}?${s}` : path;
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function request(path, init = {}) {
  const headers = { 'X-StreamStack-Token': TOKEN, ...(init.headers || {}) };
  let res;
  try {
    res = await fetch(path, { ...init, headers, cache: 'no-store' });
  } catch (err) {
    throw new ApiError('The StreamStack server is not reachable.', 0);
  }
  let body = null;
  try {
    body = await res.json();
  } catch (err) {
    if (res.ok) throw new ApiError('The server sent an unreadable answer.', res.status);
  }
  if (!res.ok || (body && body.ok === false)) {
    const msg = (body && body.error) || (res.status === 401 || res.status === 403
      ? 'Session expired — reopen the Console with “streamstack console”.'
      : `Request failed (HTTP ${res.status}).`);
    throw new ApiError(msg, res.status);
  }
  return body;
}

export const getJson = (path) => request(path);

/** POST /api/action. Resolves `{ok, message?}`, rejects ApiError carrying the server's `error`. */
export const postAction = (action, params = {}) => request('api/action', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ action, ...params }),
});
