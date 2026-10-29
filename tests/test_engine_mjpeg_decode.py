"""How a Motion-JPEG camera is decoded: the probe, its cache, the graph for each path and the Console's choice."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from streamstack import config
from streamstack.console.arcblur import ArcBlur
from streamstack.console.native import NativeVideo
from streamstack.engine import graph, probe, source
from streamstack.engine.graph import Source, Spec
from streamstack.engine.segment import Arc

NEUTRAL = {"brightness": 0.0, "contrast": 0.0, "gamma": 0.0, "saturation": 0.0, "warmth": 0.0}
MJPEG = Source("Cam Link 4K", "/dev/video0", "MJPG", 1920, 1080, 30.0)
RAW = Source("Canon", "/dev/video42", "YUYV", 1280, 720, 20.0)
NODE = "/dev/dri/renderD128"


PICTURE = bytes(range(256)) * (probe.PROBE_FRAME_BYTES // 256)  # what a correct tier writes: the reference picture


class FakeFfmpeg:
    """Stands in for subprocess.run: records the commands. `va_ok` is the zero-copy import, `copy_ok` the VA decode
    with a read-back; either fails with libplacebo's own error plus ffmpeg's generic last line. `mapping_ok` is the
    VA to Vulkan `hwmap` alone (it works on the Arc), `zero_copy_picture` what a zero-copy run that exits 0 writes."""

    def __init__(
        self,
        va_ok: bool,
        copy_ok: bool = True,
        mapping_ok: bool = True,
        zero_copy_picture: bytes = PICTURE,
        reference_ok: bool = True,
    ) -> None:
        self.va_ok, self.copy_ok, self.mapping_ok = va_ok, copy_ok, mapping_ok
        self.zero_copy_picture, self.reference_ok = zero_copy_picture, reference_ok
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        if argv[0] == "dpkg-query":
            return subprocess.CompletedProcess(argv, 0, b"mesa-vulkan-drivers 26.0.8\nlibva2 2.23.0\n", b"")
        if "-version" in argv:
            return subprocess.CompletedProcess(argv, 0, b"ffmpeg version 8.0.1\nbuilt with gcc\n", b"")
        graph_text = argv[argv.index("-vf") + 1] if "-vf" in argv else ""
        if "-hwaccel" not in argv:  # the JPEG maker and the software reference
            if "libplacebo" in graph_text and not self.reference_ok:
                return subprocess.CompletedProcess(argv, 1, b"", b"Error: no Vulkan device\n")
            return subprocess.CompletedProcess(argv, 0, PICTURE if graph_text else b"", b"")
        zero_copy = "-hwaccel_output_format" in argv
        if "libplacebo" not in graph_text:  # the mapping check, no libplacebo
            if self.mapping_ok:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return subprocess.CompletedProcess(argv, 1, b"", b"Failed to map frame: -38\nConversion failed!\n")
        if not (self.va_ok if zero_copy else self.copy_ok):
            err = b"first\nvk->CreateImage: VK_ERROR_OUT_OF_DEVICE_MEMORY\nNothing was written into output file\n"
            return subprocess.CompletedProcess(argv, 187, b"", err)
        return subprocess.CompletedProcess(argv, 0, self.zero_copy_picture if zero_copy else PICTURE, b"")

    def decodes(self, zero_copy: bool | None = None) -> int:
        """VA-API decodes that go through libplacebo (the mapping-only check is not counted)."""
        found = [c for c in self.calls if "-hwaccel" in c and "libplacebo" in c[c.index("-vf") + 1]]
        return len([c for c in found if zero_copy is None or ("-hwaccel_output_format" in c) == zero_copy])


def test_the_probe_tries_zero_copy_first_with_both_subsamplings(tmp_path: Path) -> None:
    fake = FakeFfmpeg(va_ok=True)
    assert probe.probe_mjpeg(NODE, run=fake, cache=tmp_path / "p.json") == ("vaapi", "")
    assert fake.decodes() == 2 and fake.decodes(zero_copy=True) == 2  # 4:2:0 and 4:2:2, no further tier
    decode = next(c for c in fake.calls if "-hwaccel" in c and "-vf" in c)
    assert f"vaapi=va:{NODE}" in decode and "hwmap=derive_device=vulkan,libplacebo" in decode[decode.index("-vf") + 1]


def test_a_failed_import_falls_to_the_copy_tier_and_says_why(tmp_path: Path) -> None:
    fake = FakeFfmpeg(va_ok=False)
    decode, why = probe.probe_mjpeg(NODE, run=fake, cache=tmp_path / "p.json")
    assert decode == "vaapi-copy" and "VK_ERROR_OUT_OF_DEVICE_MEMORY" in why  # the driver's line, not the last one
    assert fake.decodes(zero_copy=False) == 2
    copy = next(c for c in fake.calls if "-hwaccel" in c and "-hwaccel_output_format" not in c and "-vf" in c)
    assert "hwmap" not in copy[copy.index("-vf") + 1] and "hwupload" in copy[copy.index("-vf") + 1]


def test_software_is_the_last_tier(tmp_path: Path) -> None:
    decode, why = probe.probe_mjpeg(NODE, run=FakeFfmpeg(va_ok=False, copy_ok=False), cache=tmp_path / "p.json")
    assert decode == "software" and "VK_ERROR_OUT_OF_DEVICE_MEMORY" in why


def test_a_failed_import_is_not_reported_as_a_full_gpu(tmp_path: Path) -> None:
    """libplacebo's CreateImage failure reads VK_ERROR_OUT_OF_DEVICE_MEMORY. When the VA to Vulkan mapping alone works
    the probe says the dma-buf import was rejected, so nobody goes looking for a memory shortage (#138)."""
    fake = FakeFfmpeg(va_ok=False)
    decode, why = probe.probe_mjpeg(NODE, run=fake, cache=tmp_path / "p.json")
    assert decode == "vaapi-copy"
    assert why == probe.IMPORT_REJECTED and "not a memory shortage" in why and "mapping works" in why
    mapping = [c for c in fake.calls if "-hwaccel" in c and "hwdownload,format=yuv420p" in c[c.index("-vf") + 1]]
    assert len(mapping) == 1 and "libplacebo" not in mapping[0][mapping[0].index("-vf") + 1]


def test_a_mapping_that_fails_is_reported_as_the_mapping(tmp_path: Path) -> None:
    decode, why = probe.probe_mjpeg(NODE, run=FakeFfmpeg(va_ok=False, mapping_ok=False), cache=tmp_path / "p.json")
    assert decode == "vaapi-copy"
    assert why.startswith("the VA-API frame cannot be mapped into Vulkan") and "Failed to map frame" in why


def test_the_mapping_check_matches_the_subsampling_that_failed(tmp_path: Path) -> None:
    """A 4:2:2 JPEG that fails to import is mapped and read back as yuv422p, not relabelled as 4:2:0."""
    jpeg = tmp_path / "probe-yuvj422p.jpg"
    quiet = ["ffmpeg", "-loglevel", "error"]
    fake = FakeFfmpeg(va_ok=False)
    failed = subprocess.CompletedProcess([], 187, b"", b"vk->CreateImage: VK_ERROR_OUT_OF_DEVICE_MEMORY\n")
    why = probe._explain_import(failed, jpeg, "yuvj422p", quiet, [], fake)
    assert why == probe.IMPORT_REJECTED
    assert "hwdownload,format=yuv422p" in fake.calls[0][fake.calls[0].index("-vf") + 1]


def test_other_zero_copy_errors_are_reported_as_they_are(tmp_path: Path) -> None:
    failed = subprocess.CompletedProcess([], 1, b"", b"[vf] Failed to create the filter\n")
    fake = FakeFfmpeg(va_ok=False)
    assert (
        probe._explain_import(failed, tmp_path / "x.jpg", "yuvj420p", [], [], fake)
        == "[vf] Failed to create the filter"
    )
    assert fake.calls == []  # nothing to tell apart: no mapping check


def test_a_zero_copy_run_that_exits_0_with_the_wrong_picture_is_not_zero_copy(tmp_path: Path) -> None:
    """A stack can pass the exit code and still hand libplacebo an empty image (an all-green frame from a missing
    sync). The probe compares the picture with a software decode and falls to the copy tier."""
    green = bytes([0x4B] * probe.PROBE_FRAME_BYTES)
    decode, why = probe.probe_mjpeg(
        NODE, run=FakeFfmpeg(va_ok=True, zero_copy_picture=green), cache=tmp_path / "p.json"
    )
    assert decode == "vaapi-copy" and "differs from a software decode" in why


def test_a_zero_copy_run_that_writes_nothing_is_not_zero_copy(tmp_path: Path) -> None:
    decode, why = probe.probe_mjpeg(NODE, run=FakeFfmpeg(va_ok=True, zero_copy_picture=b""), cache=tmp_path / "p.json")
    assert decode == "vaapi-copy" and "wrote 0 bytes" in why


def test_a_picture_within_rounding_of_the_reference_passes(tmp_path: Path) -> None:
    near = bytes((b + 1) % 256 for b in PICTURE)
    assert probe.probe_mjpeg(NODE, run=FakeFfmpeg(va_ok=True, zero_copy_picture=near), cache=tmp_path / "p.json") == (
        "vaapi",
        "",
    )


def test_without_a_working_reference_every_tier_fails_with_its_error(tmp_path: Path) -> None:
    """No libplacebo on the software head means no Vulkan graph at all: software decode, with the reason."""
    decode, why = probe.probe_mjpeg(NODE, run=FakeFfmpeg(va_ok=True, reference_ok=False), cache=tmp_path / "p.json")
    assert decode == "software" and "no Vulkan device" in why


def test_a_new_libplacebo_is_a_new_stack(tmp_path: Path) -> None:
    """The failing import lives in libplacebo, so its package version is in the fingerprint the answer is kept under."""
    seen: list[list[str]] = []

    def run(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    probe.stack_fingerprint(NODE, run=run)
    dpkg = next(c for c in seen if c[0] == "dpkg-query")
    assert "libplacebo[0-9]*" in dpkg


def test_the_answer_is_kept_per_graphics_stack(tmp_path: Path) -> None:
    cache = tmp_path / "p.json"
    first = FakeFfmpeg(va_ok=False)
    assert probe.probe_mjpeg(NODE, run=first, cache=cache)[0] == "vaapi-copy"
    assert first.decodes() == 3 and cache.stat().st_mode & 0o777 == 0o600  # one zero-copy failure, then both copies
    again = FakeFfmpeg(va_ok=True)  # the stack did not change: the kept answer is used, nothing is run
    assert probe.probe_mjpeg(NODE, run=again, cache=cache)[0] == "vaapi-copy"
    assert again.decodes() == 0
    upgraded = FakeFfmpeg(va_ok=True)  # a new ffmpeg or mesa is a new stack: probe again
    original = upgraded.__call__

    def newer(argv, **kwargs):
        done = original(argv, **kwargs)
        return (
            subprocess.CompletedProcess(argv, 0, done.stdout.replace(b"8.0.1", b"8.1"), b"")
            if "-version" in argv
            else done
        )

    assert probe.probe_mjpeg(NODE, run=newer, cache=cache) == ("vaapi", "")
    assert len(json.loads(cache.read_text())) == 1  # the old stack's entry is dropped


def test_a_probe_that_could_not_run_is_not_kept(tmp_path: Path) -> None:
    def missing(argv, **kwargs):
        raise FileNotFoundError

    cache = tmp_path / "p.json"
    decode, why = probe.probe_mjpeg(NODE, run=missing, cache=cache)
    assert decode == "software" and "not installed" in why and not cache.exists()


def test_a_damaged_cache_is_probed_again(tmp_path: Path) -> None:
    cache = tmp_path / "p.json"
    cache.write_text("{not json")
    assert probe.probe_mjpeg(NODE, run=FakeFfmpeg(va_ok=True), cache=cache) == ("vaapi", "")


def test_the_copy_graph_reads_the_decoders_own_frames() -> None:
    spec = Spec(MJPEG, "/dev/video43", 1920, 1080, mjpeg_decode=graph.MJPEG_VAAPI_COPY)
    text = graph.build_graph(spec, NEUTRAL)
    assert (
        text.startswith(
            "[0:v]" + graph.lookahead_stamp(30.0) + "format=yuv420p|yuv422p|nv12,hwupload,libplacebo@grade="
        )
        and "hwmap" not in text
    )
    argv = graph.build_argv(spec, NEUTRAL)
    assert argv[argv.index("-hwaccel") : argv.index("-hwaccel") + 4] == ["-hwaccel", "vaapi", "-hwaccel_device", "va"]
    assert "-hwaccel_output_format" not in argv and "scale_vaapi" not in " ".join(argv)
    assert spec.decode_path == "vaapi-copy" and graph.build_detect_graph(spec).startswith("[0:v]fps=")


def test_the_software_graph_has_no_va_surfaces() -> None:
    spec = Spec(MJPEG, "/dev/video43", 1920, 1080, mjpeg_decode=graph.MJPEG_SOFTWARE)
    text = graph.build_graph(spec, NEUTRAL)
    assert (
        text.startswith(
            "[0:v]" + graph.lookahead_stamp(30.0) + "format=yuv420p|yuv422p|nv12,hwupload,libplacebo@grade="
        )
        and "hwmap" not in text
    )
    argv = graph.build_argv(spec, NEUTRAL)
    assert "-hwaccel" not in argv and "scale_vaapi" not in " ".join(argv)
    assert "-input_format" in argv and spec.decode_path == "software"
    detect = graph.build_detect_graph(spec)
    assert detect.startswith("[0:v]fps=") and "vaapi" not in detect


def test_the_va_graph_is_the_default_for_mjpeg() -> None:
    spec = Spec(MJPEG, "/dev/video43", 1920, 1080)
    assert "hwmap=derive_device=vulkan" in graph.build_graph(spec, NEUTRAL)
    assert "-hwaccel" in graph.build_argv(spec, NEUTRAL) and spec.decode_path == "vaapi"
    assert Spec(RAW, "/dev/video43", 1280, 720).decode_path == "none (raw)"


class StubEngine:
    def __init__(self, spec: Spec, look: dict | None = None, **kwargs) -> None:
        self.spec, self.running = spec, False

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def set_spec(self, spec: Spec) -> None:
        self.spec = spec

    def apply_look(self, look: dict) -> float:
        return 0.05

    def set_blackout(self, on: bool) -> None: ...

    def set_window(self, rect) -> None: ...

    def latest_frame(self, max_age: float = 3.0) -> bytes | None:
        return None

    def state(self) -> dict:
        return {"state": "running", "error": None, "backend": "vulkan", "decode": self.spec.decode_path}


def controller(tmp_path: Path, cfg_extra: dict, answer: tuple[str, str], src: Source):
    asked: list[str] = []
    made: list[StubEngine] = []

    def mjpeg(node: str) -> tuple[str, str]:
        asked.append(node)
        return answer

    def factory(spec: Spec, look: dict | None = None, **kwargs) -> StubEngine:
        made.append(StubEngine(spec, look))
        return made[-1]

    cfg = {**config.DEFAULTS, **cfg_extra}
    native = NativeVideo(
        cfg,
        arc=ArcBlur(cfg, probe=lambda wanted: Arc(False, "no")),
        state_file=tmp_path / "video.json",
        discover=lambda settings: [source.Camera(src.label, src.device, "camlink")],
        resolve=lambda camera, settings: src,
        engine_factory=factory,
        backend=lambda node: ("vulkan", ""),
        mjpeg=mjpeg,
    )
    native.start()
    end = time.monotonic() + 10
    while not made and time.monotonic() < end:
        time.sleep(0.01)
    assert made
    return native, made, asked


@pytest.mark.parametrize("path", ["software", "vaapi-copy"])
def test_the_console_uses_the_fallback_the_probe_picks_and_reports_it(tmp_path: Path, path: str) -> None:
    native, made, asked = controller(tmp_path, {}, (path, "libplacebo: import failed"), MJPEG)
    try:
        assert made[0].spec.mjpeg_decode == path and len(asked) == 1
        block = native.state()["engine"]
        assert block["decode"] == path and block["decode_note"] == "libplacebo: import failed"
        check = native.health_checks()[0]
        assert check["state"] == "ok" and f"{path} (zero-copy import into libplacebo fails here)" in check["detail"]
    finally:
        native.stop()


def test_the_setting_overrides_the_probe(tmp_path: Path) -> None:
    native, made, asked = controller(tmp_path, {"STREAMSTACK_ENGINE_MJPEG_DECODE": "vaapi"}, ("software", "x"), MJPEG)
    try:
        assert made[0].spec.mjpeg_decode == "vaapi" and asked == []
    finally:
        native.stop()


def test_a_raw_camera_never_runs_the_mjpeg_probe(tmp_path: Path) -> None:
    native, made, asked = controller(tmp_path, {}, ("software", "x"), RAW)
    try:
        assert asked == [] and made[0].spec.decode_path == "none (raw)"
    finally:
        native.stop()


def test_the_setting_accepts_the_three_paths() -> None:
    assert graph.MJPEG_DECODES == ("vaapi", "vaapi-copy", "software")
