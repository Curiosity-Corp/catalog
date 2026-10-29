"""Calendar-aware setup: watch the next meeting and get the rig ready a couple of minutes before it starts.

Sources are Microsoft 365 / Outlook (Microsoft Graph, device-code sign-in, `meetings/graph.py`) and any number of ICS
calendar links (`meetings/ics.py`). The sign-in token and the private links are kept in the keyring, never in the
settings file (`meetings/tokens.py`). A worker thread syncs the sources every few minutes and, when a meeting is
`lead_minutes` away, runs the chosen preparation steps once: start the studio, switch the camera on, unmute the mic,
re-run the health checks. A call that is already running is never touched.

Console actions `meetings.*`: enable, settings, sync, prepare, m365.connect / m365.cancel / m365.disconnect,
ics.add / ics.remove. Snapshot section `meetings` (docs/meetings.md).
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import ipaddress
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

from streamstack.console.errors import ActionError
from streamstack.meetings import config, graph, ics
from streamstack.meetings.model import Meeting
from streamstack.meetings.tokens import KeyringStore, MemorySecrets, SecretError, SecretStore

Notify = Callable[[str, str], None]
Clock = Callable[[], float]
Fetch = Callable[[str], str]


class Microsoft(Protocol):
    """What the controller needs of a Microsoft 365 client (`graph.Graph`, or the demo's scripted one)."""

    def begin(self) -> graph.DeviceCode: ...
    def poll(self, code: graph.DeviceCode) -> str | None: ...
    def connected(self) -> bool: ...
    def account(self) -> str: ...
    def disconnect(self) -> None: ...
    def meetings(self, start: float, end: float) -> list[Meeting]: ...


GraphFactory = Callable[[SecretStore, str, str], Microsoft]

TICK_SECONDS = 5.0
SYNC_SECONDS = 300.0
RETRY_SECONDS = 60.0
LOOKBACK_SECONDS = 3600.0  # a meeting that began less than an hour ago is still "in progress"
HORIZON_SECONDS = 36 * 3600.0
UPCOMING = 6
EVENT_LIMIT = 20
ICS_BYTES = 5_000_000
FETCH_TIMEOUT = 15.0

# What preparing does, one switch each. `skip` names a snapshot test: a step that has nothing to do is not run.
STEPS: dict[str, dict[str, Any]] = {
    "studio": {"label": "Start the studio", "action": "studio.start", "params": {}},
    "camera": {"label": "Camera on (privacy off)", "action": "video.privacy", "params": {"on": False}},
    "mic": {"label": "Unmute the microphone", "action": "audio.mute", "params": {"on": False}},
    "health": {"label": "Run the health checks", "action": "health.refresh", "params": {}},
}


def default_fetch(url: str) -> str:
    """Download an ICS calendar (http, https or webcal), at most ICS_BYTES, as text."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "webcal":
        url = urllib.parse.urlunsplit(("https", *parsed[1:]))
    request = urllib.request.Request(url, headers={"Accept": "text/calendar, */*", "User-Agent": "StreamStack"})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as resp:
            raw = resp.read(ICS_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ActionError(f"The calendar link answered HTTP {exc.code}.", 502) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ActionError(f"The calendar link could not be reached: {getattr(exc, 'reason', exc)}", 502) from exc
    if len(raw) > ICS_BYTES:
        raise ActionError("The calendar is larger than 5 MB.", 502)
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def clean_url(raw: object) -> str:
    """A calendar link the Console will fetch: http, https or webcal, with a host, not a local or private address."""
    if not isinstance(raw, str) or not raw.strip():
        raise ActionError("Paste the calendar link (ICS).", 400)
    url = raw.strip()
    if len(url) > 2000 or re.search(r"\s", url):
        raise ActionError("That does not look like a calendar link.", 400)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https", "webcal") or not parsed.hostname:
        raise ActionError("The link must start with https:// (or webcal://) and name a host.", 400)
    host = parsed.hostname.lower()
    private = host == "localhost" or host.endswith((".local", ".internal"))
    with contextlib.suppress(ValueError):
        address = ipaddress.ip_address(host)
        private = private or address.is_private or address.is_loopback or address.is_link_local
    if private:
        raise ActionError("The link must point at a calendar service on the internet, not at this network.", 400)
    return url


def empty_state() -> dict:
    """The `meetings` snapshot before there is a controller (and what the frontend starts from)."""
    return {
        "available": False,
        "enabled": True,
        "lead_minutes": 2,
        "only_online": True,
        "steps": [{"id": k, "label": v["label"], "on": k in config.DEFAULT_STEPS} for k, v in STEPS.items()],
        "keyring": {"ok": False, "reason": "Meetings are not part of this rig."},
        "sources": [],
        "next": None,
        "upcoming": [],
        "prep": {"state": "idle", "meeting": "", "time": None, "text": "", "steps": []},
        "in_call": False,
        "events": [],
        "limits": {"lead_minutes": list(config.LEAD_RANGE), "ics": config.MAX_ICS},
    }


class MeetingsController:
    """Console actions `meetings.*`; the snapshot section `meetings`."""

    def __init__(
        self,
        store: config.Store | config.MemoryStore | None = None,
        secrets: SecretStore | None = None,
        graph_factory: GraphFactory | None = None,
        fetch: Fetch = default_fetch,
        clock: Clock = time.time,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self.store = store or config.Store()
        self.secrets: SecretStore = secrets or KeyringStore()
        self._graph_factory = graph_factory or self._default_graph
        self._fetch = fetch
        self._clock = clock
        self._tick_seconds = tick_seconds
        self.notify: Notify | None = None
        self.rig: Any = None  # the Rig sets this: preparing runs Console actions
        self._lock = threading.RLock()
        self._settings = self.store.load()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._source: dict[str, dict[str, Any]] = {}  # id -> status, meetings, due
        self._code: graph.DeviceCode | None = None
        self._code_error = ""
        self._code_next = 0.0
        self._graph: Microsoft | None = None
        self._auth: tuple[bool, str] | None = None  # (signed in, account), read from the keyring off the snapshot path
        self._keyring_reason: str | None = None
        self._keyring_known = False
        self._prep: dict[str, Any] = {"state": "idle", "meeting": "", "time": None, "text": "", "steps": []}
        self._events: deque[dict] = deque(maxlen=EVENT_LIMIT)
        self._preparing = False

    @staticmethod
    def _default_graph(secrets: SecretStore, client_id: str, tenant: str) -> Microsoft:
        return graph.Graph(secrets, client_id or graph.DEFAULT_CLIENT_ID, tenant or graph.DEFAULT_TENANT)

    # -- lifecycle ------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="meetings", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(5.0)

    def _run(self) -> None:
        self._refresh_auth()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # the loop must outlive one bad calendar
                self._log("warn", f"Meetings check failed: {exc}")
            self._wake.wait(self._tick_seconds)
            self._wake.clear()

    def _say(self, level: str, text: str) -> None:
        if self.notify:
            self.notify(level, text)

    def _log(self, level: str, text: str) -> None:
        with self._lock:
            self._events.appendleft(
                {"time": time.strftime("%H:%M:%S", time.localtime(self._clock())), "level": level, "text": text}
            )

    # -- the sources ------------------------------------------------------------------

    def _entry(self, ident: str) -> dict[str, Any]:
        return self._source.setdefault(
            ident, {"status": "new", "detail": "", "meetings": [], "last_sync": None, "due": 0.0, "count": 0}
        )

    def _graph_for(self) -> Microsoft:
        with self._lock:
            if self._graph is None:
                m365 = self._settings["m365"]
                self._graph = self._graph_factory(self.secrets, m365["client_id"], m365["tenant"])
            return self._graph

    def _refresh_auth(self) -> None:
        """Read the keyring (the sign-in and whether there is a keyring at all). Never called from `state()`."""
        reason = self.secrets.available()
        try:
            client = self._graph_for()
            auth = (client.connected(), client.account()) if reason is None else (False, "")
        except (graph.GraphError, SecretError):
            auth = (False, "")
        with self._lock:
            self._keyring_reason, self._keyring_known, self._auth = reason, True, auth

    def _signed_in(self) -> bool:
        with self._lock:
            return bool(self._auth and self._auth[0])

    def tick(self) -> None:
        """One pass: finish a sign-in, sync what is due, prepare for a meeting that is close."""
        now = self._clock()
        if self._auth is None:
            self._refresh_auth()
        self._poll_sign_in(now)
        self._sync_due(now)
        self._maybe_prepare(now)

    def _poll_sign_in(self, now: float) -> None:
        with self._lock:
            code, due = self._code, self._code_next
        if code is None or now < due:
            return
        client = self._graph_for()
        try:
            account = client.poll(code)
        except (graph.GraphError, SecretError) as exc:
            with self._lock:
                self._code, self._code_error = None, str(exc)
            self._log("warn", f"Microsoft 365 sign-in: {exc}")
            return
        if account is None:
            with self._lock:
                self._code_next = now + code.interval
            return
        with self._lock:
            self._code, self._code_error = None, ""
            self._entry("m365")["due"] = 0.0
        self._refresh_auth()
        self._log("ok", f"Signed in to Microsoft 365{f' as {account}' if account else ''}.")
        self._say("ok", f"Microsoft 365 connected{f' ({account})' if account else ''}")
        self._sync_due(now)

    def _sources(self) -> list[tuple[str, str]]:
        with self._lock:
            out: list[tuple[str, str]] = [("m365", "Microsoft 365")] if self._signed_in() else []
            out.extend((f"ics:{i['id']}", i["name"] or i["host"]) for i in self._settings["ics"])
            return out

    def _sync_due(self, now: float) -> None:
        for ident, _name in self._sources():
            with self._lock:
                due = self._entry(ident)["due"]
            if now >= due:
                self._sync_one(ident, now)

    def _sync_one(self, ident: str, now: float) -> None:
        window = (now - LOOKBACK_SECONDS, now + HORIZON_SECONDS)
        try:
            found = self._graph_for().meetings(*window) if ident == "m365" else self._ics_meetings(ident, *window)
        except (graph.GraphError, SecretError, ics.IcsError, ActionError) as exc:
            text = exc.message if isinstance(exc, ActionError) else str(exc)
            if ident == "m365" and "Sign in again" in text:
                self._refresh_auth()  # the stored sign-in was revoked and has been forgotten
            with self._lock:
                entry = self._entry(ident)
                entry.update(status="error", detail=text, due=now + RETRY_SECONDS)
            self._log("warn", f"{self._name_of(ident)}: {text}")
            return
        with self._lock:
            entry = self._entry(ident)
            entry.update(
                status="ok", detail="", meetings=found, last_sync=now, due=now + SYNC_SECONDS, count=len(found)
            )

    def _name_of(self, ident: str) -> str:
        if ident == "m365":
            return "Microsoft 365"
        return next((i["name"] or i["host"] for i in self._settings["ics"] if f"ics:{i['id']}" == ident), ident)

    def _ics_meetings(self, ident: str, start: float, end: float) -> list[Meeting]:
        url = self.secrets.get(ident)
        if not url:
            raise ActionError("The calendar link is missing from the keyring. Remove it and add it again.", 409)
        return ics.meetings_in(self._fetch(url), ident, start, end)

    # -- choosing and preparing -----------------------------------------------------------

    def _upcoming(self, now: float) -> list[Meeting]:
        """Meetings not yet over, across sources, earliest first, worth preparing for."""
        with self._lock:
            only_online = self._settings["only_online"]
            every = [m for e in self._source.values() for m in e["meetings"]]
        seen: set[tuple[str, float]] = set()
        out: list[Meeting] = []
        for meeting in sorted(every, key=lambda m: (m.start, m.id)):
            twin = (meeting.title, meeting.start)
            if meeting.end <= now or (only_online and not meeting.online) or twin in seen:
                continue
            seen.add(twin)  # the same meeting seen through two calendars is one meeting
            out.append(meeting)
        return out

    def _maybe_prepare(self, now: float) -> None:
        with self._lock:
            if not self._settings["enabled"] or self._preparing:
                return
            lead = self._settings["lead_minutes"] * 60.0
            prepped = set(self._settings["prepped"])
        for meeting in self._upcoming(now):
            if meeting.start <= now:
                continue  # already begun: too late to prepare, and it is not for us to start anything now
            if meeting.start - lead > now:
                break
            if meeting.id not in prepped:
                self.prepare(meeting, manual=False)
            break

    def prepare(self, meeting: Meeting, manual: bool) -> None:
        """Run the preparation steps for one meeting. Not while a call is running, unless a person asked."""
        with self._lock:
            if self._preparing:
                return
            self._preparing = True
            steps = [s for s in self._settings["steps"] if s in STEPS]
        try:
            self._mark_prepped(meeting.id)
            snap = self.rig.snapshot() if self.rig is not None else {}
            if not manual and self._in_call(snap):
                self._record("skipped", meeting, [], "A call is already running, so the rig was left alone.")
                self._log("info", f"Not preparing for {meeting.title}: a call is already running.")
                return
            results = [self._step(s, snap) for s in steps]
            failed = [r for r in results if not r["ok"]]
            if failed:
                text = f"Prepared for {meeting.title}, but {len(failed)} step(s) did not work."
                self._record("failed", meeting, results, text)
                self._log("warn", text)
                self._say("warn", text)
            else:
                text = f"Ready for {meeting.title}."
                self._record("done", meeting, results, text)
                self._log("ok", text)
                self._say("ok", text)
        finally:
            with self._lock:
                self._preparing = False

    def _in_call(self, snap: dict) -> bool:
        """Is a Teams, Zoom or other call on or starting? The call-awareness controller (#45) knows."""
        call = getattr(self.rig, "call", None)
        return bool(call is not None and call.active())

    def _step(self, step_id: str, snap: dict) -> dict:
        step = STEPS[step_id]
        if step_id == "studio" and (snap.get("studio") or {}).get("running"):
            return {"id": step_id, "label": step["label"], "ok": True, "text": "Already running"}
        if self.rig is None:
            return {"id": step_id, "label": step["label"], "ok": False, "text": "No rig"}
        try:
            message = self.rig.act(step["action"], dict(step["params"]))
        except ActionError as exc:
            return {"id": step_id, "label": step["label"], "ok": False, "text": exc.message}
        return {"id": step_id, "label": step["label"], "ok": True, "text": message or "Done"}

    def _record(self, state: str, meeting: Meeting, steps: list[dict], text: str) -> None:
        with self._lock:
            self._prep = {"state": state, "meeting": meeting.title, "time": self._clock(), "text": text, "steps": steps}

    def _mark_prepped(self, ident: str) -> None:
        with self._lock:
            if ident not in self._settings["prepped"]:
                self._settings["prepped"] = [*self._settings["prepped"], ident][-config.MAX_PREPPED :]
        self._save()

    def _save(self) -> None:
        with self._lock:
            settings = copy.deepcopy(self._settings)
        try:
            self.store.save(settings)
        except OSError as exc:
            raise ActionError(f"The meetings settings could not be saved: {exc}", 500) from exc

    # -- the snapshot -------------------------------------------------------------------------

    def state(self, snap: dict | None = None) -> dict:
        now = self._clock()
        upcoming = self._upcoming(now)
        with self._lock:
            settings = copy.deepcopy(self._settings)
            sources = [self._m365_view(), *(self._ics_view(i) for i in settings["ics"])]
            prepped = set(settings["prepped"])
            lead = settings["lead_minutes"] * 60.0
            shown = []
            for m in upcoming[:UPCOMING]:
                view = m.as_dict()
                view["prep"] = "done" if m.id in prepped else ("waiting" if m.start - lead > now else "due")
                shown.append(view)
            prep = copy.deepcopy(self._prep)
            events = list(self._events)
        return {
            "available": True,
            "enabled": settings["enabled"],
            "lead_minutes": settings["lead_minutes"],
            "only_online": settings["only_online"],
            "steps": [{"id": k, "label": v["label"], "on": k in settings["steps"]} for k, v in STEPS.items()],
            "keyring": {"ok": self._keyring_reason is None, "reason": self._keyring_reason},
            "sources": sources,
            "next": shown[0] if shown else None,
            "upcoming": shown,
            "prep": prep,
            "in_call": self._in_call(snap or {}),
            "events": events,
            "limits": {"lead_minutes": list(config.LEAD_RANGE), "ics": config.MAX_ICS},
        }

    def _m365_view(self) -> dict:
        entry = self._source.get("m365", {})
        connected, account = self._auth or (False, "")
        code = self._code
        if code:
            status = "pending"
        elif self._code_error:
            status = "error"
        elif not connected:
            status = "signed-out"
        else:
            status = "connected" if entry.get("status") == "ok" else entry.get("status", "new")
        view: dict[str, Any] = {
            "id": "m365", "kind": "m365", "name": "Microsoft 365", "status": status,
            "detail": self._code_error or entry.get("detail", ""), "last_sync": entry.get("last_sync"),
            "count": entry.get("count", 0), "account": account, "code": None,
        }  # fmt: skip
        if code:
            view["code"] = {
                "user_code": code.user_code,
                "verification_uri": code.verification_uri,
                "expires_in": max(0, int(code.expires_at - self._clock())),
            }
        return view

    def _ics_view(self, item: dict) -> dict:
        entry = self._source.get(f"ics:{item['id']}", {})
        status = {"ok": "connected", "new": "waiting"}.get(entry.get("status", "new"), entry.get("status", "new"))
        return {
            "id": item["id"], "kind": "ics", "name": item["name"] or item["host"], "host": item["host"],
            "status": status, "detail": entry.get("detail", ""), "last_sync": entry.get("last_sync"),
            "count": entry.get("count", 0),
        }  # fmt: skip

    # -- actions -------------------------------------------------------------------------------

    def act(self, action: str, params: dict) -> str | None:
        handler = {
            "meetings.enable": self._enable,
            "meetings.settings": self._set_settings,
            "meetings.sync": self._sync_now,
            "meetings.prepare": self._prepare_now,
            "meetings.m365.connect": self._connect,
            "meetings.m365.cancel": self._cancel,
            "meetings.m365.disconnect": self._disconnect,
            "meetings.ics.add": self._ics_add,
            "meetings.ics.remove": self._ics_remove,
        }.get(action)
        if handler is None:
            return self._extra(action, params)
        return handler(params)

    def _extra(self, action: str, params: dict) -> str | None:
        raise ActionError("Unknown action.", 400)

    def _enable(self, params: dict) -> str | None:
        on = params.get("on")
        if not isinstance(on, bool):
            raise ActionError("on must be true or false.", 400)
        with self._lock:
            self._settings["enabled"] = on
        self._save()
        return "Meeting setup on" if on else "Meeting setup off"

    def _set_settings(self, params: dict) -> str | None:
        with self._lock:
            settings = copy.deepcopy(self._settings)
        if "lead_minutes" in params:
            lead = params["lead_minutes"]
            low, high = config.LEAD_RANGE
            if not isinstance(lead, int) or isinstance(lead, bool) or not low <= lead <= high:
                raise ActionError(f"lead_minutes is a whole number from {low} to {high}.", 400)
            settings["lead_minutes"] = lead
        if "only_online" in params:
            if not isinstance(params["only_online"], bool):
                raise ActionError("only_online must be true or false.", 400)
            settings["only_online"] = params["only_online"]
        if "steps" in params:
            steps = params["steps"]
            if not isinstance(steps, list) or any(s not in STEPS for s in steps):
                raise ActionError(f"steps is a list of: {', '.join(STEPS)}.", 400)
            settings["steps"] = [s for i, s in enumerate(steps) if s not in steps[:i]]
        with self._lock:
            self._settings = settings
        self._save()
        return "Saved"

    def _sync_now(self, params: dict) -> str | None:
        now = self._clock()
        for ident, _ in self._sources():
            self._sync_one(ident, now)
        self.tick()
        return "Calendars refreshed"

    def _prepare_now(self, params: dict) -> str | None:
        meeting_id = params.get("id")
        pool = self._upcoming(self._clock())
        meeting = next((m for m in pool if m.id == meeting_id), None) if meeting_id else (pool[0] if pool else None)
        if meeting is None:
            meeting = Meeting("manual", "your next call", self._clock(), self._clock(), "", "manual")
        self.prepare(meeting, manual=True)
        return self._prep["text"]

    def _connect(self, params: dict) -> str | None:
        client_id = str(params.get("client_id") or "").strip()
        tenant = str(params.get("tenant") or "").strip()
        try:
            client = self._graph_factory(self.secrets, client_id, tenant)
            code = client.begin()
        except (graph.GraphError, SecretError) as exc:
            raise ActionError(str(exc), 409) from exc
        with self._lock:
            self._settings["m365"] = {"client_id": client_id, "tenant": tenant}
            self._graph, self._code, self._code_error = client, code, ""
            self._code_next = self._clock() + code.interval
        self._save()
        self._wake.set()
        return f"Open {code.verification_uri} and enter {code.user_code}"

    def _cancel(self, params: dict) -> str | None:
        with self._lock:
            self._code, self._code_error = None, ""
        return None

    def _disconnect(self, params: dict) -> str | None:
        try:
            self._graph_for().disconnect()
        except (graph.GraphError, SecretError) as exc:
            raise ActionError(str(exc), 409) from exc
        with self._lock:
            self._source.pop("m365", None)
            self._code, self._code_error = None, ""
        self._refresh_auth()
        return "Signed out of Microsoft 365"

    def _ics_add(self, params: dict) -> str | None:
        url = clean_url(params.get("link"))
        host = urllib.parse.urlsplit(url).hostname or ""
        name = str(params.get("name") or "").strip()[:60] or host
        with self._lock:
            if len(self._settings["ics"]) >= config.MAX_ICS:
                raise ActionError(f"At most {config.MAX_ICS} calendar links.", 409)
        reason = self.secrets.available()
        if reason:
            raise ActionError(reason, 409)
        now = self._clock()
        ident = hashlib.sha256(f"{url}{now}".encode()).hexdigest()[:8]
        try:
            found = ics.meetings_in(self._fetch(url), f"ics:{ident}", now - LOOKBACK_SECONDS, now + HORIZON_SECONDS)
        except ics.IcsError as exc:
            raise ActionError(str(exc), 400) from exc
        try:
            self.secrets.set(f"ics:{ident}", url)
        except SecretError as exc:
            raise ActionError(str(exc), 409) from exc
        self._refresh_auth()
        with self._lock:
            self._settings["ics"] = [*self._settings["ics"], {"id": ident, "name": name, "host": host}]
            self._entry(f"ics:{ident}").update(
                status="ok", detail="", meetings=found, last_sync=now, due=now + SYNC_SECONDS, count=len(found)
            )
        self._save()
        return f"Added {name}"

    def _ics_remove(self, params: dict) -> str | None:
        ident = params.get("id")
        with self._lock:
            known = next((i for i in self._settings["ics"] if i["id"] == ident), None)
        if known is None:
            raise ActionError("No such calendar.", 404)
        try:
            self.secrets.delete(f"ics:{ident}")
        except SecretError as exc:
            raise ActionError(str(exc), 409) from exc
        with self._lock:
            self._settings["ics"] = [i for i in self._settings["ics"] if i["id"] != ident]
            self._source.pop(f"ics:{ident}", None)
        self._save()
        return "Calendar removed"


# -- the demo rig ------------------------------------------------------------------------------------------------------


class FakeMicrosoft:
    """A scripted Microsoft 365 for the demo rig: a sign-in a person approves with `meetings.demo.approve`."""

    def __init__(self, secrets: SecretStore, owner: DemoMeetings) -> None:
        self.secrets = secrets
        self._owner = owner
        self.signed_in = False

    def available(self) -> str | None:
        return self.secrets.available()

    def begin(self) -> graph.DeviceCode:
        return graph.DeviceCode("DEMO-1234", "https://microsoft.com/devicelogin", "demo", 1.0, time.time() + 900)

    def poll(self, code: graph.DeviceCode) -> str | None:
        if not self._owner.approved:
            return None
        self.signed_in = True
        self.secrets.set("m365", "demo-refresh-token")
        return "demo@example.com"

    def connected(self) -> bool:
        return self.signed_in or bool(self.secrets.get("m365"))

    def account(self) -> str:
        return "demo@example.com" if self.connected() else ""

    def disconnect(self) -> None:
        self.signed_in = False
        self._owner.approved = False
        self.secrets.delete("m365")

    def meetings(self, start: float, end: float) -> list[Meeting]:
        return [m for m in self._owner.cloud if m.end >= start and m.start <= end]


def _stamp(moment: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(moment))


class DemoMeetings(MeetingsController):
    """The demo rig's meetings: memory only, a scripted Microsoft 365 and calendar links `meetings.demo.*` can fill."""

    def __init__(self, clock: Clock = time.time) -> None:
        self.approved = False
        self.cloud: list[Meeting] = []
        self.feed: dict[str, list[tuple[str, float, float, bool]]] = {}
        secrets = MemorySecrets()
        super().__init__(config.MemoryStore(), secrets, self._fake_graph, self._fake_fetch, clock)

    def _fake_graph(self, secrets: SecretStore, client_id: str, tenant: str) -> Microsoft:
        return FakeMicrosoft(secrets, self)

    def _fake_fetch(self, url: str) -> str:
        if url not in self.feed:
            raise ActionError("The calendar link answered HTTP 404.", 502)
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0"]
        for index, (title, start, end, online) in enumerate(self.feed[url]):
            lines += [
                "BEGIN:VEVENT",
                f"UID:demo-{index}",
                f"SUMMARY:{title}",
                f"DTSTART:{_stamp(start)}",
                f"DTEND:{_stamp(end)}",
            ]
            if online:
                lines.append("DESCRIPTION:https://teams.microsoft.com/l/meetup-join/demo")
            lines.append("END:VEVENT")
        return "\r\n".join([*lines, "END:VCALENDAR", ""])

    def _extra(self, action: str, params: dict) -> str | None:
        now = self._clock()
        if action == "meetings.demo.cloud":
            minutes, title = params.get("minutes"), str(params.get("title") or "Demo meeting")
            if not isinstance(minutes, (int, float)):
                raise ActionError("minutes is required.", 400)
            start = now + float(minutes) * 60
            self.cloud.append(
                Meeting(
                    f"m365:demo{len(self.cloud)}:{int(start)}",
                    title,
                    start,
                    start + 1800,
                    "https://teams.microsoft.com/l/demo",
                    "m365",
                )
            )
            return None
        if action == "meetings.demo.feed":
            url, minutes = str(params.get("link") or ""), params.get("minutes")
            if not isinstance(minutes, (int, float)):
                raise ActionError("minutes is required.", 400)
            start = now + float(minutes) * 60
            self.feed.setdefault(url, []).append(
                (
                    str(params.get("title") or "Demo meeting"),
                    start,
                    start + 1800,
                    params.get("online", True) is not False,
                )
            )
            return None
        if action == "meetings.demo.approve":
            self.approved = params.get("on", True) is not False
            return None
        if action == "meetings.demo.tick":
            self.tick()
            return None
        if action == "meetings.demo.reset":
            with self._lock:
                self._settings = config.defaults()
                self._source.clear()
                self._code, self._code_error, self._graph = None, "", None
                self._prep = {"state": "idle", "meeting": "", "time": None, "text": "", "steps": []}
                self._events.clear()
            self.store.save(self._settings)
            self.approved, self.cloud, self.feed = False, [], {}
            self.secrets = MemorySecrets()
            self._auth = None
            return None
        return super()._extra(action, params)
