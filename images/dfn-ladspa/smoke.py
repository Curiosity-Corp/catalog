#!/usr/bin/env python3
"""Load the built plugin the way a LADSPA host does and run it: label, ports, one second of audio, no crash.

Checks what the chain depends on (`src/streamstack/audiochain/graph.py`): one descriptor with the label
`deep_filter_mono`, one audio input port, one audio output port; then instantiates it at 48 kHz and runs it over a
second of a noisy tone in 480-sample blocks, requiring finite output. Standard library only.
"""

from __future__ import annotations

import ctypes
import math
import sys

INPUT, OUTPUT, CONTROL, AUDIO = 1, 2, 4, 8
LABEL = b"deep_filter_mono"
RATE = 48000
BLOCK = 480


class Hints(ctypes.Structure):
    _fields_ = [("HintDescriptor", ctypes.c_int), ("LowerBound", ctypes.c_float), ("UpperBound", ctypes.c_float)]


class Descriptor(ctypes.Structure):
    _fields_ = [
        ("UniqueID", ctypes.c_ulong),
        ("Label", ctypes.c_char_p),
        ("Properties", ctypes.c_int),
        ("Name", ctypes.c_char_p),
        ("Maker", ctypes.c_char_p),
        ("Copyright", ctypes.c_char_p),
        ("PortCount", ctypes.c_ulong),
        ("PortDescriptors", ctypes.POINTER(ctypes.c_int)),
        ("PortNames", ctypes.POINTER(ctypes.c_char_p)),
        ("PortRangeHints", ctypes.POINTER(Hints)),
        ("ImplementationData", ctypes.c_void_p),
        ("instantiate", ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)),
        ("connect_port", ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p)),
        ("activate", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
        ("run", ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ulong)),
        ("run_adding", ctypes.c_void_p),
        ("set_run_adding_gain", ctypes.c_void_p),
        ("deactivate", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
        ("cleanup", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
    ]


def main(path: str) -> int:
    lib = ctypes.CDLL(path)
    lib.ladspa_descriptor.restype = ctypes.POINTER(Descriptor)
    lib.ladspa_descriptor.argtypes = [ctypes.c_ulong]
    first = lib.ladspa_descriptor(0)
    if not first:
        print("smoke: ladspa_descriptor(0) is NULL", file=sys.stderr)
        return 1
    d = first.contents
    if d.Label != LABEL:
        print(f"smoke: label is {d.Label!r}, expected {LABEL!r}", file=sys.stderr)
        return 1
    ports = [(d.PortDescriptors[i], d.PortNames[i]) for i in range(d.PortCount)]
    audio_in = [i for i, (desc, _) in enumerate(ports) if desc & INPUT and desc & AUDIO]
    audio_out = [i for i, (desc, _) in enumerate(ports) if desc & OUTPUT and desc & AUDIO]
    if len(audio_in) != 1 or len(audio_out) != 1:
        print(f"smoke: expected one audio input and one audio output, got {ports!r}", file=sys.stderr)
        return 1
    print(f"smoke: {d.Label.decode()} {d.Name.decode()!r}, ports " + ", ".join(n.decode() for _, n in ports))

    handle = d.instantiate(ctypes.byref(d), RATE)
    if not handle:
        print("smoke: instantiate failed", file=sys.stderr)
        return 1
    controls = []
    for i, (desc, _) in enumerate(ports):
        if desc & CONTROL and desc & INPUT:
            value = ctypes.c_float(d.PortRangeHints[i].UpperBound if d.PortRangeHints[i].UpperBound else 0.0)
            controls.append(value)
            d.connect_port(handle, i, ctypes.cast(ctypes.byref(value), ctypes.c_void_p))
    buf_in = (ctypes.c_float * BLOCK)()
    buf_out = (ctypes.c_float * BLOCK)()
    d.connect_port(handle, audio_in[0], ctypes.cast(buf_in, ctypes.c_void_p))
    d.connect_port(handle, audio_out[0], ctypes.cast(buf_out, ctypes.c_void_p))
    if d.activate:
        d.activate(handle)
    noise = [math.sin(n * 12.9898) * 0.025 for n in range(BLOCK)]  # a fixed rough signal, no RNG needed
    produced = 0.0
    for block in range(RATE // BLOCK):
        for n in range(BLOCK):
            t = (block * BLOCK + n) / RATE
            buf_in[n] = 0.3 * math.sin(2 * math.pi * 220 * t) + noise[n]
        d.run(handle, BLOCK)
        for n in range(BLOCK):
            if not math.isfinite(buf_out[n]):
                print("smoke: the plugin produced a non-finite sample", file=sys.stderr)
                return 1
            produced = max(produced, abs(buf_out[n]))
    if d.deactivate:
        d.deactivate(handle)
    d.cleanup(handle)
    print(f"smoke: ran {RATE // BLOCK} blocks, finite output, peak {produced:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
