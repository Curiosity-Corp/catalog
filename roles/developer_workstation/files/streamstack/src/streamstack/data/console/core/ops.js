// core/ops.js — the named operations a human does mid-stream. Keys, palette, pads and pages all call these,
// so behaviour (confirmations, optimistic updates, toasts) is identical everywhere.

import { act } from './actions.js';
import { store } from './store.js';
import { toast } from './toast.js';

export const toggleMute = () => act('audio.mute', { on: !store.state.audio.mic.muted });
export const toggleBypass = () => act('audio.bypass', { on: !store.state.audio.chain.bypass });
export const togglePrivacy = () => act('video.privacy', { on: !store.state.video.privacy });
export const toggleAuto = () => act(store.state.auto.running ? 'auto.off' : 'auto.on');
export const toggleVirtualcam = () => act('video.virtualcam', { on: !store.state.video.virtualcam });
export const selectScene = (name) => act('video.scene', { name });
export const selectSceneByIndex = (i) => {
  const sc = store.state.video.scenes[i];
  if (sc) selectScene(sc.name);
};
/** Restart a stalled microphone (suspend/resume, then card profile cycle on the server). */
export const reviveMic = () => act('audio.revive');
export const selectPreset = (name) => act('audio.preset', { name });
/** The sound check on one output (a short left/right then chime, on that output only), or on every output in turn. */
export const testOutput = (name) => act('audio.output_test', { name });
export const testAllOutputs = () => act('audio.output_test_all');
export const stopOutputTest = () => act('audio.output_test_stop');
export const setDefaultOutput = (name) => act('audio.output_default', { name });

/** Starting is safe; stopping is only done after a confirmation (hold a button, or confirm the toast). */
export function setRecord(on) { return act('video.record', { on }); }

/** For callers without a hold button (palette, keys): ask once in a toast, then run. */
export function confirmThen(text, label, run) {
  toast(text, { level: 'warn', timeout: 6000, id: `confirm:${label}`, action: { label, run } });
}
export const stopStudio = () => act('studio.stop');
export const startStudio = () => act('studio.start');

/** Every Litra on or off together (the L key and the palette). */
export const toggleLights = () => act('lighting.power', { id: 'all', on: !store.state.lighting.master.on });
export const selectLightPreset = (name) => act('lighting.preset', { name });
