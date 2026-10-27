"""Stills: AVIF snapshots, a JPEG XL archive, and lossless recompression of the camera's own JPEGs.

Three jobs, each with the best available tool and an honest check:

* `snapshot` writes any image as AVIF (the format every browser and phone opens) with ffmpeg's AV1 still encoder.
* `jpeg_to_jxl` recompresses a JPEG into JPEG XL *reversibly*: the JXL file holds the JPEG's DCT coefficients and
  the data to rebuild the original file, typically about 20 percent smaller. It calls libjxl directly (ctypes), the
  same code `cjxl` uses, so no pixels are decoded and re-encoded. `jxl_to_jpeg` rebuilds the original bytes.
* `archive` runs the first and then proves the second: it rebuilds the JPEG from the new file and compares it
  byte for byte with the original before it will keep the archive file (and only then, if asked, remove the JPEG).

None of this is a real-time path (about 0.6 s for a 1080p frame). Every function raises `StillsError` with a reason a
person can act on; nothing here guesses, falls back silently or leaves a half-written file behind.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

JPEG_SUFFIXES = (".jpg", ".jpeg", ".jpe")
CHUNK = 1 << 20
FFMPEG_TIMEOUT_S = 120.0

# libjxl's C API (jxl/encode.h, jxl/decode.h): statuses and the decoder events used here.
ENC_SUCCESS, ENC_ERROR, ENC_NEED_MORE_OUTPUT = 0, 1, 2
DEC_SUCCESS, DEC_ERROR, DEC_NEED_MORE_INPUT, DEC_JPEG_NEED_MORE_OUTPUT = 0, 1, 2, 6
DEC_FULL_IMAGE, DEC_JPEG_RECONSTRUCTION = 0x1000, 0x2000


class StillsError(Exception):
    """A still could not be written, recompressed or verified; the message says why."""


# -- what this host can do ---------------------------------------------------------------------------------------


def _library() -> ctypes.CDLL | None:
    for name in (ctypes.util.find_library("jxl"), "libjxl.so.0.11", "libjxl.so.0.10", "libjxl.so.0.9", "libjxl.so.0.8"):
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        if hasattr(lib, "JxlEncoderAddJPEGFrame") and hasattr(lib, "JxlDecoderSetJPEGBuffer"):
            return lib
    return None


def _ffmpeg_encoders(ffmpeg: str) -> str:
    try:
        run = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return run.stdout


@dataclass(frozen=True)
class Capabilities:
    avif: bool  # ffmpeg with an AV1 encoder and the AVIF muxer
    avif_encoder: str
    jxl_lossless_jpeg: bool  # libjxl present: reversible JPEG recompression
    reasons: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "avif": self.avif,
            "avif_encoder": self.avif_encoder,
            "jxl_lossless_jpeg": self.jxl_lossless_jpeg,
            "jxl_backend": backend(),
            "reasons": dict(self.reasons),
        }


def capabilities(ffmpeg: str | None = None) -> Capabilities:
    reasons: dict[str, str] = {}
    ffmpeg = ffmpeg or shutil.which("ffmpeg") or ""
    encoder = ""
    if not ffmpeg:
        reasons["avif"] = "ffmpeg is not installed"
    else:
        listing = _ffmpeg_encoders(ffmpeg)
        encoder = next((e for e in ("libsvtav1", "libaom-av1", "librav1e") if e in listing), "")
        if not encoder:
            reasons["avif"] = "this ffmpeg has no AV1 encoder (libaom-av1, libsvtav1 or librav1e)"
    if not backend():
        reasons["jxl_lossless_jpeg"] = (
            "no libjxl: install the system package (libjxl0.11) or the Python extra, pip install 'streamstack[stills]'"
        )
    return Capabilities(bool(encoder), encoder, "jxl_lossless_jpeg" not in reasons, reasons)


# -- AVIF --------------------------------------------------------------------------------------------------------


def snapshot(source: Path, target: Path, quality: int = 28, ffmpeg: str | None = None) -> Path:
    """Write `source` (JPEG, PNG, ...) as an AVIF still. `quality` is the AV1 CRF: lower is better (0-63)."""
    if not 0 <= quality <= 63:
        raise StillsError("quality is the AV1 CRF, from 0 (best) to 63.")
    ffmpeg = ffmpeg or shutil.which("ffmpeg") or ""
    if not ffmpeg:
        raise StillsError("ffmpeg is not installed, so AVIF cannot be written.")
    caps = capabilities(ffmpeg)
    if not caps.avif:
        raise StillsError(f"AVIF cannot be written here: {caps.reasons['avif']}.")
    if not source.is_file():
        raise StillsError(f"{source} is not a file.")
    options = {
        "libsvtav1": ["-c:v", "libsvtav1", "-crf", str(quality), "-preset", "4"],
        "libaom-av1": ["-c:v", "libaom-av1", "-crf", str(quality), "-cpu-used", "6", "-still-picture", "1"],
        "librav1e": ["-c:v", "librav1e", "-qp", str(quality * 4)],
    }[caps.avif_encoder]
    with _partial(target) as partial:
        argv = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source), "-frames:v", "1"]
        argv += [*options, "-pix_fmt", "yuv420p", "-f", "avif", str(partial)]
        _run(argv, f"writing {target.name}")
        if not partial.stat().st_size:
            raise StillsError(f"ffmpeg wrote an empty {target.name}.")
    return target


# -- JPEG <-> JPEG XL, reversible ------------------------------------------------------------------------------


def _need_library() -> ctypes.CDLL:
    lib = _library()
    if lib is None:
        raise StillsError("libjxl is not installed (package libjxl0.11 or libjxl), so JPEG XL cannot be used.")
    return lib


def _imagecodecs() -> Any | None:
    """The `imagecodecs` wheel (optional extra `stills`) bundles its own libjxl; preferred when installed."""
    try:
        import imagecodecs
    except ImportError:
        return None
    return imagecodecs if getattr(getattr(imagecodecs, "JPEGXL", None), "available", False) else None


def backend() -> str:
    """Which libjxl the JPEG recompression runs on: "imagecodecs", "libjxl" (the system library) or "" (none)."""
    return "imagecodecs" if _imagecodecs() else "libjxl" if _library() else ""


def jpeg_bytes_to_jxl(data: bytes) -> bytes:
    """Recompress JPEG bytes into a JPEG XL container that can be turned back into exactly these bytes."""
    if data[:2] != b"\xff\xd8":
        raise StillsError("the input is not a JPEG (it does not start with an SOI marker).")
    codecs = _imagecodecs()
    if codecs is not None:
        try:
            return bytes(codecs.jpegxl_encode_jpeg(data))
        except Exception as exc:
            raise StillsError(f"libjxl could not read this JPEG ({exc}).") from None
    return _libjxl_encode(data)


def jxl_bytes_to_jpeg(data: bytes) -> bytes:
    """Rebuild the original JPEG bytes from a JPEG XL file made by `jpeg_bytes_to_jxl` (or `cjxl` from a JPEG)."""
    codecs = _imagecodecs()
    if codecs is not None:
        try:
            return bytes(codecs.jpegxl_decode_jpeg(data))
        except Exception as exc:
            if "NEED_MORE_INPUT" in str(exc):
                raise StillsError("the JPEG XL file is truncated.") from None
            raise StillsError(f"this JPEG XL file is damaged or cannot be turned back into a JPEG ({exc}).") from None
    return _libjxl_decode(data)


def _libjxl_encode(data: bytes) -> bytes:
    lib = _need_library()
    lib.JxlEncoderCreate.restype = ctypes.c_void_p
    lib.JxlEncoderFrameSettingsCreate.restype = ctypes.c_void_p
    lib.JxlEncoderFrameSettingsCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.JxlEncoderAddJPEGFrame.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
    lib.JxlEncoderStoreJPEGMetadata.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.JxlEncoderUseContainer.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.JxlEncoderCloseInput.argtypes = [ctypes.c_void_p]
    lib.JxlEncoderDestroy.argtypes = [ctypes.c_void_p]
    lib.JxlEncoderProcessOutput.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    encoder = lib.JxlEncoderCreate(None)
    if not encoder:
        raise StillsError("libjxl could not create an encoder.")
    try:
        if (
            lib.JxlEncoderUseContainer(encoder, 1) != ENC_SUCCESS
            or lib.JxlEncoderStoreJPEGMetadata(encoder, 1) != ENC_SUCCESS
        ):
            raise StillsError("libjxl would not store the JPEG reconstruction data.")
        settings = lib.JxlEncoderFrameSettingsCreate(encoder, None)
        if lib.JxlEncoderAddJPEGFrame(settings, data, len(data)) != ENC_SUCCESS:
            raise StillsError(
                "libjxl could not read this JPEG (progressive and arithmetic-coded files may be refused)."
            )
        lib.JxlEncoderCloseInput(encoder)
        chunks: list[bytes] = []
        while True:
            buffer = (ctypes.c_uint8 * CHUNK)()
            out = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint8))
            avail = ctypes.c_size_t(CHUNK)
            status = lib.JxlEncoderProcessOutput(encoder, ctypes.byref(out), ctypes.byref(avail))
            chunks.append(bytes(buffer[: CHUNK - avail.value]))
            if status == ENC_SUCCESS:
                return b"".join(chunks)
            if status != ENC_NEED_MORE_OUTPUT:
                raise StillsError("libjxl failed while writing the JPEG XL file.")
    finally:
        lib.JxlEncoderDestroy(encoder)


def _libjxl_decode(data: bytes) -> bytes:
    lib = _need_library()
    lib.JxlDecoderCreate.restype = ctypes.c_void_p
    lib.JxlDecoderSubscribeEvents.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.JxlDecoderSetInput.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
    lib.JxlDecoderCloseInput.argtypes = [ctypes.c_void_p]
    lib.JxlDecoderProcessInput.argtypes = [ctypes.c_void_p]
    lib.JxlDecoderSetJPEGBuffer.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    lib.JxlDecoderReleaseJPEGBuffer.argtypes = [ctypes.c_void_p]
    lib.JxlDecoderReleaseJPEGBuffer.restype = ctypes.c_size_t
    lib.JxlDecoderDestroy.argtypes = [ctypes.c_void_p]
    decoder = lib.JxlDecoderCreate(None)
    if not decoder:
        raise StillsError("libjxl could not create a decoder.")
    try:
        if lib.JxlDecoderSubscribeEvents(decoder, DEC_JPEG_RECONSTRUCTION | DEC_FULL_IMAGE) != DEC_SUCCESS:
            raise StillsError("libjxl would not subscribe to the JPEG reconstruction events.")
        if lib.JxlDecoderSetInput(decoder, data, len(data)) != DEC_SUCCESS:
            raise StillsError("libjxl would not take the input.")
        lib.JxlDecoderCloseInput(decoder)
        size = CHUNK
        buffer = (ctypes.c_uint8 * size)()
        reconstructing = False
        while True:
            status = lib.JxlDecoderProcessInput(decoder)
            if status == DEC_JPEG_RECONSTRUCTION:
                reconstructing = True
                lib.JxlDecoderSetJPEGBuffer(decoder, buffer, size)
            elif status == DEC_JPEG_NEED_MORE_OUTPUT:
                # libjxl writes whole sections, so the room left must grow, not just be handed over again.
                used = size - lib.JxlDecoderReleaseJPEGBuffer(decoder)
                size *= 2
                grown = (ctypes.c_uint8 * size)()
                ctypes.memmove(grown, buffer, used)
                buffer = grown
                lib.JxlDecoderSetJPEGBuffer(decoder, ctypes.byref(buffer, used), size - used)
            elif status == DEC_FULL_IMAGE:
                continue
            elif status == DEC_SUCCESS:
                if not reconstructing:
                    raise StillsError(
                        "this JPEG XL file holds no JPEG reconstruction data (it was not made from a JPEG)."
                    )
                return bytes(buffer[: size - lib.JxlDecoderReleaseJPEGBuffer(decoder)])
            elif status == DEC_NEED_MORE_INPUT:
                raise StillsError("the JPEG XL file is truncated.")
            else:
                raise StillsError("libjxl could not decode this file (it is damaged or not JPEG XL).")
    finally:
        lib.JxlDecoderDestroy(decoder)


@dataclass(frozen=True)
class Archived:
    source: Path
    target: Path
    original_bytes: int
    archive_bytes: int
    sha256: str  # of the original JPEG, which the archive reproduces exactly
    removed_original: bool

    @property
    def saved_percent(self) -> float:
        return round(100.0 * (1 - self.archive_bytes / self.original_bytes), 1) if self.original_bytes else 0.0


def archive(source: Path, target: Path | None = None, remove_original: bool = False) -> Archived:
    """Recompress one JPEG to `<name>.jxl`, prove the original can be rebuilt byte for byte, then keep it.

    The archive file is written to a temporary name, read back, rebuilt and compared before it takes its final
    name; on any mismatch it is deleted and `StillsError` is raised. The JPEG is removed only when asked and only
    after that proof.
    """
    if source.suffix.lower() not in JPEG_SUFFIXES:
        raise StillsError(f"{source.name} is not a JPEG file name (.jpg or .jpeg).")
    try:
        original = source.read_bytes()
    except OSError as exc:
        raise StillsError(f"cannot read {source}: {exc.strerror or exc}.") from None
    target = target or source.with_suffix(".jxl")
    if target.exists():
        raise StillsError(f"{target} already exists; it is not overwritten.")
    packed = jpeg_bytes_to_jxl(original)
    with _partial(target) as partial:
        partial.write_bytes(packed)
        rebuilt = jxl_bytes_to_jpeg(partial.read_bytes())
        if rebuilt != original:
            raise StillsError(
                f"{source.name}: the rebuilt JPEG differs from the original, so the archive was discarded."
            )
    removed = False
    if remove_original:
        try:
            source.unlink()
            removed = True
        except OSError as exc:
            raise StillsError(
                f"{target.name} is verified, but {source.name} could not be removed: {exc.strerror or exc}."
            ) from None
    return Archived(source, target, len(original), len(packed), hashlib.sha256(original).hexdigest(), removed)


def restore(source: Path, target: Path | None = None) -> Path:
    """Write the original JPEG back from a `.jxl` made by `archive`."""
    target = target or source.with_suffix(".jpg")
    if target.exists():
        raise StillsError(f"{target} already exists; it is not overwritten.")
    try:
        data = jxl_bytes_to_jpeg(source.read_bytes())
    except OSError as exc:
        raise StillsError(f"cannot read {source}: {exc.strerror or exc}.") from None
    with _partial(target) as partial:
        partial.write_bytes(data)
    return target


# -- helpers -----------------------------------------------------------------------------------------------------


@contextlib.contextmanager
def _partial(target: Path) -> Iterator[Path]:
    """A temporary name beside `target`: renamed into place on success, deleted on any error."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".part", dir=target.parent)
    except OSError as exc:
        raise StillsError(f"cannot write in {target.parent}: {exc.strerror or exc}.") from None
    os.close(handle)
    path = Path(name)
    try:
        yield path
        os.replace(path, target)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _run(argv: list[str], what: str) -> None:
    try:
        run = subprocess.run(argv, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        raise StillsError(f"ffmpeg timed out {what}.") from None
    except OSError as exc:
        raise StillsError(f"ffmpeg could not start {what}: {exc.strerror or exc}.") from None
    if run.returncode:
        detail = (run.stderr.strip().splitlines() or ["no output"])[-1][:200]
        raise StillsError(f"ffmpeg failed {what}: {detail}")
