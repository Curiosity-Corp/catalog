"""Just enough JPEG: cut a concatenated MJPEG stream into frames and read a frame's size and subsampling."""

from __future__ import annotations

from dataclasses import dataclass

SOI = b"\xff\xd8\xff"
EOI = b"\xff\xd9"
# Start-of-frame markers (baseline, extended, progressive, lossless, arithmetic); not DHT (c4), JPG (c8), DAC (cc).
SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
SUBSAMPLING = {0x11: "4:4:4", 0x21: "4:2:2", 0x22: "4:2:0", 0x41: "4:1:1", 0x12: "4:4:0"}


@dataclass(frozen=True)
class JpegInfo:
    width: int
    height: int
    components: int
    subsampling: str


class JpegSplitter:
    """Cut a concatenated MJPEG byte stream into single JPEG images (SOI ff d8 ff ... EOI ff d9)."""

    MAX_BUFFER = 8 * 1024 * 1024

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buffer += chunk
        frames: list[bytes] = []
        while True:
            start = self.buffer.find(SOI)
            if start < 0:
                del self.buffer[: max(0, len(self.buffer) - 2)]  # keep a marker split across two reads
                break
            end = self.buffer.find(EOI, start + 3)
            if end < 0:
                del self.buffer[:start]
                if len(self.buffer) > self.MAX_BUFFER:
                    self.buffer.clear()  # a runaway stream without an end marker
                break
            frames.append(bytes(self.buffer[start : end + 2]))
            del self.buffer[: end + 2]
        return frames


def jpeg_info(data: bytes | bytearray | memoryview) -> JpegInfo | None:
    """Size and chroma subsampling from the first start-of-frame segment; None when the data is not a JPEG."""
    view = bytes(data[:65536])  # headers sit at the front; Canon puts EXIF and a thumbnail before the frame
    if not view.startswith(b"\xff\xd8"):
        return None
    i, n = 2, len(view)
    while i + 4 <= n:
        if view[i] != 0xFF:
            return None
        marker = view[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0x01, *range(0xD0, 0xD9)):  # standalone markers carry no length
            i += 2
            continue
        length = (view[i + 2] << 8) | view[i + 3]
        if marker in SOF_MARKERS:
            if i + 10 > n:
                return None
            height = (view[i + 5] << 8) | view[i + 6]
            width = (view[i + 7] << 8) | view[i + 8]
            components = view[i + 9]
            sampling = view[i + 11] if components and i + 12 <= n else 0
            if not width or not height:
                return None
            return JpegInfo(width, height, components, SUBSAMPLING.get(sampling, "gray" if components == 1 else "?"))
        if marker == 0xDA:  # start of scan before any frame header: malformed
            return None
        i += 2 + length
    return None
