"""The WHIP HTTP exchange against a scripted local server: Location and ETag handling, redirects, trickle, errors."""

from __future__ import annotations

import http.server
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest

from streamstack.stream.whip import protocol
from streamstack.stream.whip.protocol import WhipClient, WhipError

ANSWER = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Script:
    """What the fake server does for each request: a function from the request to (status, headers, body)."""

    handler: Callable[[Seen], tuple[int, dict[str, str], bytes]]
    seen: list[Seen] = field(default_factory=list)


def make_server(script: Script) -> tuple[http.server.ThreadingHTTPServer, str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            return

        def respond(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            request = Seen(self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body)
            script.seen.append(request)
            status, headers, payload = script.handler(request)
            self.send_response(status)
            links = headers.pop("Link", None)
            for key, value in headers.items():
                self.send_header(key, value)
            for link in (links or "").split("|") if links else []:
                self.send_header("Link", link)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

        do_POST = do_PATCH = do_DELETE = do_GET = respond

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def serve() -> Iterator[Callable[[Callable[[Seen], tuple[int, dict[str, str], bytes]]], tuple[Script, str]]]:
    servers: list[http.server.ThreadingHTTPServer] = []

    def start(handler: Callable[[Seen], tuple[int, dict[str, str], bytes]]) -> tuple[Script, str]:
        script = Script(handler)
        server, base = make_server(script)
        servers.append(server)
        return script, base

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def created(location: str = "/whip/abc", **extra: str) -> tuple[int, dict[str, str], bytes]:
    return 201, {"Content-Type": "application/sdp", "Location": location, **extra}, ANSWER.encode()


class TestPublish:
    def test_post_sends_the_offer_and_the_bearer_token(self, serve) -> None:
        script, base = serve(lambda r: created())
        resource = WhipClient(f"{base}/whip", "s3cret").publish("v=0\r\nOFFER")
        request = script.seen[0]
        assert request.method == "POST" and request.path == "/whip" and request.body == b"v=0\r\nOFFER"
        assert request.headers["content-type"] == "application/sdp"
        assert request.headers["authorization"] == "Bearer s3cret"
        assert resource.answer == ANSWER and resource.url == f"{base}/whip/abc" and resource.origin_token

    def test_no_token_means_no_authorization_header(self, serve) -> None:
        script, base = serve(lambda r: created())
        WhipClient(f"{base}/whip").publish("v=0\r\n")
        assert "authorization" not in script.seen[0].headers

    def test_a_relative_location_is_resolved_against_the_endpoint(self, serve) -> None:
        _, base = serve(lambda r: created("../resource/9"))
        assert WhipClient(f"{base}/a/b/whip").publish("v=0\r\n").url == f"{base}/a/resource/9"

    def test_an_absolute_location_on_another_host_is_kept_but_never_gets_the_token(self, serve) -> None:
        _, base = serve(lambda r: created("http://localhost:1/other"))
        resource = WhipClient(f"{base}/whip", "tok").publish("v=0\r\n")
        assert resource.url == "http://localhost:1/other" and not resource.origin_token

    def test_the_query_of_the_endpoint_is_kept(self, serve) -> None:
        script, base = serve(lambda r: created())
        WhipClient(f"{base}/whip?room=7").publish("v=0\r\n")
        assert script.seen[0].path == "/whip?room=7"

    def test_etag_and_accept_patch_and_ice_servers(self, serve) -> None:
        links = (
            '<stun:stun.example.com:3478>; rel="ice-server"|<turn:turn.example.com>; rel="ice-server"; '
            'username="u"; credential="p"|<https://x/y>; rel="other"'
        )
        _, base = serve(
            lambda r: created(ETag='"v1"', **{"Accept-Patch": "application/trickle-ice-sdpfrag", "Link": links})
        )
        resource = WhipClient(f"{base}/whip").publish("v=0\r\n")
        assert resource.etag == '"v1"' and resource.trickle
        assert [(s.urls, s.username, s.credential) for s in resource.ice_servers] == [
            (("stun:stun.example.com:3478",), "", ""),
            (("turn:turn.example.com",), "u", "p"),
        ]

    def test_200_is_accepted_too(self, serve) -> None:
        _, base = serve(lambda r: (200, {"Content-Type": "application/sdp", "Location": "/r"}, ANSWER.encode()))
        assert WhipClient(f"{base}/w").publish("v=0\r\n").url.endswith("/r")

    def test_307_is_followed_with_the_body_and_the_token_on_the_same_origin(self, serve) -> None:
        def handler(r: Seen) -> tuple[int, dict[str, str], bytes]:
            return (307, {"Location": "/real"}, b"") if r.path == "/old" else created("/res")

        script, base = serve(handler)
        resource = WhipClient(f"{base}/old", "tok").publish("v=0\r\nOFFER")
        assert [s.path for s in script.seen] == ["/old", "/real"]
        assert script.seen[1].method == "POST" and script.seen[1].body == b"v=0\r\nOFFER"
        assert script.seen[1].headers["authorization"] == "Bearer tok"
        assert resource.url == f"{base}/res"

    def test_a_redirect_to_another_origin_drops_the_token(self, serve) -> None:
        other_script, other = serve(lambda r: created("/res"))
        _, base = serve(lambda r: (307, {"Location": f"{other}/moved"}, b""))
        resource = WhipClient(f"{base}/w", "tok").publish("v=0\r\n")
        assert "authorization" not in other_script.seen[0].headers
        assert resource.url == f"{other}/res" and not resource.origin_token

    def test_redirect_loops_stop(self, serve) -> None:
        _, base = serve(lambda r: (307, {"Location": "/again"}, b""))
        with pytest.raises(WhipError, match="Too many redirects") as err:
            WhipClient(f"{base}/w").publish("v=0\r\n")
        assert err.value.fatal

    def test_https_to_http_redirects_are_refused(self) -> None:
        client = WhipClient("https://example.test/w")
        reply = protocol.Reply(307, {"location": "http://example.test/w"}, [], b"")
        client._once = lambda *a, **k: reply  # type: ignore[method-assign]
        with pytest.raises(WhipError, match="downgrade"):
            client.publish("v=0\r\n")

    @pytest.mark.parametrize(
        ("response", "message"),
        [
            ((200, {"Content-Type": "text/html", "Location": "/r"}, b"<html>"), "does not look like a WHIP endpoint"),
            ((201, {"Content-Type": "application/sdp"}, ANSWER.encode()), "no Location"),
            ((201, {"Content-Type": "application/sdp", "Location": "ftp://x/y"}, ANSWER.encode()), "not an http"),
        ],
    )
    def test_a_reply_that_is_not_whip_is_fatal(self, serve, response, message) -> None:
        _, base = serve(lambda r: response)
        with pytest.raises(WhipError, match=message) as err:
            WhipClient(f"{base}/w").publish("v=0\r\n")
        assert err.value.fatal

    def test_an_endpoint_that_is_not_http_is_rejected_up_front(self) -> None:
        with pytest.raises(WhipError, match="https://host/path") as err:
            WhipClient("rtmp://x/y")
        assert err.value.fatal


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "fatal", "text"),
        [
            (401, True, "Bearer token is missing or wrong"),
            (403, True, "not allowed to publish"),
            (404, True, "check the WHIP address"),
            (405, True, "not a WHIP endpoint"),
            (415, True, "SDP offer"),
            (400, True, "rejected the offer"),
            (406, True, "rejected the offer"),
            (422, True, "rejected the offer"),
            (409, False, "rejected the offer"),
            (429, False, "busy or down"),
            (500, False, "busy or down"),
            (503, False, "busy or down"),
        ],
    )
    def test_status_classification(self, serve, status: int, fatal: bool, text: str) -> None:
        _, base = serve(lambda r: (status, {}, b""))
        with pytest.raises(WhipError, match=text) as err:
            WhipClient(f"{base}/w", "tok").publish("v=0\r\n")
        assert err.value.status == status and err.value.fatal is fatal

    def test_retry_after_is_honoured_and_capped(self, serve) -> None:
        _, base = serve(lambda r: (503, {"Retry-After": "7"}, b""))
        with pytest.raises(WhipError) as err:
            WhipClient(f"{base}/w").publish("v=0\r\n")
        assert err.value.retry_after == 7.0
        _, base = serve(lambda r: (429, {"Retry-After": "86400"}, b""))
        with pytest.raises(WhipError) as err:
            WhipClient(f"{base}/w").publish("v=0\r\n")
        assert err.value.retry_after == protocol.MAX_RETRY_AFTER

    def test_neither_the_token_nor_the_query_appears_in_an_error(self, serve) -> None:
        _, base = serve(lambda r: (400, {}, b"bad offer from tok-123 sorry"))
        with pytest.raises(WhipError) as err:
            WhipClient(f"{base}/w?key=querysecret", "tok-123").publish("v=0\r\n")
        assert "tok-123" not in str(err.value) and "querysecret" not in str(err.value)
        assert "bad offer" in str(err.value)

    def test_html_error_bodies_are_not_pasted_into_the_message(self, serve) -> None:
        _, base = serve(lambda r: (400, {}, b"<html><body>Bad Request</body></html>"))
        with pytest.raises(WhipError) as err:
            WhipClient(f"{base}/w").publish("v=0\r\n")
        assert "<" not in str(err.value)

    def test_a_server_that_never_answers_times_out(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            with pytest.raises(WhipError, match="did not answer within") as err:
                WhipClient(f"http://127.0.0.1:{listener.getsockname()[1]}/w", timeout=0.3).publish("v=0\r\n")
            assert not err.value.fatal and err.value.status == 0
        finally:
            listener.close()

    def test_a_refused_connection_is_retryable(self) -> None:
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        with pytest.raises(WhipError, match="refused the connection") as err:
            WhipClient(f"http://127.0.0.1:{port}/w", timeout=2).publish("v=0\r\n")
        assert not err.value.fatal

    def test_an_unknown_name_says_so(self) -> None:
        with pytest.raises(WhipError, match="cannot be resolved") as err:
            WhipClient("http://no-such-host.invalid/w", timeout=5).publish("v=0\r\n")
        assert not err.value.fatal

    def test_an_oversized_answer_is_refused(self, serve) -> None:
        _, base = serve(lambda r: (200, {"Content-Type": "application/sdp"}, b"v=0" + b"a" * (protocol.MAX_BODY + 5)))
        with pytest.raises(WhipError, match="larger than"):
            WhipClient(f"{base}/w").publish("v=0\r\n")


class TestTrickleAndDelete:
    def resource(self, base: str, etag: str = '"e1"') -> protocol.Resource:
        return protocol.Resource(f"{base}/whip/r", ANSWER, etag, trickle=True)

    def test_patch_sends_the_fragment_with_if_match_and_takes_the_new_etag(self, serve) -> None:
        script, base = serve(lambda r: (204, {"ETag": '"e2"'}, b""))
        res = self.resource(base)
        fragment = protocol.build_sdpfrag("uf", "pw", "0", ["1 1 UDP 100 10.0.0.1 5000 typ host"], end=True)
        assert WhipClient(f"{base}/whip", "tok").patch(res, fragment) == '"e2"'
        request = script.seen[0]
        assert request.method == "PATCH" and request.path == "/whip/r"
        assert request.headers["if-match"] == '"e1"' and request.headers["content-type"] == protocol.SDPFRAG
        assert request.headers["authorization"] == "Bearer tok"
        assert b"a=candidate:1 1 UDP 100 10.0.0.1 5000 typ host" in request.body
        assert b"a=end-of-candidates" in request.body and b"a=ice-ufrag:uf" in request.body
        assert res.etag == '"e2"'

    def test_patch_without_an_etag_sends_if_match_star(self, serve) -> None:
        script, base = serve(lambda r: (204, {}, b""))
        WhipClient(f"{base}/whip").patch(self.resource(base, etag=""), "a=x\r\n")
        assert script.seen[0].headers["if-match"] == "*"

    @pytest.mark.parametrize(
        ("status", "error"),
        [
            (405, protocol.TrickleUnsupported),
            (501, protocol.TrickleUnsupported),
            (412, protocol.PreconditionFailed),
            (404, protocol.SessionGone),
            (410, protocol.SessionGone),
            (500, WhipError),
        ],
    )
    def test_patch_errors(self, serve, status: int, error: type[WhipError]) -> None:
        _, base = serve(lambda r: (status, {}, b""))
        with pytest.raises(error):
            WhipClient(f"{base}/whip").patch(self.resource(base), "a=x\r\n")

    def test_patch_is_not_redirected(self, serve) -> None:
        script, base = serve(lambda r: (307, {"Location": "/elsewhere"}, b""))
        with pytest.raises(WhipError):
            WhipClient(f"{base}/whip").patch(self.resource(base), "a=x\r\n")
        assert len(script.seen) == 1

    @pytest.mark.parametrize("status", [200, 202, 204, 404, 410])
    def test_delete_succeeds_and_a_gone_session_counts(self, serve, status: int) -> None:
        script, base = serve(lambda r: (status, {}, b""))
        WhipClient(f"{base}/whip", "tok").delete(self.resource(base))
        request = script.seen[0]
        assert request.method == "DELETE" and request.path == "/whip/r" and request.body == b""
        assert request.headers["authorization"] == "Bearer tok"

    def test_delete_failure_is_reported(self, serve) -> None:
        _, base = serve(lambda r: (500, {}, b""))
        with pytest.raises(WhipError, match="busy or down"):
            WhipClient(f"{base}/whip").delete(self.resource(base))

    def test_the_token_is_not_sent_to_a_resource_on_another_origin(self, serve) -> None:
        script, base = serve(lambda r: (204, {}, b""))
        res = protocol.Resource(f"{base}/whip/r", ANSWER, origin_token=False)
        WhipClient("http://127.0.0.1:1/w", "tok").delete(res)
        assert "authorization" not in script.seen[0].headers


class TestHelpers:
    def test_plain_strips_query_fragment_and_credentials(self) -> None:
        assert protocol.plain("https://u:p@host.test:8443/a/b?token=x#f") == "https://host.test:8443/a/b"
        assert protocol.plain("http://[::1]:9/x?y") == "http://[::1]:9/x"

    def test_origin_defaults_ports(self) -> None:
        assert protocol.origin("https://H.test/a") == protocol.origin("https://h.test:443/b")
        assert protocol.origin("http://h.test/") != protocol.origin("https://h.test/")

    def test_link_headers_ignore_malformed_and_other_relations(self) -> None:
        values = ['<stun:s.test>; rel="ice-server"', "<>; rel=ice-server", "garbage", '<http://x>; rel="ice-server"']
        assert [s.urls for s in protocol.parse_link_headers(values)] == [("stun:s.test",)]

    def test_sdpfrag_prefixes_candidates(self) -> None:
        text = protocol.build_sdpfrag(
            "u", "p", "3", ["candidate:1 1 UDP 1 1.1.1.1 1 typ host", "2 1 UDP 1 2.2.2.2 2 typ host"]
        )
        assert "a=candidate:1 1 UDP" in text and "a=candidate:2 1 UDP" in text and "end-of-candidates" not in text
        assert "a=mid:3" in text
