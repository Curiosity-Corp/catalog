// core/theme.js — dark (default) / light, remembered when storage is available.

const KEY = 'streamstack.console.theme';
let theme = 'dark';

function read() {
  try {
    const v = localStorage.getItem(KEY);
    return v === 'light' || v === 'dark' ? v : null;
  } catch (err) {
    return null;                       // storage blocked: fine, per-session only
  }
}

export function applyTheme(next) {
  theme = next === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = theme;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', theme === 'light' ? '#eef0f4' : '#0b0d10');
  try {
    localStorage.setItem(KEY, theme);
  } catch (err) {
    // ignored on purpose: the theme still applies for this session
  }
  window.dispatchEvent(new CustomEvent('ss:theme', { detail: theme }));
}

export const getTheme = () => theme;
export const toggleTheme = () => applyTheme(theme === 'dark' ? 'light' : 'dark');
export function initTheme() {
  theme = read() || 'dark';
  document.documentElement.dataset.theme = theme;
}
