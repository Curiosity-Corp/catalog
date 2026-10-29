"""Microsoft 365 / Outlook calendars through Microsoft Graph, signed in with the device-code flow.

The same sign-in the Teams plugin of the developer workstation uses (`developer_workstation/files/teams.py`): the app
shows a short code, the person types it at microsoft.com/devicelogin on any device, and the app polls the token
endpoint until they are done. Only a refresh token is kept, in the keyring (`tokens.py`); access tokens live in memory
for their hour. The scopes are read-only: `Calendars.Read offline_access User.Read`.

    begin() -> DeviceCode          show `user_code` and `verification_uri`
    poll(code) -> account | None   None while the person has not finished; raises GraphError when it cannot succeed
    meetings(start, end)           the timed, not cancelled, not declined meetings in the window

All network access goes through one injected `http` function, so tests run against a local fake of the two
endpoints and never touch the internet.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from streamstack.meetings.model import Meeting, find_join_link
from streamstack.meetings.tokens import SecretError, SecretStore

# The public client the developer workstation's Teams plugin signs in with (multi-tenant, device code allowed).
DEFAULT_CLIENT_ID = "fcda3faa-1259-4b56-a04d-3281fc98d8f1"
DEFAULT_TENANT = "organizations"
AUTHORITY = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "Calendars.Read offline_access User.Read"
KEYRING_ITEM = "m365"
TIMEOUT = 15.0
MAX_PAGES = 5
REFRESH_MARGIN = 120.0
CLIENT_ID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
TENANT = re.compile(r"organizations|common|consumers|[0-9a-fA-F-]{36}|[a-zA-Z0-9.-]{3,80}")

Http = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, Any]]]


class GraphError(RuntimeError):
    """A sign-in or calendar call failed; the message is for the person."""


def urllib_http(method: str, url: str, headers: dict[str, str], data: bytes | None) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            raw, status = resp.read(4_000_000), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(1_000_000), exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GraphError(f"Could not reach Microsoft: {getattr(exc, 'reason', exc)}") from exc
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = {}
    return status, body if isinstance(body, dict) else {}


@dataclass
class DeviceCode:
    user_code: str
    verification_uri: str
    device_code: str
    interval: float
    expires_at: float
    message: str = ""


def _form(values: dict[str, str]) -> bytes:
    return urllib.parse.urlencode(values).encode()


def _error(body: dict[str, Any], status: int) -> str:
    error = body.get("error")
    text = error.get("message", "") if isinstance(error, dict) else body.get("error_description") or error or ""
    return str(text).split("\r\n")[0].strip() or f"HTTP {status}"


class Graph:
    def __init__(
        self,
        secrets: SecretStore,
        client_id: str = DEFAULT_CLIENT_ID,
        tenant: str = DEFAULT_TENANT,
        authority: str = AUTHORITY,
        graph: str = GRAPH,
        http: Http = urllib_http,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not CLIENT_ID.fullmatch(client_id):
            raise GraphError("The application (client) id must look like 00000000-0000-0000-0000-000000000000.")
        if not TENANT.fullmatch(tenant):
            raise GraphError("The tenant must be organizations, common, consumers, a tenant id or a domain.")
        self.secrets = secrets
        self.client_id = client_id
        self.tenant = tenant
        self._auth = f"{authority.rstrip('/')}/{tenant}/oauth2/v2.0"
        self._graph = graph.rstrip("/")
        self._http = http
        self._clock = clock
        self._access: tuple[str, float] | None = None

    # -- the stored sign-in ---------------------------------------------------------

    def _saved(self) -> dict[str, str] | None:
        raw = self.secrets.get(KEYRING_ITEM)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) and data.get("refresh_token") else None

    def connected(self) -> bool:
        try:
            return self._saved() is not None
        except SecretError:
            return False

    def account(self) -> str:
        saved = self._saved()
        return str(saved.get("account", "")) if saved else ""

    def disconnect(self) -> None:
        self._access = None
        self.secrets.delete(KEYRING_ITEM)

    # -- the device-code sign-in ----------------------------------------------------------

    def begin(self) -> DeviceCode:
        reason = self.secrets.available()
        if reason:  # fail before the person types a code that could not be kept
            raise GraphError(reason)
        status, body = self._http(
            "POST",
            f"{self._auth}/devicecode",
            {"Content-Type": "application/x-www-form-urlencoded"},
            _form({"client_id": self.client_id, "scope": SCOPES}),
        )
        if status != 200 or not body.get("device_code"):
            raise GraphError(f"Microsoft would not start the sign-in: {_error(body, status)}")
        return DeviceCode(
            user_code=str(body.get("user_code", "")),
            verification_uri=str(body.get("verification_uri", "https://microsoft.com/devicelogin")),
            device_code=str(body["device_code"]),
            interval=float(body.get("interval", 5) or 5),
            expires_at=self._clock() + float(body.get("expires_in", 900) or 900),
            message=str(body.get("message", "")),
        )

    def poll(self, code: DeviceCode) -> str | None:
        """None while the person has not finished; the signed-in account when they have."""
        if self._clock() >= code.expires_at:
            raise GraphError("The sign-in code expired. Start again.")
        status, body = self._http(
            "POST",
            f"{self._auth}/token",
            {"Content-Type": "application/x-www-form-urlencoded"},
            _form(
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self.client_id,
                    "device_code": code.device_code,
                }
            ),
        )
        error = body.get("error")
        if error == "authorization_pending":
            return None
        if error == "slow_down":
            code.interval += 5
            return None
        if error in ("authorization_declined", "access_denied"):
            raise GraphError("The sign-in was declined.")
        if error in ("expired_token", "code_expired"):
            raise GraphError("The sign-in code expired. Start again.")
        if status != 200 or not body.get("refresh_token"):
            raise GraphError(f"The sign-in failed: {_error(body, status)}")
        self._remember(body)
        account = self._account_name(str(body["access_token"]))
        self.secrets.set(
            KEYRING_ITEM,
            json.dumps({"refresh_token": body["refresh_token"], "account": account, "client_id": self.client_id}),
        )
        return account

    def _remember(self, body: dict[str, Any]) -> None:
        token = str(body.get("access_token", ""))
        self._access = (token, self._clock() + float(body.get("expires_in", 3600) or 3600)) if token else None

    def _account_name(self, access: str) -> str:
        try:
            status, body = self._http(
                "GET", f"{self._graph}/me?$select=userPrincipalName,mail", self._bearer(access), None
            )
        except GraphError:
            return ""
        if status != 200:
            return ""
        return str(body.get("mail") or body.get("userPrincipalName") or "")

    # -- tokens and calendar calls ---------------------------------------------------------

    @staticmethod
    def _bearer(access: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access}", "Accept": "application/json", "Prefer": 'outlook.timezone="UTC"'}

    def access_token(self) -> str:
        if self._access and self._access[1] - REFRESH_MARGIN > self._clock():
            return self._access[0]
        saved = self._saved()
        if saved is None:
            raise GraphError("Microsoft 365 is not signed in.")
        status, body = self._http(
            "POST",
            f"{self._auth}/token",
            {"Content-Type": "application/x-www-form-urlencoded"},
            _form(
                {
                    "grant_type": "refresh_token",
                    "client_id": self.client_id,
                    "refresh_token": saved["refresh_token"],
                    "scope": SCOPES,
                }
            ),
        )
        if status != 200 or not body.get("access_token"):
            if body.get("error") in ("invalid_grant", "interaction_required"):
                self.disconnect()
                raise GraphError("The Microsoft 365 sign-in ended. Sign in again.")
            raise GraphError(f"Microsoft would not refresh the sign-in: {_error(body, status)}")
        self._remember(body)
        if body.get("refresh_token") and body["refresh_token"] != saved["refresh_token"]:
            self.secrets.set(KEYRING_ITEM, json.dumps({**saved, "refresh_token": body["refresh_token"]}))
        return str(body["access_token"])

    def meetings(self, start: float, end: float) -> list[Meeting]:
        """The meetings between two Unix times, from the signed-in account's calendar."""
        query = urllib.parse.urlencode(
            {
                "startDateTime": datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                "endDateTime": datetime.fromtimestamp(end, UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                "$select": "id,subject,start,end,isAllDay,isCancelled,isOnlineMeeting,onlineMeeting,onlineMeetingUrl,"
                "location,bodyPreview,responseStatus",
                "$orderby": "start/dateTime",
                "$top": "50",
            }
        )
        url: str | None = f"{self._graph}/me/calendarView?{query}"
        found: list[Meeting] = []
        for _ in range(MAX_PAGES):
            if url is None:
                break
            status, body = self._http("GET", url, self._bearer(self.access_token()), None)
            if status == 401:
                self._access = None
                raise GraphError("Microsoft 365 rejected the sign-in. Sign in again.")
            if status != 200:
                raise GraphError(f"Microsoft 365 would not list the calendar: {_error(body, status)}")
            for item in body.get("value", []):
                meeting = self._meeting(item)
                if meeting is not None:
                    found.append(meeting)
            next_link = body.get("@odata.nextLink")
            url = str(next_link) if next_link else None
        return sorted(found, key=lambda m: (m.start, m.id))

    @staticmethod
    def _meeting(item: dict[str, Any]) -> Meeting | None:
        if item.get("isAllDay") or item.get("isCancelled"):
            return None
        if (item.get("responseStatus") or {}).get("response") == "declined":
            return None
        try:
            start = _utc(item["start"]["dateTime"])
            end = _utc(item["end"]["dateTime"])
        except (KeyError, TypeError, ValueError):
            return None
        online = item.get("onlineMeeting") or {}
        join = str(online.get("joinUrl") or item.get("onlineMeetingUrl") or "")
        if not join:
            join = find_join_link((item.get("location") or {}).get("displayName", ""), item.get("bodyPreview", ""))
        if not join and item.get("isOnlineMeeting"):
            join = "online"
        return Meeting(
            f"m365:{item.get('id', '')}:{int(start)}", str(item.get("subject") or "Meeting"), start, end, join, "m365"
        )


def _utc(text: str) -> float:
    clean = re.sub(r"\.\d+", "", text).rstrip("Z")
    return datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).timestamp()
