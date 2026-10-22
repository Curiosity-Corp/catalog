"""The face model: pinned, fetched over HTTPS, accepted only when the checksum matches."""

from __future__ import annotations

import hashlib
import io
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest

from streamstack.framing import model
from streamstack.framing.model import ModelError

PAYLOAD = b"not really an onnx file" * 100
SPEC = replace(
    model.YUNET,
    sha256=hashlib.sha256(PAYLOAD).hexdigest(),
    size=len(PAYLOAD),
    url="https://example.invalid/model.onnx",
)


class Response(io.BytesIO):
    def __enter__(self) -> Response:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def opener(payload: bytes = PAYLOAD, calls: list | None = None):
    def open_url(url: str, timeout: float) -> Response:
        if calls is not None:
            calls.append((url, timeout))
        return Response(payload)

    return open_url


def test_the_pinned_model_is_a_real_pin() -> None:
    spec = model.YUNET
    assert spec.url.startswith("https://github.com/opencv/opencv_zoo/raw/")
    assert "/main/" not in spec.url  # a commit, never a moving branch
    assert len(spec.sha256) == 64 and int(spec.sha256, 16)
    assert spec.license.startswith("MIT")
    assert model.model_path().parent == model.models_dir()


def test_models_live_under_xdg_data_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert model.models_dir() == tmp_path / "data/streamstack/models"


def test_fetch_installs_a_verified_model_and_reuses_it() -> None:
    calls: list = []
    path = model.fetch(SPEC, opener=opener(calls=calls))
    assert path.read_bytes() == PAYLOAD
    assert calls == [(SPEC.url, model.FETCH_TIMEOUT)]
    assert model.status(SPEC)[0] == "ok"
    model.fetch(SPEC, opener=opener(calls=calls))
    assert len(calls) == 1  # already verified: no second download
    model.fetch(SPEC, opener=opener(calls=calls), force=True)
    assert len(calls) == 2
    assert not list(path.parent.glob(".fetch-*"))  # no temp files left behind


def test_a_download_with_the_wrong_checksum_is_never_installed() -> None:
    tampered = b"x" * len(PAYLOAD)
    with pytest.raises(ModelError, match="does not match the pinned model"):
        model.fetch(SPEC, opener=opener(tampered))
    assert not model.model_path(SPEC).exists()
    assert not list(model.models_dir().glob(".fetch-*"))


def test_a_truncated_download_is_refused() -> None:
    with pytest.raises(ModelError, match="does not match"):
        model.fetch(SPEC, opener=opener(PAYLOAD[:-5]))
    assert not model.model_path(SPEC).exists()


def test_an_endless_download_is_cut_off() -> None:
    with pytest.raises(ModelError, match="far larger"):
        model.fetch(SPEC, opener=opener(b"y" * (len(PAYLOAD) * 3)))
    assert not model.model_path(SPEC).exists()


def test_network_errors_become_model_errors() -> None:
    def broken(url: str, timeout: float) -> Response:
        raise urllib.error.URLError("no route to host")

    with pytest.raises(ModelError, match="could not download the face model: no route to host"):
        model.fetch(SPEC, opener=broken)


def test_plain_http_is_refused() -> None:
    with pytest.raises(ModelError, match="non-HTTPS"):
        model.fetch(replace(SPEC, url="http://example.invalid/m.onnx"), opener=opener())


def test_verify_reports_missing_wrong_size_and_corrupt_files() -> None:
    assert model.status(SPEC)[0] == "missing"
    with pytest.raises(ModelError, match="streamstack framing --fetch"):
        model.verify(spec=SPEC)
    target = model.model_path(SPEC)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"short")
    with pytest.raises(ModelError, match="5 bytes"):
        model.verify(spec=SPEC)
    target.write_bytes(b"z" * len(PAYLOAD))
    state, detail = model.status(SPEC)
    assert state == "corrupt" and "checksum" in detail
    target.write_bytes(PAYLOAD)
    assert model.verify(spec=SPEC) == target


def test_a_file_changed_after_verification_is_caught() -> None:
    path = model.fetch(SPEC, opener=opener())
    assert model.verify(path, SPEC) == path
    path.write_bytes(b"q" * len(PAYLOAD))  # same size, new content (mtime moves)
    with pytest.raises(ModelError, match="checksum"):
        model.verify(path, SPEC)
