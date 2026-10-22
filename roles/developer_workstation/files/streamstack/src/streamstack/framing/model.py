"""The face-detection model: where it comes from, where it lives, and proof it is the file we expect.

StreamStack does not ship the model. `streamstack framing --fetch` downloads it once from a pinned commit of
OpenCV Zoo and refuses any file whose SHA-256 differs from the one recorded here, so a changed upstream file, a
truncated download or a swapped mirror cannot reach the detector.

YuNet is a 75k-parameter face detector (232 KB). The network is by Wei Wu and Shiqi Yu
(github.com/ShiqiYu/libfacedetection.train, MIT licence); the ONNX export is the one OpenCV Zoo publishes
(github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet, MIT licence, copyright 2020 Shiqi Yu).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from streamstack import config

FETCH_TIMEOUT = 30.0
CHUNK = 64 * 1024


class ModelError(Exception):
    """The model is missing, corrupt or could not be fetched (the message says why and what to do)."""


@dataclass(frozen=True)
class ModelSpec:
    key: str
    name: str
    filename: str
    url: str
    sha256: str
    size: int
    license: str
    source: str


YUNET = ModelSpec(
    key="yunet-2023mar",
    name="YuNet 2023mar",
    filename="face_detection_yunet_2023mar.onnx",
    # Pinned to the OpenCV Zoo commit that last touched the file ("update yunet to v2", #151).
    url=(
        "https://github.com/opencv/opencv_zoo/raw/f12e12798e8314f7c074a6656816c048dcc95b7a/"
        "models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
    ),
    sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    size=232589,
    license="MIT (Copyright 2020 Shiqi Yu)",
    source="https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet",
)


def models_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return base / "streamstack" / "models"


def model_path(spec: ModelSpec = YUNET) -> Path:
    return models_dir() / spec.filename


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelError(f"cannot read {path}: {exc.strerror or exc}") from exc
    return digest.hexdigest()


# A verified file is not hashed again until it changes (the Console asks for health every few seconds).
_verified: dict[Path, tuple[int, int, str]] = {}


def verify(path: Path | None = None, spec: ModelSpec = YUNET) -> Path:
    """The model path once its checksum matches; raises ModelError otherwise."""
    path = path or model_path(spec)
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise ModelError(f"the face model is not installed ({path}); run: streamstack framing --fetch") from exc
    except OSError as exc:
        raise ModelError(f"cannot read the face model {path}: {exc.strerror or exc}") from exc
    stamp = (info.st_size, info.st_mtime_ns, spec.sha256)
    if _verified.get(path) == stamp:
        return path
    if info.st_size != spec.size:
        raise ModelError(
            f"the face model {path} is {info.st_size} bytes, expected {spec.size}; "
            "delete it and run: streamstack framing --fetch"
        )
    found = sha256_file(path)
    if found != spec.sha256:
        raise ModelError(
            f"the face model {path} fails its checksum (sha256 {found[:12]}..., expected {spec.sha256[:12]}...); "
            "delete it and run: streamstack framing --fetch"
        )
    _verified[path] = stamp
    return path


def status(spec: ModelSpec = YUNET) -> tuple[str, str]:
    """("ok" | "missing" | "corrupt", detail) without raising."""
    try:
        verify(spec=spec)
    except ModelError as exc:
        missing = not model_path(spec).exists()
        return ("missing" if missing else "corrupt"), str(exc)
    return "ok", f"{spec.name} at {model_path(spec)} (sha256 verified)"


Opener = Callable[..., IO[bytes]]


def fetch(spec: ModelSpec = YUNET, *, opener: Opener | None = None, force: bool = False) -> Path:
    """Download the model next to where it will be used and install it only if the checksum matches."""
    target = model_path(spec)
    if not force:
        try:
            return verify(target, spec)
        except ModelError:
            pass  # missing or wrong: fetch it
    if not spec.url.startswith("https://"):
        raise ModelError(f"refusing to fetch the face model over a non-HTTPS URL: {spec.url}")
    open_url = opener or urllib.request.urlopen
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ModelError(f"cannot create {target.parent}: {exc.strerror or exc}") from exc
    digest = hashlib.sha256()
    received = 0
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".fetch-", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, open_url(spec.url, timeout=FETCH_TIMEOUT) as response:
            while chunk := response.read(CHUNK):
                received += len(chunk)
                if received > spec.size * 2:
                    raise ModelError(f"{spec.url} is far larger than the expected {spec.size} bytes; not saving it")
                digest.update(chunk)
                out.write(chunk)
        if received != spec.size or digest.hexdigest() != spec.sha256:
            raise ModelError(
                f"the download from {spec.url} does not match the pinned model "
                f"({received} bytes, sha256 {digest.hexdigest()[:12]}...); nothing was installed"
            )
        tmp.chmod(0o644)
        os.replace(tmp, target)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", None) or getattr(exc, "strerror", None) or exc
        raise ModelError(f"could not download the face model: {reason}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    _verified.pop(target, None)
    return verify(target, spec)


def cache_dir() -> Path:
    """Compiled-model cache for OpenVINO (GPU kernels take a while to build the first time)."""
    return config.cache_dir() / "openvino"
