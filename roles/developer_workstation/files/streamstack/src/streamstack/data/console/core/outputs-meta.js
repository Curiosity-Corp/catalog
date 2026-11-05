// core/outputs-meta.js — how an audio output is named and pictured, shared by the Audio page's strips, the Live
// page's picker and the top-bar output menu so one device reads the same everywhere.

export const KIND_ICON = { hdmi: 'monitor', headphones: 'headphones', headset: 'headset', speaker: 'speaker', interface: 'audio', route: 'wave', other: 'volume' };
export const BUS_LABEL = { usb: 'USB', bluetooth: 'Bluetooth', pci: 'Built in' };
export const KIND_LABEL = { hdmi: 'HDMI', headphones: 'Headphones', headset: 'Headset', speaker: 'Speakers', interface: 'Audio interface', other: 'Output' };

/** "HDMI · Built in · Digital Stereo (HDMI 2)": what kind of output it is and how it is connected, without repeats. */
export function outputDetail(d) {
  return [KIND_LABEL[d.kind], BUS_LABEL[d.bus] || '', d.detail].filter((x, i, a) => x && a.indexOf(x) === i).join(' · ');
}

export const ROUTE_NOTE = 'A route, not a device: sound sent here is processed, then plays on your outputs';
