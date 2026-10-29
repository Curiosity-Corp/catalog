"""Which video codecs a Twitch account can publish (Enhanced Broadcasting), asked of Twitch itself.

Twitch tells a broadcaster's software what its account may send through the `GetClientConfiguration` call that OBS
makes before an Enhanced Broadcasting stream: the answer lists the encoder configurations the account is enrolled
for. StreamStack asks the same question once per stream start, with the stream key as the credential, and offers only
those codecs, so an account that is not enrolled streams H.264 straight away instead of failing to connect with AV1
and falling back after a retry.

The key is sent only in the request body to Twitch over https, is never written to a log or an event, and is not
kept: the cache is keyed by a hash of it. Any failure (offline, an answer in a shape this code does not know, a
refused key) means "not known": the destination then behaves exactly as before and the existing fallback applies.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

CONFIG_URL = "https://ingest.contribute.live-video.net/api/v3/GetClientConfiguration"
SCHEMA_VERSION = "2024-06-04"
TIMEOUT_S = 5.0
HIT_TTL_S = 600.0
MISS_TTL_S = 60.0
CLIENT_CODECS = ("h264", "h265", "av1")
KNOWN_HOSTS = ("twitch.tv", "live-video.net", "justin.tv")

Poster = Callable[[str, dict[str, object], float], dict[str, object]]


@dataclass(frozen=True)
class TwitchCaps:
    """What the probe found: `known` is False when Twitch could not be asked or its answer was not understood."""

    known: bool
    codecs: tuple[str, ...] = ()
    max_bitrate_kbps: int | None = None
    reason: str = ""
    configurations: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        return {
            "known": self.known,
            "codecs": list(self.codecs),
            "max_bitrate_kbps": self.max_bitrate_kbps,
            "reason": self.reason,
        }


def is_twitch(url: str) -> bool:
    """True for a Twitch ingest address (live.twitch.tv, a regional `*.contribute.twitch.tv`, `*.live-video.net`)."""
    host = (urlsplit(url).hostname or "").lower()
    return any(host == known or host.endswith("." + known) for known in KNOWN_HOSTS)


def _codec_of(kind: str) -> str | None:
    name = kind.lower()
    if "av1" in name:
        return "av1"
    if "hevc" in name or "h265" in name or "h.265" in name:
        return "hevc"
    if name:
        return "h264"  # obs_x264, jim_nvenc, amd_amf_h264, ...: the plain encoders are all H.264
    return None


def parse_configuration(answer: Mapping[str, object]) -> TwitchCaps:
    """Read a GetClientConfiguration answer. Unknown shapes are "not known", never guessed."""
    status = answer.get("status")
    if isinstance(status, Mapping) and status.get("result") == "error":
        return TwitchCaps(False, reason="Twitch refused the account: " + _plain(status.get("html_en_us")))
    configs = answer.get("encoder_configurations")
    if not isinstance(configs, list) or not configs:
        return TwitchCaps(False, reason="Twitch's answer lists no encoder configurations.")
    codecs: list[str] = []
    labels: list[str] = []
    top: int | None = None
    for config in configs:
        if not isinstance(config, Mapping):
            continue
        kind = str(config.get("type", ""))
        codec = _codec_of(kind)
        if codec is not None and codec not in codecs:
            codecs.append(codec)
        labels.append(kind)
        settings = config.get("settings")
        rate = settings.get("bitrate") if isinstance(settings, Mapping) else None
        if isinstance(rate, int) and not isinstance(rate, bool) and rate > 0:
            top = max(top or 0, rate if rate < 100_000 else rate // 1000)  # kbps, or bps in some answers
    if not codecs:
        return TwitchCaps(False, reason="Twitch's answer names no usable encoder.")
    order = {"av1": 0, "hevc": 1, "h264": 2}
    return TwitchCaps(True, tuple(sorted(codecs, key=order.__getitem__)), top, "", tuple(labels))


def _plain(text: object) -> str:
    import re

    return re.sub(r"<[^>]+>", "", str(text or "no reason given")).strip()[:200]


def post_json(url: str, body: dict[str, object], timeout: float) -> dict[str, object]:
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", "User-Agent": "StreamStack"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        parsed = json.loads(response.read(1_000_000))
    if not isinstance(parsed, dict):
        raise ValueError("the answer is not a JSON object")
    return parsed


def request_body(key: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "service": "IVS",
        "authentication": key,
        "client": {"name": "streamstack", "version": "1", "supported_codecs": list(CLIENT_CODECS)},
        "capabilities": {},
        "preferences": {"vod_track_audio": False, "maximum_video_tracks": 1},
    }


class AccountProbe:
    """Asks Twitch what an account may publish, and remembers the answer briefly (per key, by hash)."""

    def __init__(
        self,
        url: str = CONFIG_URL,
        post: Poster = post_json,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if urlsplit(url).scheme not in ("https", "http"):
            raise ValueError("the Twitch configuration address must be http or https")
        self._url, self._post, self._clock = url, post, clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, TwitchCaps]] = {}

    def lookup(self, key: str) -> TwitchCaps:
        if not key:
            return TwitchCaps(False, reason="No stream key is set, so the account cannot be asked.")
        token = hashlib.sha256(key.encode()).hexdigest()
        now = self._clock()
        with self._lock:
            hit = self._cache.get(token)
            if hit is not None and hit[0] > now:
                return hit[1]
        caps = self._ask(key)
        with self._lock:
            self._cache[token] = (now + (HIT_TTL_S if caps.known else MISS_TTL_S), caps)
        return caps

    def _ask(self, key: str) -> TwitchCaps:
        try:
            answer = self._post(self._url, request_body(key), TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                body = json.loads(exc.read(100_000))
                status = body.get("status") if isinstance(body, dict) else None
                detail = _plain(status.get("html_en_us")) if isinstance(status, dict) else ""
            except (ValueError, OSError):
                detail = ""
            return TwitchCaps(False, reason=f"Twitch answered HTTP {exc.code}" + (f": {detail}" if detail else "."))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return TwitchCaps(False, reason=f"Twitch could not be asked ({getattr(exc, 'reason', exc)}).")
        except ValueError as exc:
            return TwitchCaps(False, reason=f"Twitch's answer was not understood ({exc}).")
        return parse_configuration(answer)
