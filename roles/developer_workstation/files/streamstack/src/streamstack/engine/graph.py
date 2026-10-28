"""The filter graph and the runtime commands, as pure functions.

One ffmpeg process runs the whole camera path, so a slider never restarts anything: every setting that can move
while the call is running is a *command* sent to a named filter instance over ffmpeg's stdin (`c<target> -1 <name>
<value>`), applied before the next frame. Names used here (filter@instance):

    libplacebo@grade   Vulkan: crop window, scale to the output size, brightness / contrast / saturation / gamma,
                       and warmth (libplacebo's colour temperature, set through `extra_opts`)
    colorcorrect@tone  YUV:    the split-tone "cinematic" grade (teal lows, orange highs); warmth too on the CPU backend
    cas@sharp          YUV:    contrast-adaptive sharpening (the CPU path)

`colorcorrect` and `cas` sit behind the timeline `enable` switch, so at a neutral look they pass frames through
untouched. When no Vulkan device is usable the same controls run on the CPU (`eq@grade`, no crop), see `Backend`.

Sharpening has a GPU form too (`Spec.shaders`): a contrast-adaptive shader hooked into `libplacebo@grade`
(see shaders.py). libplacebo takes a shader at init only, so its strength is *baked* into the graph, in steps of
`SHARP_STEP`; `GraphKey` is what is baked, and the supervisor rebuilds the child when a look changes it. At 1080p the
CPU `cas` costs about 260 % of a core; the shader costs a few points (docs/native-video-engine.md).

Background blur (`Spec.blur`) adds one more input, the person mask (a raw yuv444p stream fed by
`streamstack.engine.blurfeed`), a low-rate raw RGB output the segmenter reads, and two commandable filters:

    gblur@bg           YUV:    Gaussian blur of the background copy (sigma = the strength slider)
    maskedmerge@blur   YUV:    blurred copy where the mask is white, the sharp picture elsewhere (`enable` = on/off)

Seams for later work are marked in `build_graph`: `pre_grade` (a GPU filter list) and `taps` (encode branches, see
encode.py).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# v4l2 fourcc -> ffmpeg input format (raw formats the engine uploads as they are)
RAW_FORMATS = {"YUYV": "yuyv422", "NV12": "nv12", "YU12": "yuv420p", "UYVY": "uyvy422"}
MIN_SIDE = 64
MAX_ZOOM = 8.0

GRADE = "libplacebo@grade"
CPU_GRADE = "eq@grade"
TONE = "colorcorrect@tone"
SHARP = "cas@sharp"
BLUR_BG = "gblur@bg"
BLUR_MERGE = "maskedmerge@blur"

WARMTH_SPAN = 0.35  # colorcorrect shift at warmth = +-1 (CPU backend)
SPLIT_SPAN = 0.25  # colorcorrect shift at lut_amount = 1
SHARP_STEP = 0.05  # the GPU sharpen strengths are baked in these steps (one shader file each)
WARM_TEMPERATURE = -1.0  # libplacebo temperature at warmth = +1 (negative is warmer; its range is -1.143 to 5)
COOL_TEMPERATURE = 5.0  # ... at warmth = -1 (libplacebo's cool side tops out at a mild shift, see the docs)


class GraphError(ValueError):
    """The source or settings cannot make a graph (unsupported pixel format, empty size)."""


@dataclass(frozen=True)
class Source:
    """One camera input: where it is and the mode the driver delivers."""

    label: str
    device: str
    fourcc: str
    width: int
    height: int
    fps: float

    @property
    def mjpeg(self) -> bool:
        return self.fourcc == "MJPG"

    @property
    def pix_fmt(self) -> str:
        if self.mjpeg:
            return "mjpeg"
        try:
            return RAW_FORMATS[self.fourcc]
        except KeyError as exc:
            raise GraphError(
                f"{self.label}: {self.fourcc} is not a format the engine reads (YUYV, NV12, YU12, UYVY, MJPG)"
            ) from exc


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class Tap:
    """An encode branch fed by the program picture (see encode.py for the encoder choice)."""

    url: str
    encoder: str
    args: tuple[str, ...] = ()
    format: str = "matroska"


@dataclass(frozen=True)
class BlurGraph:
    """The background-blur branch: the size of the picture the segmenter sees and of the mask it sends back.

    The model sees the whole camera frame squeezed to `seg_size` square (`seg_size` is the model's input). The mask
    comes back at `mask_width` x `mask_height` (the output's aspect) as yuv444p, so the same three planes serve
    `maskedmerge`; the background is blurred at 1/`down` of the output size, which is what makes a wide blur cheap.
    """

    seg_size: int = 192
    mask_width: int = 320
    mask_height: int = 180
    down: int = 4


@dataclass(frozen=True)
class Spec:
    source: Source
    out_device: str
    out_width: int
    out_height: int
    backend: str = "vulkan"  # vulkan | cpu
    render_node: str = "/dev/dri/renderD128"
    detect_fps: float = 8.0
    detect_width: int = 320
    lut: str = ""  # optional static .cube 3D LUT, applied on the GPU
    # Directory of generated GPU shaders (see shaders.py). Set: sharpening runs on the GPU in baked steps and the
    # graph is rebuilt when a look moves to another step. Empty: sharpening is the live CPU `cas` filter.
    shaders: str = ""
    pre_grade: tuple[str, ...] = ()
    taps: tuple[Tap, ...] = ()
    output_format: str = "yuyv422"
    # Replaces the v4l2 input (all input options and "-i ..."): the benchmark and tests feed raw frames this way.
    input_override: tuple[str, ...] = ()
    blur: BlurGraph | None = None


@dataclass(frozen=True)
class GraphKey:
    """What a look bakes into the graph (the parts libplacebo takes at init only). A new key means a new child."""

    sharpen: float = 0.0  # GPU sharpen strength, a multiple of SHARP_STEP; 0 = no sharpen stage
    split: bool = False  # the colorcorrect split-tone stage is in the graph


@dataclass(frozen=True)
class Command:
    target: str
    name: str
    value: str

    def line(self) -> str:
        return f"c{self.target} -1 {self.name} {self.value}\n"


def fnum(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


# ---------------------------------------------------------------------------
# Looks and windows -> commands
# ---------------------------------------------------------------------------


def grade_values(look: dict, blackout: bool = False) -> dict[str, float]:
    """libplacebo/eq controls from a Console look (brightness/contrast/gamma/saturation, each -1..1 style)."""
    if blackout:
        return {"brightness": -1.0, "contrast": 0.0, "saturation": 0.0, "gamma": 1.0}
    return {
        "brightness": float(look.get("brightness", 0.0)),
        "contrast": 1.0 + float(look.get("contrast", 0.0)),
        "saturation": 1.0 + float(look.get("saturation", 0.0)),
        "gamma": 2.0 ** float(look.get("gamma", 0.0)),
    }


def warmth_temperature(warmth: float) -> float:
    """libplacebo colour temperature for the warmth slider (-1 cool .. +1 warm; 0 is neutral).

    libplacebo's scale is inverted (negative is warmer) and lopsided: -1 is a strong orange cast, +5 only a mild
    blue one (a white-point shift saturates). The warm side is matched to what the CPU `colorcorrect` gave (red up
    and blue down by about the same amount at +-1); the cool side uses the whole range libplacebo has.
    """
    warmth = min(1.0, max(-1.0, warmth))
    return (warmth * WARM_TEMPERATURE if warmth >= 0 else -warmth * COOL_TEMPERATURE) + 0.0  # + 0.0: never "-0"


def split_values(look: dict) -> float:
    """The split-tone (teal lows, orange highs) shift; 0 when the look has none."""
    return float(look.get("lut_amount", 0.0)) * SPLIT_SPAN if look.get("lut_enabled") else 0.0


def tone_values(look: dict, warmth: bool = True) -> dict[str, float]:
    """colorcorrect spots: warmth shifts shadows and highlights together, the LUT grade splits them.

    `warmth=False` is the Vulkan backend, where warmth is libplacebo's colour temperature and colorcorrect only
    carries the split-tone.
    """
    warm = float(look.get("warmth", 0.0)) * WARMTH_SPAN if warmth else 0.0
    split = split_values(look)
    return {"rl": warm - split, "bl": -warm + split, "rh": warm + split, "bh": -warm - split}


def sharp_values(look: dict) -> tuple[bool, float]:
    on = bool(look.get("sharpen_enabled"))
    return on, float(look.get("sharpen_amount", 0.0)) if on else 0.0


def sharpen_level(look: dict) -> float:
    """The GPU sharpen strength for a look: its amount rounded to a step (never 0 while sharpening is on)."""
    on, amount = sharp_values(look)
    if not on or amount <= 0:
        return 0.0
    steps = max(1, round(min(1.0, amount) / SHARP_STEP))
    return round(steps * SHARP_STEP, 2)


def graph_key(spec: Spec, look: dict) -> GraphKey | None:
    """What `look` bakes into the graph of `spec`, or None when nothing is baked (CPU sharpen, CPU backend)."""
    if spec.backend != "vulkan" or not spec.shaders:
        return None
    return GraphKey(sharpen=sharpen_level(look), split=abs(split_values(look)) > 1e-4)


def look_controls(
    look: dict, backend: str = "vulkan", blackout: bool = False, key: GraphKey | None = None
) -> dict[tuple[str, str], str]:
    """Every (target, command name) -> value the look needs. Diff two of these to get the minimal commands.

    `key` is the baked graph the child was built with (`graph_key`); None means the graph with the live CPU
    colorcorrect and cas filters. With a key, sharpening is in the shader and colorcorrect exists only when the key
    says so, so no command goes to a filter that is not there.
    """
    controls: dict[tuple[str, str], str] = {}
    grade = grade_values(look, blackout)
    if backend == "vulkan":
        for name, value in grade.items():
            controls[(GRADE, name)] = fnum(value)
        controls[(GRADE, "extra_opts")] = f"temperature={fnum(warmth_temperature(float(look.get('warmth', 0.0))))}"
    else:
        # eq: brightness -1..1, contrast 0..2 (-1000..1000 allowed), saturation 0..3, gamma 0.1..10
        for name, value in grade.items():
            controls[(CPU_GRADE, name)] = fnum(value)
    if key is not None and not key.split:
        return controls
    tone = tone_values(look, warmth=backend != "vulkan")
    tone_active = any(abs(v) > 1e-4 for v in tone.values())
    for name, value in tone.items():
        controls[(TONE, name)] = fnum(value)
    controls[(TONE, "enable")] = "1" if tone_active else "0"
    if key is None:
        sharp_on, amount = sharp_values(look)
        controls[(SHARP, "strength")] = fnum(amount)
        controls[(SHARP, "enable")] = "1" if sharp_on and amount > 0 else "0"
    return controls


BLUR_MIN, BLUR_MAX = 1, 20


def blur_sigma(strength: int | float) -> float:
    """gblur sigma, in pixels of the 1/4-size background copy, for the 1..20 strength slider."""
    return 0.5 + 0.6 * min(BLUR_MAX, max(BLUR_MIN, float(strength)))


def blur_controls(on: bool, strength: int | float) -> dict[tuple[str, str], str]:
    """The blur filters' commandable controls. Off leaves the merge disabled, so the sharp picture passes through."""
    return {(BLUR_BG, "sigma"): fnum(blur_sigma(strength)), (BLUR_MERGE, "enable"): "1" if on else "0"}


def diff_controls(old: dict[tuple[str, str], str] | None, new: dict[tuple[str, str], str]) -> list[Command]:
    """The commands that take the filters from `old` to `new`; all of them when `old` is None."""
    commands = []
    for (target, name), value in new.items():
        if old is None or old.get((target, name)) != value:
            commands.append(Command(target, name, value))
    # an `enable` that turns a filter on goes last, an `enable` that turns it off goes first: no half-set frame
    commands.sort(key=lambda c: (c.name == "enable") * (1 if c.value == "1" else -1))
    return commands


def window_rect(cx: float, cy: float, zoom: float, width: int, height: int) -> Rect:
    """The pixel rectangle of a framing window (centre and zoom in frame units), even-sized and inside the frame."""
    zoom = min(MAX_ZOOM, max(1.0, zoom))
    w = max(MIN_SIDE, round(width / zoom / 2) * 2)
    h = max(MIN_SIDE, round(height / zoom / 2) * 2)
    w, h = min(w, width), min(h, height)
    x = round(cx * width - w / 2)
    y = round(cy * height - h / 2)
    x = min(max(0, x), width - w)
    y = min(max(0, y), height - h)
    return Rect(x - x % 2, y - y % 2, w, h)


def crop_commands(rect: Rect, grow: bool = True) -> list[Command]:
    """libplacebo crop. Growing sends size first, shrinking sends the origin first, so a frame caught between two
    commands never sees a box outside the picture."""
    size = [("crop_w", rect.w), ("crop_h", rect.h)]
    origin = [("crop_x", rect.x), ("crop_y", rect.y)]
    return [Command(GRADE, name, str(value)) for name, value in (size + origin if grow else origin + size)]


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------


def _grade_filter(
    spec: Spec, look: dict, rect: Rect | None, blackout: bool, key: GraphKey | None = None, out_format: str = "yuv444p"
) -> str:
    grade = grade_values(look, blackout)
    if spec.backend == "vulkan":
        opts = [
            f"w={spec.out_width}",
            f"h={spec.out_height}",
            f"format={out_format}",
            "upscaler=ewa_lanczos",
            "downscaler=ewa_lanczos",
        ]
        if rect is not None:
            opts += [f"crop_x={rect.x}", f"crop_y={rect.y}", f"crop_w={rect.w}", f"crop_h={rect.h}"]
        opts += [f"{k}={fnum(v)}" for k, v in grade.items()]
        opts.append(f"extra_opts=temperature={fnum(warmth_temperature(float(look.get('warmth', 0.0))))}")
        if spec.lut:
            opts += [f"lut={_quote(spec.lut)}", "lut_type=conversion"]
        if key is not None and key.sharpen > 0:
            opts.append(f"custom_shader_path={_quote(sharpen_shader_path(spec.shaders, key.sharpen))}")
        return f"libplacebo@grade={':'.join(opts)}"
    sets = ":".join(f"{k}={fnum(v)}" for k, v in grade.items())
    return f"scale={spec.out_width}:{spec.out_height}:flags=bicubic,format=yuv444p,eq@grade={sets}:eval=frame"


UPLOAD_FORMATS = {"yuyv422": "yuv422p", "uyvy422": "yuv422p", "nv12": "nv12", "yuv420p": "yuv420p"}


def upload_format(src: Source) -> str:
    """The pixel format a raw camera goes up to the GPU in: its own chroma layout, planar (4:2:2 stays 4:2:2)."""
    return UPLOAD_FORMATS[src.pix_fmt]


def sharpen_shader_path(directory: str, level: float) -> str:
    """Where the shader for `level` lives (shaders.ensure_sharpen writes it); a pure function of the two."""
    return f"{directory.rstrip('/')}/cas-{level:.2f}.hook"


def _quote(path: str) -> str:
    if any(c in path for c in "':,;[]\\ "):
        raise GraphError(f"{path!r}: a LUT path may not contain spaces or filter-syntax characters")
    return path


def _yuv_stage(look: dict, backend: str, key: GraphKey | None) -> str:
    """The CPU stages after the grade: colorcorrect (split-tone; warmth too on the CPU backend) and cas.

    With a key the sharpen is in the shader and colorcorrect is in the graph only when the key says so; the empty
    string means no CPU stage at all.
    """
    parts = []
    if key is None or key.split:
        tone = tone_values(look, warmth=backend != "vulkan")
        tone_opts = ":".join(f"{k}={fnum(v)}" for k, v in tone.items())
        tone_on = any(abs(v) > 1e-4 for v in tone.values())
        parts.append(f"colorcorrect@tone={tone_opts}:enable={int(tone_on)}")
    if key is None:
        sharp_on, amount = sharp_values(look)
        parts.append(f"cas@sharp=strength={fnum(amount)}:enable={int(sharp_on and amount > 0)}")
    return ",".join(parts)


def build_detect_graph(spec: Spec) -> str:
    """The detection graph: low-rate, uncropped JPEG frames for the framing detector.

    It is a graph of its own on purpose. Splitting the input inside the program graph adds 40 to 80 ms to the
    program path (measured, see docs/native-video-engine.md); a second graph reading the same stream costs nothing.
    """
    tail = f"fps={fnum(spec.detect_fps)},scale={spec.detect_width}:-2:flags=fast_bilinear,format=yuvj420p[det]"
    if spec.backend == "vulkan" and spec.source.mjpeg:
        # decoded on the Arc by VA-API; shrink there and read back the small picture only
        return f"[0:v]scale_vaapi=w={spec.detect_width}:h=-2,hwdownload,format=nv12,{tail}"
    return f"[0:v]{tail}"


def build_seg_graph(spec: Spec) -> str:
    """The segmenter's graph: every frame, uncropped, squeezed to the model's square input as raw RGB.

    Like the detection graph it is a graph of its own: the program graph is not split, so it keeps its latency.
    """
    if spec.blur is None:
        raise GraphError("this spec has no blur branch")
    side = spec.blur.seg_size
    if spec.backend == "vulkan" and spec.source.mjpeg:
        return f"[0:v]scale_vaapi=w={side}:h={side},hwdownload,format=nv12,format=rgb24[seg]"
    return f"[0:v]scale={side}:{side}:flags=fast_bilinear,format=rgb24[seg]"


def _blur_branch(spec: Spec, on: bool, strength: float) -> str:
    """The graph text that follows the `split[fg][bgs]`: background copy, mask, and the merge.

    The picture is split; one copy is shrunk, blurred and grown again (a wide blur at 1/4 the cost); the mask is
    feathered at its own small size, then grown to the output size. `setpts=N/fps` on both the picture and the mask
    makes them pair by frame number, not by clock, so a camera that runs at 29.97 never drifts out of step.
    """
    blur = spec.blur
    if blur is None:
        raise GraphError("this spec has no blur branch")
    w, h = spec.out_width, spec.out_height
    small_w, small_h = max(2, w // blur.down // 2 * 2), max(2, h // blur.down // 2 * 2)
    sigma = fnum(blur_sigma(strength))
    return (
        f";[bgs]scale={small_w}:{small_h}:flags=bilinear,gblur@bg=sigma={sigma}:steps=2,"
        f"scale={w}:{h}:flags=bilinear[bg];"
        f"[1:v]gblur=sigma=1.5,scale={w}:{h}:flags=bilinear[m];"
        f"[fg][bg][m]maskedmerge@blur=enable={int(on)}"
    )


def build_graph(
    spec: Spec,
    look: dict,
    rect: Rect | None = None,
    blackout: bool = False,
    blur: tuple[bool, float] | None = None,
    key: GraphKey | None = None,
) -> str:
    """The program `-filter_complex` string. Output: [prog] (the camera), plus [tap0..] encode branches.

    [0:v] -> pre_grade -> libplacebo@grade -> hwdownload -> (blur) -> (colorcorrect -> cas) -> [prog] (+ taps)

    On the Vulkan backend the camera goes up as 4:2:2 (4:2:0 for NV12 and YU12 sources) and comes back as 4:2:2
    when nothing on the CPU needs whole planes. The parenthesised CPU stages are present as `_yuv_stage` says.

    `blur` is (on, strength) for a spec with a blur branch; the branch is always in the graph (switching it on or off
    is a command, not a new graph) and costs a few milliseconds of CPU per frame while it is.
    """
    src = spec.source
    src.pix_fmt  # noqa: B018 - raises GraphError for a format the engine cannot read
    if src.width <= 0 or src.height <= 0:
        raise GraphError("the source has no size")
    if spec.blur is not None and blur is None:
        blur = (False, 8)
    key = key or graph_key(spec, look)
    # frame-number timestamps pair the picture with its mask (see _blur_branch)
    stamp = f"setpts=N/({fnum(src.fps)}*TB)," if spec.blur is not None else ""
    yuv = _yuv_stage(look, spec.backend, key)
    if spec.backend == "vulkan":
        # MJPEG is decoded on the Arc by VA-API and mapped into Vulkan without leaving video memory
        head = f"[0:v]{stamp}" + (
            "hwmap=derive_device=vulkan" if src.mjpeg else f"format={upload_format(src)},hwupload"
        )
        # 4:4:4 when a CPU stage or the blur merge needs whole planes; otherwise the camera's own 4:2:2 comes back
        out_format = "yuv444p" if yuv or spec.blur is not None else "yuv422p"
        grade = _grade_filter(spec, look, rect, blackout, key, out_format)
        stages = [*spec.pre_grade, grade, "hwdownload", f"format={out_format}"]
    else:
        if src.mjpeg:
            raise GraphError("MJPEG cameras need the Vulkan/VA-API backend (the CPU backend reads raw formats only)")
        head = f"[0:v]{stamp}null"
        stages = [*spec.pre_grade, _grade_filter(spec, look, None, blackout)]
    if spec.blur is not None and blur is not None:
        chain = ",".join([head, *stages]) + ",split[fg][bgs]" + _blur_branch(spec, blur[0], blur[1])
        if yuv:
            chain += "," + yuv
    else:
        chain = ",".join([head, *stages, *([yuv] if yuv else [])])
    tail_in = f"{chain},format={spec.output_format}"
    if spec.taps:
        outs = ["prog"] + [f"t{i}" for i in range(len(spec.taps))]
        labels = "".join(f"[{o}]" for o in outs)
        graph = f"{tail_in},split={len(outs)}{labels}"
        for i, _ in enumerate(spec.taps):
            graph += f";[t{i}]format=nv12[tap{i}]"
        return graph
    return f"{tail_in}[prog]"


def input_args(spec: Spec) -> list[str]:
    src = spec.source
    args = ["-f", "v4l2", "-thread_queue_size", "4", "-use_wallclock_as_timestamps", "1", "-framerate", fnum(src.fps)]
    args += ["-video_size", f"{src.width}x{src.height}"]
    if src.mjpeg:
        args += ["-input_format", "mjpeg"]
    else:
        args += ["-input_format", src.pix_fmt]
    return args


def mask_input_args(spec: Spec, fd: int) -> list[str]:
    """The person mask as a second input: raw yuv444p frames on an inherited pipe, one per camera frame."""
    blur = spec.blur
    if blur is None:
        raise GraphError("this spec has no blur branch")
    return [
        "-f",
        "rawvideo",
        "-pixel_format",
        "yuv444p",
        "-video_size",
        f"{blur.mask_width}x{blur.mask_height}",
        "-framerate",
        fnum(spec.source.fps),
        "-thread_queue_size",
        "4",
        "-i",
        f"/dev/fd/{fd}",
    ]


def build_argv(
    spec: Spec,
    look: dict,
    rect: Rect | None = None,
    blackout: bool = False,
    progress_fd: int | None = None,
    blur: tuple[bool, float] | None = None,
    blur_fds: tuple[int, int] | None = None,
    key: GraphKey | None = None,
) -> list[str]:
    """The whole ffmpeg command line for `spec`. stdin carries commands, stdout carries the detection JPEGs.

    A spec with a blur branch also needs `blur_fds` = (the pipe ffmpeg writes camera frames to for the segmenter,
    the pipe it reads the mask from): both are inherited descriptors, see `VideoEngine`.
    """
    if spec.blur is not None and blur_fds is None:
        raise GraphError("a blur branch needs its two pipes (blur_fds)")
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats", "-y"]
    if progress_fd is not None:
        argv += ["-progress", f"pipe:{progress_fd}", "-stats_period", "0.02"]
    argv += ["-fflags", "nobuffer", "-flags", "low_delay"]
    if spec.backend == "vulkan":
        argv += [
            "-init_hw_device",
            f"vaapi=va:{spec.render_node}",
            "-init_hw_device",
            "vulkan=vk@va",
            "-filter_hw_device",
            "vk",
        ]
        if spec.source.mjpeg:
            argv += ["-hwaccel", "vaapi", "-hwaccel_device", "va", "-hwaccel_output_format", "vaapi"]
    if any(tap.encoder.endswith("_qsv") for tap in spec.taps):
        # the Quick Sync encoders open their own device: without this they take the first GPU libva finds, which on
        # a machine with an integrated GPU is not the one the graph runs on
        argv += ["-init_hw_device", f"qsv=qs:hw,child_device={spec.render_node},child_device_type=vaapi"]
    argv += list(spec.input_override) or [*input_args(spec), "-i", spec.source.device]
    if spec.blur is not None and blur_fds is not None:
        argv += mask_input_args(spec, blur_fds[1])
    argv += ["-filter_complex", build_graph(spec, look, rect, blackout, blur, key)]
    argv += ["-filter_complex", build_detect_graph(spec)]
    if spec.blur is not None:
        argv += ["-filter_complex", build_seg_graph(spec)]
    argv += [
        "-map",
        "[prog]",
        "-fps_mode",
        "passthrough",
        "-pix_fmt",
        spec.output_format,
        "-f",
        "v4l2",
        spec.out_device,
    ]
    argv += ["-map", "[det]", "-fps_mode", "passthrough", "-c:v", "mjpeg", "-q:v", "5", "-f", "image2pipe", "pipe:1"]
    if spec.blur is not None and blur_fds is not None:
        argv += ["-map", "[seg]", "-fps_mode", "passthrough", "-c:v", "rawvideo", "-pix_fmt", "rgb24"]
        # -flush_packets: without it the last 12 KB of every frame waits in ffmpeg's I/O buffer for the next frame
        argv += ["-flush_packets", "1", "-f", "rawvideo", f"pipe:{blur_fds[0]}"]
    for i, tap in enumerate(spec.taps):
        argv += ["-map", f"[tap{i}]", "-c:v", tap.encoder, *tap.args, "-f", tap.format, tap.url]
    return argv


def changed_pixels(a: Rect, b: Rect) -> int:
    return max(abs(a.x - b.x), abs(a.y - b.y), abs(a.w - b.w), abs(a.h - b.h))


def smoothstep(mix: float) -> float:
    mix = min(1.0, max(0.0, mix))
    return mix * mix * (3 - 2 * mix)


def blend_looks(start: dict, target: dict, mix: float) -> dict:
    """A look part-way between two: numbers eased, flags jump half-way (a LUT never flashes at full strength)."""
    out = dict(target)
    for key, value in target.items():
        if isinstance(value, bool) or key not in start or isinstance(start[key], bool):
            continue
        out[key] = start[key] + (value - start[key]) * smoothstep(mix)
    for key in ("sharpen_enabled", "lut_enabled"):
        if key in target and key in start:
            out[key] = target[key] if mix >= 0.5 else start[key]
    return {k: (round(v, 4) if isinstance(v, float) and not math.isnan(v) else v) for k, v in out.items()}
