"""WHIP signalling over HTTP (RFC 9725): POST an SDP offer, PATCH trickled candidates, DELETE to end.

Pure standard library (`http.client`), blocking; the session runs it in a worker thread. What this module owns:

- the offer POST with a Bearer token, `201 Created`, the `Location` of the session resource (relative or absolute),
  the `ETag`, the `Link: rel="ice-server"` STUN and TURN servers, and `Accept-Patch`;
- redirects (307 and 308 keep the method and body; the token follows only to the same origin, so a redirect cannot
  carry it to another host);
- the trickle-ICE PATCH with `If-Match` and its error answers (405 and 501: the server cannot take it; 412: the
  ETag moved on; 404 and 410: the session is gone);
- the DELETE that ends the session (an already-gone session is a success);
- every error as a `WhipError` whose message says what to do and never contains the token or the URL's query.
"""

from __future__ import annotations

import http.client
import re
import socket
import ssl
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

USER_AGENT = "StreamStack-WHIP"
SDP = "application/sdp"
SDPFRAG = "application/trickle-ice-sdpfrag"
MAX_BODY = 256 * 1024  # an SDP answer is a few kilobytes; more than this is not one
MAX_REDIRECTS = 3
MAX_RETRY_AFTER = 60.0


class WhipError(Exception):
    """A WHIP exchange failed. `fatal` means retrying cannot help (a wrong address or token); `status` is the HTTP
    status or 0 for a network failure; `retry_after` is the server's `Retry-After` in seconds (0 when none)."""

    def __init__(self, message: str, status: int = 0, fatal: bool = False, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.fatal = fatal
        self.retry_after = retry_after


class TrickleUnsupported(WhipError):
    """The server does not take PATCH (405 or 501): candidates must be in the offer."""


class SessionGone(WhipError):
    """The session resource no longer exists (404 or 410)."""


class PreconditionFailed(WhipError):
    """The ETag in `If-Match` is out of date (412)."""


@dataclass(frozen=True)
class IceServer:
    urls: tuple[str, ...]
    username: str = ""
    credential: str = ""


@dataclass
class Resource:
    """What a successful POST returns: where the session lives and the server's answer."""

    url: str  # absolute URL of the session resource
    answer: str
    etag: str = ""
    ice_servers: list[IceServer] = field(default_factory=list)
    trickle: bool = False  # Accept-Patch lists application/trickle-ice-sdpfrag
    origin_token: bool = True  # the resource is on the endpoint's origin, so the token goes with requests to it


def plain(url: str) -> str:
    """`url` without query, fragment or credentials, safe to show in an error."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    host = f"[{host}]" if ":" in host else host
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


def origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    default = 443 if parts.scheme == "https" else 80
    return parts.scheme, (parts.hostname or "").lower(), parts.port or default


# -- Link headers --------------------------------------------------------------------------------------------------

_TARGET = re.compile(r"<([^>]*)>")
_PARAM = re.compile(r";\s*([A-Za-z0-9_-]+)\s*(?:=\s*(\"[^\"]*\"|[^;,\s]*))?")
ICE_SCHEMES = ("stun", "stuns", "turn", "turns")


def parse_link_headers(values: list[str]) -> list[IceServer]:
    """The `rel="ice-server"` links of a response as ICE servers. Anything else, or malformed, is ignored."""
    servers: list[IceServer] = []
    for value in values:
        targets = list(_TARGET.finditer(value))
        for k, match in enumerate(targets):
            end = targets[k + 1].start() if k + 1 < len(targets) else len(value)
            rest = value[match.end() : end]
            target = match.group(1).strip()
            params = {name.lower(): val.strip('"') for name, val in _PARAM.findall(rest)}
            rels = params.get("rel", "").lower().split()
            scheme = target.split(":", 1)[0].lower()
            if "ice-server" not in rels or scheme not in ICE_SCHEMES:
                continue
            servers.append(IceServer((target,), params.get("username", ""), params.get("credential", "")))
    return servers


# -- trickle SDP fragments (RFC 8840) -------------------------------------------------------------------------------


def build_sdpfrag(ufrag: str, password: str, mid: str, candidates: list[str], end: bool = False) -> str:
    """The body of a trickle-ICE PATCH. `candidates` are the `candidate:` attribute values (without `a=`)."""
    lines = [f"a=ice-ufrag:{ufrag}", f"a=ice-pwd:{password}", "m=audio 9 UDP/TLS/RTP/SAVPF 0", f"a=mid:{mid}"]
    lines += [f"a={c if c.startswith('candidate:') else 'candidate:' + c}" for c in candidates]
    if end:
        lines.append("a=end-of-candidates")
    return "\r\n".join(lines) + "\r\n"


# -- the client ----------------------------------------------------------------------------------------------------


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    links: list[str]
    body: bytes


class WhipClient:
    """One WHIP endpoint. `token` is the Bearer token (empty: none). All methods block and raise `WhipError`."""

    def __init__(
        self,
        endpoint: str,
        token: str = "",
        timeout: float = 10.0,
        context: ssl.SSLContext | None = None,
    ) -> None:
        parts = urlsplit(endpoint)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise WhipError("A WHIP address looks like https://host/path.", fatal=True)
        self.endpoint = endpoint
        self._token = token
        self._timeout = timeout
        self._context = context

    # -- publish ---------------------------------------------------------------------------------------------

    def publish(self, offer: str) -> Resource:
        """POST the offer. Returns the session resource with the server's answer."""
        reply, final = self._exchange("POST", self.endpoint, offer.encode(), SDP)
        if reply.status not in (200, 201):
            raise self._error(reply, "publish", self.endpoint)
        ctype = reply.headers.get("content-type", "").split(";")[0].strip().lower()
        text = reply.body.decode("utf-8", "replace")
        if ctype != SDP and not text.startswith("v=0"):
            raise WhipError(
                f"{plain(final)} answered without an SDP answer (content type {ctype or 'none'}); "
                "this does not look like a WHIP endpoint.",
                reply.status,
                fatal=True,
            )
        location = reply.headers.get("location", "")
        if not location:
            raise WhipError(
                f"{plain(final)} accepted the offer but sent no Location header, so the session cannot be ended "
                "cleanly; this is not a WHIP server (RFC 9725 requires it).",
                reply.status,
                fatal=True,
            )
        url = urljoin(final, location)
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
            raise WhipError("The server's Location is not an http(s) address.", reply.status, fatal=True)
        accept_patch = reply.headers.get("accept-patch", "").lower()
        return Resource(
            url=url,
            answer=text,
            etag=reply.headers.get("etag", ""),
            ice_servers=parse_link_headers(reply.links),
            trickle=SDPFRAG in accept_patch,
            origin_token=origin(url) == origin(self.endpoint),
        )

    # -- trickle ---------------------------------------------------------------------------------------------

    def patch(self, resource: Resource, fragment: str, etag: str | None = None) -> str:
        """PATCH candidates (an SDP fragment) to the session. Returns the new ETag ("" if the server sent none).

        `etag` defaults to the resource's current one; with none known, `If-Match: *` is sent.
        """
        tag = resource.etag if etag is None else etag
        extra = {"If-Match": tag or "*"}
        reply, _ = self._exchange(
            "PATCH", resource.url, fragment.encode(), SDPFRAG, extra, auth=resource.origin_token, follow=False
        )
        if reply.status in (200, 204):
            new = reply.headers.get("etag", "")
            if new:
                resource.etag = new
            return new
        if reply.status in (405, 501):
            raise TrickleUnsupported(
                f"{plain(resource.url)} does not accept trickled candidates (HTTP {reply.status}).", reply.status
            )
        if reply.status == 412:
            raise PreconditionFailed("The server's session changed (HTTP 412); the ETag is out of date.", 412)
        if reply.status in (404, 410):
            raise SessionGone(f"{plain(resource.url)} no longer exists (HTTP {reply.status}).", reply.status)
        raise self._error(reply, "send candidates to", resource.url)

    # -- teardown --------------------------------------------------------------------------------------------

    def delete(self, resource: Resource) -> None:
        """End the session. A session that is already gone counts as ended."""
        reply, _ = self._exchange(
            "DELETE", resource.url, b"", "", auth=resource.origin_token, follow=False, send_body=False
        )
        if reply.status in (200, 202, 204, 404, 410):
            return
        raise self._error(reply, "end the session on", resource.url)

    # -- internals -------------------------------------------------------------------------------------------

    def _exchange(
        self,
        method: str,
        url: str,
        body: bytes,
        content_type: str,
        extra: dict[str, str] | None = None,
        auth: bool = True,
        follow: bool = True,
        send_body: bool = True,
    ) -> tuple[Reply, str]:
        """One request, following 307 and 308 for a POST. Returns the final reply and the URL that answered."""
        hops = 0
        current = url
        while True:
            reply = self._once(method, current, body, content_type, extra or {}, auth, send_body)
            if follow and reply.status in (301, 302, 303, 307, 308):
                target = reply.headers.get("location", "")
                if not target:
                    raise WhipError(f"{plain(current)} redirected without a Location.", reply.status)
                hops += 1
                if hops > MAX_REDIRECTS:
                    raise WhipError(f"Too many redirects from {plain(url)}.", reply.status, fatal=True)
                nxt = urljoin(current, target)
                if urlsplit(nxt).scheme not in ("http", "https"):
                    raise WhipError("The server redirected to an address that is not http(s).", reply.status, True)
                if origin(nxt) != origin(self.endpoint):
                    auth = False  # the token never leaves the origin the person configured
                if urlsplit(current).scheme == "https" and urlsplit(nxt).scheme == "http":
                    raise WhipError(
                        f"{plain(current)} redirected from https to http; refusing to downgrade.", reply.status, True
                    )
                current = nxt
                continue
            return reply, current

    def _once(
        self,
        method: str,
        url: str,
        body: bytes,
        content_type: str,
        extra: dict[str, str],
        auth: bool,
        send_body: bool,
    ) -> Reply:
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        headers = {"User-Agent": USER_AGENT, "Connection": "close", "Accept": "*/*", **extra}
        if content_type:
            headers["Content-Type"] = content_type
        if send_body:
            headers["Content-Length"] = str(len(body))
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        host = parts.hostname or ""
        try:
            if parts.scheme == "https":
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                    host, parts.port, timeout=self._timeout, context=self._context or ssl.create_default_context()
                )
            else:
                conn = http.client.HTTPConnection(host, parts.port, timeout=self._timeout)
            try:
                conn.request(method, path, body=body if send_body else None, headers=headers)
                response = conn.getresponse()
                data = response.read(MAX_BODY + 1)
                if len(data) > MAX_BODY:
                    raise WhipError(f"{plain(url)} sent an answer larger than {MAX_BODY // 1024} KiB.", fatal=True)
                lowered = {k.lower(): v for k, v in response.getheaders()}
                links = response.msg.get_all("Link") or []
                return Reply(response.status, lowered, links, data)
            finally:
                conn.close()
        except ssl.SSLCertVerificationError as exc:
            raise WhipError(
                f"The certificate of {host} is not trusted ({exc.verify_message}); "
                "a WHIP server needs a valid HTTPS certificate.",
                fatal=True,
            ) from None
        except ssl.SSLError as exc:
            raise WhipError(f"TLS to {host} failed: {exc.reason or exc}.") from None
        except TimeoutError:
            raise WhipError(f"{plain(url)} did not answer within {self._timeout:g} s.") from None
        except socket.gaierror:
            raise WhipError(f"The name {host} cannot be resolved.") from None
        except ConnectionRefusedError:
            raise WhipError(
                f"{host}:{parts.port or (443 if parts.scheme == 'https' else 80)} refused the connection."
            ) from None
        except (http.client.HTTPException, OSError) as exc:
            reason = getattr(exc, "strerror", None) or type(exc).__name__
            raise WhipError(f"Talking to {plain(url)} failed: {reason}.") from None

    def _error(self, reply: Reply, doing: str, url: str) -> WhipError:
        status = reply.status
        where = plain(url)
        retry_after = 0.0
        raw = reply.headers.get("retry-after", "")
        if raw.isdigit():
            retry_after = min(float(raw), MAX_RETRY_AFTER)
        if status in (401, 403):
            hint = (
                "the Bearer token is missing or wrong" if status == 401 else "the token is not allowed to publish here"
            )
            return WhipError(f"{where} refused to {doing} it (HTTP {status}): {hint}.", status, fatal=True)
        if status == 404:
            return WhipError(f"{where} was not found (HTTP 404): check the WHIP address.", status, fatal=True)
        if status == 405:
            return WhipError(
                f"{where} does not accept WHIP publishing (HTTP 405): it is not a WHIP endpoint.", status, fatal=True
            )
        if status == 415:
            return WhipError(f"{where} does not accept an SDP offer (HTTP 415).", status, fatal=True)
        if status in (400, 406, 409, 422, 501):
            detail = reply.body[:160].decode("utf-8", "replace").strip().replace("\n", " ")
            if self._token:
                detail = detail.replace(self._token, "***")  # a server that echoes the request must not leak it
            tail = f": {detail}" if detail and "<" not in detail else ""
            return WhipError(f"{where} rejected the offer (HTTP {status}){tail}.", status, fatal=status != 409)
        if status == 429 or status >= 500:
            return WhipError(f"{where} is busy or down (HTTP {status}).", status, retry_after=retry_after)
        return WhipError(f"{where} answered HTTP {status} when asked to {doing} it.", status)
