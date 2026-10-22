// core/toast.js — fire-and-forget notifications. The <ss-toast> host (components/ss-toast.js)
// listens for the event, so any module can toast without importing a component.
//
//   toast('Saved')                                   // info, 4 s
//   toast('OBS is not running', { level: 'error', action: { label: 'Start studio', run: () => act('studio.start') } })
//   toast.error(err)  toast.success('…')  toast.warn('…')
//
// Options: level info|success|warn|error, timeout ms (0 = sticky), action {label, run}, id (replaces a toast with the same id).

export function toast(text, options = {}) {
  window.dispatchEvent(new CustomEvent('ss:toast', { detail: { text: String(text), ...options } }));
}
toast.error = (e, options = {}) => toast(e instanceof Error ? e.message : String(e), { level: 'error', timeout: 7000, ...options });
toast.success = (t, options = {}) => toast(t, { level: 'success', ...options });
toast.warn = (t, options = {}) => toast(t, { level: 'warn', timeout: 6000, ...options });
