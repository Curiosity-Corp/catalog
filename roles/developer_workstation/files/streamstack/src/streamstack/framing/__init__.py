"""Auto-framing: a face tracker beside OBS that moves a smooth crop over obs-websocket.

    engine    detections -> smooth window (pure maths, no I/O)
    model     the YuNet face model: pinned URL, checksum, fetch
    detector  OpenVINO (GPU, CPU) and OpenCV backends behind one interface
    crop      the window as an OBS scene item crop, with crash-safe restore
    health    the doctor / System check

The Console wiring (threads, actions, state) lives in `streamstack.console.framing`.
"""

from streamstack.framing.engine import MODES, PROFILES, Face, Framer, Window, resolution_zoom_cap

__all__ = ["MODES", "PROFILES", "Face", "Framer", "Window", "resolution_zoom_cap"]
