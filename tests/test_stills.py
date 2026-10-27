"""Stills: the byte-exact JPEG <-> JPEG XL round trip, the verified archive, AVIF snapshots, and the CLI."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from streamstack import cli, stills

FFMPEG = shutil.which("ffmpeg")
CAPS = stills.capabilities()
needs_jxl = pytest.mark.skipif(not CAPS.jxl_lossless_jpeg, reason="libjxl is needed")
needs_avif = pytest.mark.skipif(not CAPS.avif, reason="ffmpeg with an AV1 encoder is needed")


FIXTURE = Path(__file__).parent / "fixtures" / "camera-frame.jpg"  # a 640x480 baseline JPEG, 25 kB


@pytest.fixture
def jpeg(tmp_path: Path) -> Path:
    target = tmp_path / "frame.jpg"
    shutil.copy(FIXTURE, target)
    return target


@pytest.fixture(params=["imagecodecs", "libjxl"])
def engine(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run a test once on each libjxl the code can use; skipped where that one is not installed."""
    if request.param == "imagecodecs":
        if stills._imagecodecs() is None:
            pytest.skip("the imagecodecs extra is not installed")
    else:
        if stills._library() is None:
            pytest.skip("the system libjxl is not installed")
        monkeypatch.setattr(stills, "_imagecodecs", lambda: None)
    return str(request.param)


@needs_jxl
def test_round_trip_restores_the_original_jpeg_bytes(jpeg: Path, engine: str) -> None:
    original = jpeg.read_bytes()
    packed = stills.jpeg_bytes_to_jxl(original)
    assert packed != original and len(packed) < len(original)
    assert stills.jxl_bytes_to_jpeg(packed) == original  # lossless: the very same file, not the same picture


def noisy_jpeg() -> bytes:
    """A JPEG of about 3 MB: random noise does not compress, so the decoder's first output room is too small."""
    try:
        import imagecodecs
        import numpy
    except ImportError:
        pytest.skip("numpy and imagecodecs are needed to make a large JPEG")
    pixels = numpy.random.default_rng(7).integers(0, 256, (1600, 2400, 3), dtype=numpy.uint8)
    return bytes(imagecodecs.jpeg8_encode(pixels, level=90))


@needs_jxl
def test_round_trip_holds_for_a_multi_megabyte_noisy_jpeg(engine: str) -> None:
    original = noisy_jpeg()
    assert len(original) > 2_000_000
    assert stills.jxl_bytes_to_jpeg(stills.jpeg_bytes_to_jxl(original)) == original


@needs_jxl
def test_archive_verifies_then_keeps_the_original_unless_asked(jpeg: Path, engine: str) -> None:
    digest = hashlib.sha256(jpeg.read_bytes()).hexdigest()
    done = stills.archive(jpeg)
    assert done.target == jpeg.with_suffix(".jxl") and done.target.is_file() and jpeg.is_file()
    assert done.sha256 == digest and done.saved_percent > 5 and not done.removed_original
    restored = stills.restore(done.target, jpeg.with_name("again.jpg"))
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == digest
    assert not list(jpeg.parent.glob(".*.part"))


@needs_jxl
def test_remove_original_only_after_proof(jpeg: Path) -> None:
    done = stills.archive(jpeg, remove_original=True)
    assert done.removed_original and not jpeg.exists()
    assert stills.restore(done.target).read_bytes()[:2] == b"\xff\xd8"


@needs_jxl
def test_a_mismatching_rebuild_discards_the_archive_and_keeps_the_jpeg(
    jpeg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stills, "jxl_bytes_to_jpeg", lambda data: b"\xff\xd8 not the same")
    with pytest.raises(stills.StillsError, match="differs from the original"):
        stills.archive(jpeg, remove_original=True)
    assert jpeg.is_file() and not jpeg.with_suffix(".jxl").exists() and not list(jpeg.parent.glob(".*.part"))


@needs_jxl
def test_nothing_is_overwritten(jpeg: Path) -> None:
    jpeg.with_suffix(".jxl").write_bytes(b"keep me")
    with pytest.raises(stills.StillsError, match="already exists"):
        stills.archive(jpeg)
    assert jpeg.with_suffix(".jxl").read_bytes() == b"keep me"


@needs_jxl
@pytest.mark.parametrize("data", [b"", b"not a jpeg", b"\xff\xd8\xff\xe0 truncated"])
def test_bad_jpegs_are_refused_with_a_reason(data: bytes) -> None:
    with pytest.raises(stills.StillsError):
        stills.jpeg_bytes_to_jxl(data)


@needs_jxl
def test_garbage_and_truncated_jxl_are_refused(jpeg: Path) -> None:
    packed = stills.jpeg_bytes_to_jxl(jpeg.read_bytes())
    with pytest.raises(stills.StillsError, match=r"truncated|damaged"):
        stills.jxl_bytes_to_jpeg(packed[: len(packed) // 2])
    with pytest.raises(stills.StillsError):
        stills.jxl_bytes_to_jpeg(b"definitely not jpeg xl")


def test_a_non_jpeg_name_is_refused(tmp_path: Path) -> None:
    (tmp_path / "x.png").write_bytes(b"\x89PNG")
    with pytest.raises(stills.StillsError, match="not a JPEG file name"):
        stills.archive(tmp_path / "x.png")


def test_a_missing_libjxl_says_how_to_get_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stills, "_library", lambda: None)
    monkeypatch.setattr(stills, "_imagecodecs", lambda: None)
    with pytest.raises(stills.StillsError, match="libjxl is not installed"):
        stills.jpeg_bytes_to_jxl(b"\xff\xd8")
    assert not stills.capabilities().jxl_lossless_jpeg


@needs_avif
def test_snapshot_writes_a_real_avif(jpeg: Path, tmp_path: Path) -> None:
    target = stills.snapshot(jpeg, tmp_path / "out" / "frame.avif", quality=35)
    data = target.read_bytes()
    assert data[4:8] == b"ftyp" and b"avif" in data[:32]  # an ISO BMFF file branded avif
    assert len(data) < jpeg.stat().st_size
    assert not list(target.parent.glob(".*.part"))


@needs_avif
def test_snapshot_failure_leaves_nothing_behind(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    with pytest.raises(stills.StillsError, match="ffmpeg failed"):
        stills.snapshot(bad, tmp_path / "bad.avif")
    assert list(tmp_path.glob("*.avif")) == [] and not list(tmp_path.glob(".*.part"))


def test_snapshot_validates_its_input(tmp_path: Path) -> None:
    with pytest.raises(stills.StillsError, match="CRF"):
        stills.snapshot(tmp_path / "x.jpg", tmp_path / "x.avif", quality=99)


def test_cli_status_archive_and_restore(jpeg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    if not CAPS.jxl_lossless_jpeg:
        pytest.skip("libjxl is needed")
    assert cli.main(["stills", "status", "--json"]) == 0
    assert '"jxl_lossless_jpeg": true' in capsys.readouterr().out
    assert cli.main(["stills", "archive", str(jpeg)]) == 0
    assert "rebuilt byte for byte" in capsys.readouterr().out
    jpeg.unlink()
    assert cli.main(["stills", "restore", str(jpeg.with_suffix(".jxl"))]) == 0
    assert jpeg.is_file()


def test_cli_reports_an_error_with_a_nonzero_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "a.png").write_bytes(b"x")
    assert cli.main(["stills", "archive", str(tmp_path / "a.png")]) == 1
    assert "not a JPEG file name" in capsys.readouterr().err
