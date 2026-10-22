// core/time.js — human time for journals and notes (no dependencies).
//
//   parseLocal(iso)         ISO-8601 local string ("2026-10-22T14:03:11") -> Date | null
//   relTime(iso, now?)      "just now", "40 s ago", "3 min ago", "2 h ago", "yesterday 14:03", "Oct 20 14:03"
//   minuteKey(iso)          "2026-10-22T14:03" — groups journal rows by minute
//   mmss(seconds)           83 -> "1:23"

export function parseLocal(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

const pad = (n) => String(n).padStart(2, '0');
const hhmm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

export function relTime(iso, now = new Date()) {
  const d = parseLocal(iso);
  if (!d) return '';
  const s = Math.round((now.getTime() - d.getTime()) / 1000);
  if (s < 5) return 'just now';
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 6 * 3600) return `${Math.floor(s / 3600)} h ago`;
  const days = Math.floor((new Date(now.getFullYear(), now.getMonth(), now.getDate()) - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
  if (days === 0) return `today ${hhmm(d)}`;
  if (days === 1) return `yesterday ${hhmm(d)}`;
  return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${hhmm(d)}`;
}

export function minuteKey(iso) {
  return iso ? String(iso).slice(0, 16) : '';
}

export function mmss(seconds) {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${pad(s % 60)}`;
}
