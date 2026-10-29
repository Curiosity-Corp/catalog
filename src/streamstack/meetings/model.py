"""The meeting record every calendar source produces, and the join-link finder."""

from __future__ import annotations

import re
from dataclasses import dataclass

JOIN_LINK = re.compile(
    r"https?://(?:[\w-]+\.)*(?:teams\.microsoft\.com|teams\.live\.com|zoom\.us|zoomgov\.com|meet\.google\.com|"
    r"webex\.com|whereby\.com|gotomeeting\.com|bluejeans\.com|meet\.jit\.si)/[^\s<>\"')\\]*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Meeting:
    """One timed meeting. `start` and `end` are Unix seconds; `join` is a video-call link or ""."""

    id: str
    title: str
    start: float
    end: float
    join: str = ""
    source: str = ""

    @property
    def online(self) -> bool:
        return bool(self.join)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "start": self.start,
            "end": self.end,
            "online": self.online,
            "join": self.join,
            "source": self.source,
        }


def find_join_link(*texts: str) -> str:
    """The first video-call link in any of the texts, or ''."""
    for text in texts:
        found = JOIN_LINK.search(text or "")
        if found:
            return found.group(0).rstrip(".,;")
    return ""
