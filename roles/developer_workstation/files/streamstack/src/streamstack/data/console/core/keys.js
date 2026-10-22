// core/keys.js — keyboard map + command registry (palette and shortcut help read from here).
//
//   registerKeys([{ key:'m', desc:'Mute', group:'Mic', run, when? }]) -> unregister
//        key: 'm' | '1' | '?' | 'ctrl+k' | 'alt+1' | 'shift+g'  (lower-case; modifiers ctrl/alt/shift/meta)
//   registerCommands(provider)  -> unregister
//        provider: () => [{ id, title, group, icon?, hint?, keywords?, danger?, run }]
//        Providers run each time the palette opens, so lists (scenes, presets) are always current.
//   allKeys() / allCommands()

const keymaps = new Set();
const providers = new Set();

export function registerKeys(list) {
  const entry = list.map((k) => ({ ...k, key: k.key.toLowerCase() }));
  keymaps.add(entry);
  return () => keymaps.delete(entry);
}

export function registerCommands(provider) {
  providers.add(provider);
  return () => providers.delete(provider);
}

export const allKeys = () => [...keymaps].flat();

export function allCommands() {
  const out = [];
  for (const p of providers) {
    try {
      out.push(...p());
    } catch (err) {
      console.error('command provider failed', err);
    }
  }
  return out;
}

function descriptor(ev) {
  const parts = [];
  if (ev.ctrlKey) parts.push('ctrl');
  if (ev.metaKey) parts.push('meta');
  if (ev.altKey) parts.push('alt');
  // '?' already implies shift; keep shift only for letters/digits combos.
  const k = ev.key.toLowerCase();
  if (ev.shiftKey && !/^[^a-z0-9]$/i.test(ev.key)) parts.push('shift');
  parts.push(ev.altKey && /^Digit\d$/.test(ev.code) ? ev.code.slice(5) : k);
  return parts.join('+');
}

function typing(ev) {
  const t = ev.composedPath()[0];
  if (!(t instanceof HTMLElement)) return false;
  if (t.isContentEditable) return true;
  return ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName) && !(t.tagName === 'INPUT' && ['range', 'checkbox', 'radio', 'button'].includes(t.type));
}

/** Install the one global key handler. Dialogs (palette/help) stop propagation themselves. */
export function installKeyboard() {
  window.addEventListener('keydown', (ev) => {
    if (ev.defaultPrevented || ev.isComposing) return;
    const d = descriptor(ev);
    const modified = ev.ctrlKey || ev.metaKey || ev.altKey;
    if (typing(ev) && !modified) return;
    for (const map of keymaps) {
      for (const k of map) {
        if (k.key !== d) continue;
        if (k.when && !k.when()) continue;
        ev.preventDefault();
        k.run(ev);
        return;
      }
    }
  });
}

/** "ctrl+k" -> "Ctrl K" for display. */
export function prettyKey(key) {
  return key.split('+').map((p) => ({ ctrl: 'Ctrl', alt: 'Alt', shift: 'Shift', meta: 'Meta', escape: 'Esc' }[p] || p.toUpperCase())).join(' ');
}
