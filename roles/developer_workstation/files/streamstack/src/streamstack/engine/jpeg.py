"""Cut a concatenated MJPEG byte stream into single JPEG images (SOI ff d8 ... EOI ff d9)."""

from __future__ import annotations


class JpegSplitter:
    MAX_BUFFER = 8 * 1024 * 1024

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buffer += chunk
        frames: list[bytes] = []
        while True:
            start = self.buffer.find(b"\xff\xd8\xff")
            if start < 0:
                del self.buffer[: max(0, len(self.buffer) - 2)]  # keep a marker split across chunks
                break
            end = self.buffer.find(b"\xff\xd9", start + 3)
            if end < 0:
                del self.buffer[:start]
                if len(self.buffer) > self.MAX_BUFFER:
                    self.buffer.clear()  # a runaway stream without an end marker
                break
            frames.append(bytes(self.buffer[start : end + 2]))
            del self.buffer[: end + 2]
        return frames
