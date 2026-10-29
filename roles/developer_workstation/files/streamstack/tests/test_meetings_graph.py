"""Microsoft Graph: the device-code sign-in, token refresh, and the calendar view, against a fake of both endpoints."""

# ruff: noqa: E501  (fixtures are long single lines)

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from streamstack.meetings import graph
from streamstack.meetings.tokens import MemorySecrets, SecretError

CLIENT = "11111111-2222-3333-4444-555555555555"


class FakeMicrosoft:
    """Records requests and answers like login.microsoftonline.com and graph.microsoft.com."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], dict[str, str]]] = []
        self.device_answers: list[dict[str, Any]] = [{"error": "authorization_pending"}]
        self.refresh_answer: tuple[int, dict[str, Any]] = (
            200,
            {"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
        )
        self.events_pages: list[dict[str, Any]] = []
        self.events_status = 200

    def __call__(self, method: str, url: str, headers: dict[str, str], data: bytes | None) -> tuple[int, dict]:
        form = dict(urllib.parse.parse_qsl(data.decode())) if data else {}
        self.calls.append((method, url, headers, form))
        if url.endswith("/devicecode"):
            return 200, {
                "user_code": "ABCD-1234", "device_code": "dev-code", "verification_uri": "https://microsoft.com/devicelogin",
                "interval": 5, "expires_in": 900, "message": "go",
            }  # fmt: skip
        if url.endswith("/token") and form.get("grant_type", "").endswith("device_code"):
            answer = self.device_answers.pop(0) if self.device_answers else {"error": "authorization_pending"}
            if "error" in answer:
                return 400, answer
            return 200, answer
        if url.endswith("/token"):
            return self.refresh_answer
        if "/me?" in url:
            return 200, {"mail": "jo@example.com"}
        if "/me/calendarView" in url or "page2" in url:
            if self.events_status != 200:
                return self.events_status, {"error": {"message": "nope"}}
            return 200, self.events_pages.pop(0) if self.events_pages else {"value": []}
        return 404, {}


def make(fake: FakeMicrosoft, secrets: MemorySecrets | None = None, now: list[float] | None = None) -> graph.Graph:
    clock = now if now is not None else [1000.0]
    return graph.Graph(secrets or MemorySecrets(), client_id=CLIENT, http=fake, clock=lambda: clock[0])


def sign_in(g: graph.Graph, fake: FakeMicrosoft) -> None:
    fake.device_answers = [{"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600}]
    assert g.poll(g.begin()) == "jo@example.com"


def test_the_device_code_flow_waits_then_signs_in_and_keeps_only_the_refresh_token() -> None:
    fake, secrets = FakeMicrosoft(), MemorySecrets()
    g = make(fake, secrets)
    code = g.begin()
    assert (code.user_code, code.verification_uri) == ("ABCD-1234", "https://microsoft.com/devicelogin")
    start = fake.calls[0]
    assert start[3] == {"client_id": CLIENT, "scope": graph.SCOPES}
    assert g.poll(code) is None  # pending
    fake.device_answers = [{"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600}]
    assert g.poll(code) == "jo@example.com"
    stored = json.loads(secrets.get("m365") or "{}")
    assert stored["refresh_token"] == "refresh-1" and "access" not in json.dumps(stored)
    assert g.connected() and g.account() == "jo@example.com"
    assert secrets.names() == ["m365"]


def test_slow_down_widens_the_interval_and_declined_or_expired_codes_raise() -> None:
    fake = FakeMicrosoft()
    g = make(fake)
    code = g.begin()
    fake.device_answers = [{"error": "slow_down"}]
    assert g.poll(code) is None and code.interval == 10
    fake.device_answers = [{"error": "authorization_declined"}]
    with pytest.raises(graph.GraphError, match="declined"):
        g.poll(code)
    fake.device_answers = [{"error": "expired_token"}]
    with pytest.raises(graph.GraphError, match="expired"):
        g.poll(code)
    now = [1000.0]
    g2 = make(fake, now=now)
    late = g2.begin()
    now[0] += 901
    with pytest.raises(graph.GraphError, match="expired"):
        g2.poll(late)


def test_no_keyring_means_no_sign_in_at_all() -> None:
    fake = FakeMicrosoft()
    g = make(fake, MemorySecrets("There is no keyring."))
    with pytest.raises(graph.GraphError, match="no keyring"):
        g.begin()
    assert fake.calls == []  # the person is never shown a code that could not be kept


def test_the_access_token_is_reused_then_refreshed_and_the_new_refresh_token_is_saved() -> None:
    fake, secrets, now = FakeMicrosoft(), MemorySecrets(), [1000.0]
    g = make(fake, secrets, now)
    sign_in(g, fake)
    assert g.access_token() == "access-1"  # still fresh: no call
    assert not [c for c in fake.calls if c[3].get("grant_type") == "refresh_token"]
    now[0] += 3600
    assert g.access_token() == "access-2"
    refresh = next(c for c in fake.calls if c[3].get("grant_type") == "refresh_token")
    assert refresh[3]["refresh_token"] == "refresh-1"
    assert json.loads(secrets.get("m365") or "{}")["refresh_token"] == "refresh-2"


def test_a_revoked_sign_in_is_forgotten_and_says_to_sign_in_again() -> None:
    fake, now = FakeMicrosoft(), [1000.0]
    g = make(fake, now=now)
    sign_in(g, fake)
    now[0] += 4000
    fake.refresh_answer = (400, {"error": "invalid_grant", "error_description": "revoked"})
    with pytest.raises(graph.GraphError, match="Sign in again"):
        g.access_token()
    assert not g.connected()


def test_not_signed_in_is_an_error_not_a_crash() -> None:
    with pytest.raises(graph.GraphError, match="not signed in"):
        make(FakeMicrosoft()).meetings(0, 1)


def item(ident: str, start: str, end: str, **extra: Any) -> dict[str, Any]:
    return {"id": ident, "subject": f"s-{ident}", "start": {"dateTime": start}, "end": {"dateTime": end}, **extra}


def test_the_calendar_view_lists_timed_meetings_with_join_links_and_follows_pages() -> None:
    fake = FakeMicrosoft()
    g = make(fake)
    sign_in(g, fake)
    fake.events_pages = [
        {
            "value": [
                item("a", "2026-10-28T14:00:00.0000000", "2026-10-28T14:30:00.0000000", onlineMeeting={"joinUrl": "https://teams.microsoft.com/l/x"}),
                item("allday", "2026-10-28T00:00:00.0000000", "2026-10-29T00:00:00.0000000", isAllDay=True),
                item("cancelled", "2026-10-28T15:00:00.0000000", "2026-10-28T16:00:00.0000000", isCancelled=True),
                item("declined", "2026-10-28T16:00:00.0000000", "2026-10-28T17:00:00.0000000", responseStatus={"response": "declined"}),
                {"id": "broken", "subject": "x"},
            ],
            "@odata.nextLink": "https://graph.example/page2",
        },
        {"value": [item("b", "2026-10-28T09:00:00.0000000", "2026-10-28T09:15:00.0000000", location={"displayName": "https://meet.google.com/abc-defg-hij"}),
                   item("c", "2026-10-28T10:00:00.0000000", "2026-10-28T10:15:00.0000000", isOnlineMeeting=True),
                   item("d", "2026-10-28T11:00:00.0000000", "2026-10-28T11:15:00.0000000")]},
    ]  # fmt: skip
    found = g.meetings(1793000000, 1793100000)
    assert [(m.title, m.join) for m in found] == [
        ("s-b", "https://meet.google.com/abc-defg-hij"), ("s-c", "online"), ("s-d", ""),
        ("s-a", "https://teams.microsoft.com/l/x"),
    ]  # fmt: skip
    assert found[3].start == 1793196000.0 and found[3].end == 1793197800.0  # 2026-10-28 14:00 to 14:30 UTC
    request = next(c for c in fake.calls if "calendarView" in c[1])
    assert request[2]["Authorization"] == "Bearer access-1" and request[2]["Prefer"] == 'outlook.timezone="UTC"'
    assert "startDateTime=2026-10-" in request[1]


def test_a_calendar_error_is_reported_in_words() -> None:
    fake = FakeMicrosoft()
    g = make(fake)
    sign_in(g, fake)
    fake.events_status = 403
    with pytest.raises(graph.GraphError, match="would not list the calendar: nope"):
        g.meetings(0, 1)


def test_a_bad_client_id_or_tenant_is_refused() -> None:
    with pytest.raises(graph.GraphError):
        graph.Graph(MemorySecrets(), client_id="not-a-guid")
    with pytest.raises(graph.GraphError):
        graph.Graph(MemorySecrets(), client_id=CLIENT, tenant="../evil")


def test_memory_secrets_with_a_reason_refuse_everything() -> None:
    secrets = MemorySecrets("no keyring")
    with pytest.raises(SecretError):
        secrets.set("a", "b")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self._answer(200, {"device_code": "d", "user_code": "U-1", "interval": 5, "expires_in": 900})

    def do_GET(self) -> None:
        self._answer(404, {"error": {"message": "no such thing"}})

    def _answer(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: object) -> None:
        return None


def test_the_real_http_function_talks_json_over_a_socket_and_reports_a_dead_server() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        g = graph.Graph(MemorySecrets(), client_id=CLIENT, authority=base)
        assert g.begin().user_code == "U-1"
        assert graph.urllib_http("GET", f"{base}/x", {}, None)[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    with pytest.raises(graph.GraphError, match="Could not reach Microsoft"):
        graph.urllib_http("GET", f"{base}/x", {}, None)
