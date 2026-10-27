# Auto-framing

A face detector runs beside the native video engine and moves a smooth crop and zoom on the camera, so you stay
framed without a camera operator. It adds no video latency: detection works on a small, low-rate copy of the picture
and never sits in the video path. The engine keeps rendering the full-rate camera; only its crop window changes.

```
engine (uncropped frame, 320 px, ~8 Hz) --> detector (YuNet, OpenVINO: Arc GPU or CPU)
                                                |
        set_window (crop only, 20 Hz while moving) <-- framing engine (dead zone, smoothing, composition)
```

## Modes

| Mode | What it does | Zoom limit |
|---|---|---|
| Off | The camera picture as it is. Any crop StreamStack made is eased away and the original restored. | 1.0 |
| Gentle | Slow, small corrections. Keeps you comfortably in the picture. | 1.25 |
| Tight | Closer on your face; still moves rarely and slowly. | 1.8 |
| Follow | Stays with you when you lean or move around. | 1.5 |

The zoom limit is also capped by the camera's resolution: the picture is never enlarged more than 2x in the
output. A 704-line Canon live view drawn 1080 lines tall gets about 1.3x; a 1080p Cam Link gets up to the mode's
limit. Hold freezes the crop where it is (and stops detecting). Recentre forgets the crop and finds you again.

## Install

```
pip install 'streamstack[framing]'     # numpy, opencv-python-headless, openvino
streamstack framing --fetch            # downloads and verifies the face model (once)
streamstack framing                    # shows backends, device and the model
streamstack framing --probe            # times a detection on a synthetic picture (no camera is touched)
```

Without the extra, or without the model, StreamStack works exactly as before: the Console shows the control
disabled with the fix, and `streamstack doctor` reports `Auto-framing is not installed (optional)`.

## Device

`STREAMSTACK_FRAMING_DEVICE` is `auto` (default), `gpu` or `cpu`. `auto` uses the Intel GPU when OpenVINO lists
one (the Arc A380 shows as `GPU.0` once the compute runtime is installed) and the CPU otherwise. When the GPU
cannot compile the model it falls back to the CPU. When OpenVINO is missing it uses OpenCV's DNN module on the
CPU. When nothing works, framing says unavailable, switches itself off and tells you why. A face detection takes
about 2 to 5 ms on a CPU core at 320 px; the Console health check shows the device and the measured time and
warns above 40 ms.

Other settings: `STREAMSTACK_FRAMING_MODE` (`off` default; the mode saved from the Console wins),
`STREAMSTACK_FRAMING_FPS` (detections per second, 5 to 10).

## Model and licence

| | |
|---|---|
| Model | YuNet `face_detection_yunet_2023mar.onnx` |
| Source | OpenCV Zoo, pinned commit `f12e12798e8314f7c074a6656816c048dcc95b7a` |
| Size / SHA-256 | 232,589 bytes / `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |
| Licence | MIT, Copyright (c) 2020 Shiqi Yu (the YuNet authors); the OpenCV Zoo repository is also MIT |

The model is not shipped in the wheel. `streamstack framing --fetch` downloads it over HTTPS from the pinned
URL (size-capped), checks the SHA-256 and moves it into `$XDG_DATA_HOME/streamstack/models/` atomically; a file
that fails the check is refused and never used. The file is checked again before it is loaded.

## How the crop reaches the engine

The engine reads one camera, so there is one picture to crop. StreamStack sends the window to the engine child as a
`set_window` command (margins in source pixels, keeping the frame's aspect ratio, so the picture is zoomed and not
distorted). libplacebo evaluates the crop between two frames: the window moves without a restart and the crop and
the rescale happen on the GPU as part of the frame the engine was drawing anyway. Detection reads the engine's own
uncropped frame, so the crop never feeds back into the detector. While the engine is off air (camera absent, no
signal, privacy blackout) nothing is cropped or detected, and the window resumes without a jump.

Nothing is persisted: a new engine child always starts from the full frame, so turning framing off, stopping the
Console or a crash cannot leave the picture cropped. The Console state lists anything it could not crop under
`framing.skipped`.

## The director's face finder

The AI Director (`docs/auto-mode.md`) reuses this detector for metering the face: the same YuNet model and backends,
opened on the CPU so it never competes with live framing for the GPU. It is lazy (nothing loads until the first
still), and a missing extra or model falls back to the skin-colour heuristic, which the Auto page reports.
`STREAMSTACK_AUTO_FACES=false` keeps the heuristic.

## Actuator API (for the AI Director, issue #27)

In process, on the Console rig (`rig.framing`); the same calls exist on the demo simulation.

```python
rig.framing.set_mode("tight")   # "off" | "gentle" | "tight" | "follow"; ActionError 409 if the backends are missing
rig.framing.hold(True)          # freeze / release the crop; ActionError 409 while off
rig.framing.reset()             # recentre: forget the crop and find the face again
rig.framing.status()            # the `framing` state object below, no side effects
```

Over HTTP, the actions `framing.mode` (`mode`), `framing.hold` (`on`) and `framing.reset` (docs/console.md).
They are idempotent, safe to repeat, and bounded by the engine's own smoothing and zoom limits, so a director can
call them at any rate without making the picture jump. Errors are `ActionError` with an HTTP-style status and a
message fit to show to a person.

State (`framing` in the snapshot): `mode`, `state` (`off`, `starting`, `tracking`, `held`, `no-face`,
`unavailable`), `held`, `available`, `unavailable_hint`, `reason`, `backend`, `device`, `detect_ms`,
`detect_p95_ms`, `fps`, `model`, `zoom`, `window` (`cx`, `cy`, `zoom` in frame units), `sources[]`, `skipped[]`,
`modes[]`.

## Engine

`streamstack.framing.engine` is pure Python with no I/O: 5 to 10 Hz detections go through a One Euro filter, a
dead zone that ignores small movement (so a still head does not make the crop breathe), headroom and rule-of-thirds
composition (eye line near the upper third), and a critically damped spring with a speed limit that moves the
window at the display rate. The same code runs against synthetic face tracks in `tests/test_framing_engine.py`.

## Host packages

The exact packages for the workstation are tracked in Curiosity-Corp/catalog#101. The
Arc GPU path needs the Intel compute runtime in addition to `openvino`; the CPU path needs only the Python
extra.
