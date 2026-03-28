# teams.py - Microsoft Teams integration for WeeChat via Microsoft Graph API
# Uses device code flow with delegated permissions for multi-tenant support
# Follows the patterns established by wee_most.py (Mattermost plugin)
# License: GPL3

import calendar
import json
import re
import time
import urllib.parse
import weechat

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_NAME = "teams"
SCRIPT_AUTHOR = "curiosity-corp"
SCRIPT_VERSION = "0.1.0"
SCRIPT_LICENSE = "GPL3"
SCRIPT_DESC = "Microsoft Teams integration via Graph API (multi-tenant)"

CLIENT_ID = "fcda3faa-1259-4b56-a04d-3281fc98d8f1"
SCOPES = (
    "Chat.ReadWrite ChannelMessage.Send Channel.ReadBasic.All "
    "Team.ReadBasic.All Group.ReadWrite.All User.Read User.ReadBasic.All "
    "offline_access openid profile"
)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
AUTH_BASE = "https://login.microsoftonline.com/organizations/oauth2/v2.0"
DEVICE_CODE_URL = AUTH_BASE + "/devicecode"
TOKEN_URL = AUTH_BASE + "/token"

REQUEST_TIMEOUT_MS = 30 * 1000
DEFAULT_POLL_INTERVAL = 10  # seconds
CHANNEL_POLL_INTERVAL = 60  # seconds
TOKEN_REFRESH_INTERVAL = 50 * 60  # seconds (refresh before 60 min expiry)
QUEUE_INTERVAL_MS = 200  # milliseconds

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

tenants = {}  # {name: Tenant}


# ---------------------------------------------------------------------------
# HTML stripping (no external deps)
# ---------------------------------------------------------------------------

def strip_html(html):
    if not html:
        return ""
    text = re.sub(r'<br\s*/?>', '\n', html)
    text = re.sub(r'<[^>]+>', '', text)
    text = (
        text.replace('&amp;', '&')
        .replace('&lt;', '<')
        .replace('&gt;', '>')
        .replace('&nbsp;', ' ')
        .replace('&#39;', "'")
        .replace('&quot;', '"')
    )
    return text.strip()


# ---------------------------------------------------------------------------
# ISO datetime -> unix timestamp
# ---------------------------------------------------------------------------

def iso_to_unix(dt_str):
    if not dt_str:
        return 0
    try:
        # Strip fractional seconds and trailing Z / timezone
        clean = re.sub(r'\.[\d]+', '', dt_str)
        clean = clean.replace('Z', '')
        # Handle +00:00 style offsets
        clean = re.sub(r'[+-]\d{2}:\d{2}$', '', clean)
        return calendar.timegm(time.strptime(clean, '%Y-%m-%dT%H:%M:%S'))
    except (ValueError, OverflowError):
        return 0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config:

    def __init__(self):
        self.file = None
        self.sections = {}
        self.options = {}

    def get_value(self, section, name):
        option = self.options.get("{}.{}".format(section, name))
        if not option:
            return ""
        if option["type"] == "boolean":
            return weechat.config_boolean(option["pointer"])
        elif option["type"] == "integer":
            return weechat.config_integer(option["pointer"])
        elif option["type"] == "string":
            return weechat.config_string(option["pointer"])
        return ""

    def get_tenant_value(self, tenant_name, name):
        value = self.get_value("tenant", "{}.{}".format(tenant_name, name))
        if not value:
            ptr = weechat.config_get("teams.tenant.{}.{}".format(tenant_name, name))
            if ptr:
                value = weechat.config_string(ptr)
        if name == "refresh_token":
            return weechat.string_eval_expression(str(value), {}, {}, {})
        return value

    def is_tenant_valid(self, tenant_name):
        if "tenant.{}.refresh_token".format(tenant_name) in self.options:
            return True
        ptr = weechat.config_get("teams.tenant.{}.refresh_token".format(tenant_name))
        return ptr != ""

    def tenant_names(self):
        names = set()
        for key in self.options:
            if key.startswith("tenant."):
                parts = key.split(".")
                if len(parts) == 3:
                    names.add(parts[1])
        return sorted(names)

    def add_tenant_options(self, tenant_name):
        key_rt = "tenant.{}.refresh_token".format(tenant_name)
        if key_rt not in self.options:
            self.options[key_rt] = {
                "pointer": weechat.config_new_option(
                    self.file, self.sections["tenant"],
                    "{}.refresh_token".format(tenant_name), "string",
                    "Refresh token for {} tenant (use sec.data for security)".format(tenant_name),
                    "", 0, 0, "", "", 0, "", "", "", "", "", ""
                ),
                "type": "string",
            }

        key_ac = "tenant.{}.autoconnect".format(tenant_name)
        if key_ac not in self.options:
            self.options[key_ac] = {
                "pointer": weechat.config_new_option(
                    self.file, self.sections["tenant"],
                    "{}.autoconnect".format(tenant_name), "string",
                    "Set to 'on' to auto-connect to {} on start".format(tenant_name),
                    "", 0, 0, "off", "off", 0, "", "", "", "", "", ""
                ),
                "type": "string",
            }

    def remove_tenant_options(self, tenant_name):
        for suffix in ("refresh_token", "autoconnect"):
            key = "tenant.{}.{}".format(tenant_name, suffix)
            opt = self.options.pop(key, None)
            if opt:
                weechat.config_option_free(opt["pointer"])

    def set_tenant_refresh_token(self, tenant_name, token):
        key = "tenant.{}.refresh_token".format(tenant_name)
        opt = self.options.get(key)
        if opt:
            weechat.config_option_set(opt["pointer"], token, 1)

    def save(self):
        if self.file:
            weechat.config_write(self.file)

    def read(self):
        self._ensure_tenants_from_conf_file()
        if self.file:
            weechat.config_read(self.file)

    def _ensure_tenants_from_conf_file(self):
        import os
        conf_path = os.path.join(
            weechat.info_get("weechat_config_dir", "") or
            weechat.info_get("weechat_dir", ""),
            "teams.conf"
        )
        if not os.path.exists(conf_path):
            return
        tenant_ids = set()
        try:
            with open(conf_path, "r") as f:
                in_tenant_section = False
                for line in f:
                    line = line.strip()
                    if line == "[tenant]":
                        in_tenant_section = True
                        continue
                    if line.startswith("[") and line.endswith("]"):
                        in_tenant_section = False
                        continue
                    if not in_tenant_section:
                        continue
                    if "=" not in line or line.startswith("#"):
                        continue
                    key = line.split("=", 1)[0].strip()
                    if "." in key:
                        tenant_id = key.split(".", 1)[0]
                        tenant_ids.add(tenant_id)
        except Exception:
            return
        for tenant_id in tenant_ids:
            if not self.is_tenant_valid(tenant_id):
                self.add_tenant_options(tenant_id)

    def setup(self):
        self.file = weechat.config_new("teams", "", "")

        # [look] section
        self.sections["look"] = weechat.config_new_section(
            self.file, "look", 0, 0, "", "", "", "", "", "", "", "", "", ""
        )
        self.options["look.poll_interval"] = {
            "pointer": weechat.config_new_option(
                self.file, self.sections["look"],
                "poll_interval", "integer",
                "Interval in seconds between chat polls",
                "", 5, 300, str(DEFAULT_POLL_INTERVAL), str(DEFAULT_POLL_INTERVAL),
                0, "", "", "", "", "", ""
            ),
            "type": "integer",
        }
        self.options["look.channel_poll_interval"] = {
            "pointer": weechat.config_new_option(
                self.file, self.sections["look"],
                "channel_poll_interval", "integer",
                "Interval in seconds between channel polls",
                "", 15, 600, str(CHANNEL_POLL_INTERVAL), str(CHANNEL_POLL_INTERVAL),
                0, "", "", "", "", "", ""
            ),
            "type": "integer",
        }
        self.options["look.max_chat_buffers"] = {
            "pointer": weechat.config_new_option(
                self.file, self.sections["look"],
                "max_chat_buffers", "integer",
                "Max number of chat buffers to open automatically (0=unlimited, most recent first)",
                "", 0, 500, "20", "20",
                0, "", "", "", "", "", ""
            ),
            "type": "integer",
        }

        # [tenant] section (user can add options dynamically)
        self.sections["tenant"] = weechat.config_new_section(
            self.file, "tenant", 1, 0, "", "",
            "", "", "", "",
            "config_create_tenant_option_cb", "", "", ""
        )


def config_create_tenant_option_cb(data, config_file, section, option_name, value):
    if not re.match(r'^[a-zA-Z0-9_-]+\.(refresh_token|autoconnect)$', option_name):
        return weechat.WEECHAT_CONFIG_OPTION_SET_ERROR

    global config

    config.options["tenant.{}".format(option_name)] = {
        "pointer": weechat.config_new_option(
            config_file, section, option_name, "string", "",
            "", 0, 0, value, value, 0, "", "", "", "", "", ""
        ),
        "type": opt_type,
    }
    return weechat.WEECHAT_CONFIG_OPTION_SET_OK_CHANGED


# ---------------------------------------------------------------------------
# EventRouter (response buffering + request queue)
# ---------------------------------------------------------------------------

class EventRouter:

    def __init__(self):
        self.enqueued_requests = []
        self.response_buffers = {}

    def enqueue_request(self, method, *params):
        self.enqueued_requests.append([method, params])

    def handle_next(self):
        if not self.enqueued_requests:
            return
        request = self.enqueued_requests.pop(0)
        func = globals().get(request[0])
        if func:
            func(*request[1])

    def buffered_response_cb(self, data, command, rc, out, err):
        # Format: "request_id::callback::cb_data"
        parts = data.split("::", 2)
        if len(parts) < 3:
            return weechat.WEECHAT_RC_ERROR

        request_id = parts[0]
        real_cb = parts[1]
        real_data = parts[2]

        # Use request_id as buffer key — unique per hook_process call
        buf_key = request_id

        if buf_key not in self.response_buffers:
            self.response_buffers[buf_key] = {"out": "", "err": ""}

        if rc == weechat.WEECHAT_HOOK_PROCESS_RUNNING:
            self.response_buffers[buf_key]["out"] += out
            self.response_buffers[buf_key]["err"] += err
            return weechat.WEECHAT_RC_OK

        response = self.response_buffers[buf_key]["out"] + out
        full_err = self.response_buffers[buf_key]["err"] + err
        del self.response_buffers[buf_key]

        cb_func = globals().get(real_cb)
        if cb_func:
            return cb_func(real_data, command, rc, response, full_err)

        return weechat.WEECHAT_RC_ERROR


_request_counter = 0

def build_buffer_cb_data(url, cb, cb_data):
    global _request_counter
    _request_counter += 1
    return "{}::{}::{}".format(_request_counter, cb, cb_data)


def handle_queued_request_cb(data, remaining_calls):
    EVENTROUTER.handle_next()
    return weechat.WEECHAT_RC_OK


# ---------------------------------------------------------------------------
# Tenant class
# ---------------------------------------------------------------------------

class Tenant:

    def __init__(self, name):
        self.name = name
        self.access_token = ""
        self.refresh_token = ""
        self.me = None  # {"id": ..., "displayName": ...}
        self.chats = {}  # {chat_id: {"topic":..., "chatType":..., "members":[], "last_message_ts":"", "last_message_id":"", "buffer": ptr}}
        self.teams = {}  # {team_id: {"displayName":..., "channels": {channel_id: {"displayName":..., "buffer": ptr, "last_message_ts":"", "last_message_id":""}}}}
        self.buffer = None  # server buffer
        self.connected = False
        self.poll_hook = None
        self.channel_poll_hook = None
        self.refresh_hook = None
        self.device_code_poll_hook = None
        self._pending_device_code = None  # used during auth

        self._create_buffer()

    def _create_buffer(self):
        buffer_name = "teams.{}*".format(self.name)
        self.buffer = weechat.buffer_new(buffer_name, "", "", "", "")
        weechat.buffer_set(self.buffer, "short_name", self.name)
        weechat.buffer_set(self.buffer, "localvar_set_tenant_name", self.name)
        weechat.buffer_set(self.buffer, "localvar_set_type", "server")
        buffer_merge(self.buffer)

    def print(self, message):
        weechat.prnt(self.buffer, message)

    def print_error(self, message):
        weechat.prnt(self.buffer, weechat.prefix("error") + message)

    def disconnect(self):
        self.connected = False
        if self.poll_hook:
            weechat.unhook(self.poll_hook)
            self.poll_hook = None
        if self.channel_poll_hook:
            weechat.unhook(self.channel_poll_hook)
            self.channel_poll_hook = None
        if self.refresh_hook:
            weechat.unhook(self.refresh_hook)
            self.refresh_hook = None
        if self.device_code_poll_hook:
            weechat.unhook(self.device_code_poll_hook)
            self.device_code_poll_hook = None
        self.access_token = ""
        self.print("Disconnected")

    def unload(self):
        self.disconnect()
        # Close all chat buffers
        for chat_info in self.chats.values():
            buf = chat_info.get("buffer")
            if buf:
                weechat.buffer_close(buf)
        # Close all channel buffers
        for team_info in self.teams.values():
            for ch_info in team_info.get("channels", {}).values():
                buf = ch_info.get("buffer")
                if buf:
                    weechat.buffer_close(buf)
        self.chats = {}
        self.teams = {}
        if self.buffer:
            weechat.buffer_close(self.buffer)
            self.buffer = None


def buffer_merge(buffer):
    """Merge server buffers together like wee_most does."""
    server_buffer_merge = weechat.config_string(
        weechat.config_get("irc.look.server_buffer")
    )
    if server_buffer_merge == "merge_with_core":
        weechat.buffer_merge(buffer, weechat.buffer_search_main())
    elif server_buffer_merge == "merge_without_core":
        # Merge with first teams server buffer found, or core
        for t in tenants.values():
            if t.buffer and t.buffer != buffer:
                weechat.buffer_merge(buffer, t.buffer)
                return
        weechat.buffer_merge(buffer, weechat.buffer_search_main())


# ---------------------------------------------------------------------------
# Graph API HTTP helpers
# ---------------------------------------------------------------------------

def graph_get(tenant_name, path, cb, cb_data):
    t = tenants.get(tenant_name)
    if not t or not t.access_token:
        return
    url = GRAPH_BASE + path
    weechat.hook_process_hashtable(
        "url:" + url,
        {
            "httpheader": "Authorization: Bearer {}\nContent-Type: application/json".format(t.access_token),
        },
        REQUEST_TIMEOUT_MS,
        "buffered_response_cb",
        build_buffer_cb_data(url, cb, cb_data),
    )


def graph_post(tenant_name, path, body_dict, cb, cb_data):
    t = tenants.get(tenant_name)
    if not t or not t.access_token:
        return
    url = GRAPH_BASE + path
    weechat.hook_process_hashtable(
        "url:" + url,
        {
            "httpheader": "Authorization: Bearer {}\nContent-Type: application/json".format(t.access_token),
            "postfields": json.dumps(body_dict),
        },
        REQUEST_TIMEOUT_MS,
        "buffered_response_cb",
        build_buffer_cb_data(url, cb, cb_data),
    )


# ---------------------------------------------------------------------------
# Device code auth flow
# ---------------------------------------------------------------------------

def start_device_code_auth(tenant_name):
    t = tenants.get(tenant_name)
    if not t:
        weechat.prnt("", weechat.prefix("error") + "teams: unknown tenant '{}'".format(tenant_name))
        return

    t.print("Starting device code authentication...")

    form_data = {
        "client_id": CLIENT_ID,
        "scope": SCOPES,
    }

    cb_data = build_buffer_cb_data(DEVICE_CODE_URL, "device_code_response_cb", tenant_name)

    weechat.hook_process_hashtable(
        "url:" + DEVICE_CODE_URL,
        {
            "httpheader": "Content-Type: application/x-www-form-urlencoded",
            "postfields": urllib.parse.urlencode(form_data),
        },
        REQUEST_TIMEOUT_MS,
        "buffered_response_cb",
        cb_data,
    )


def device_code_response_cb(tenant_name, command, rc, out, err):
    t = tenants.get(tenant_name)
    if not t:
        return weechat.WEECHAT_RC_ERROR

    if rc != 0:
        t.print_error("Failed to start device code flow (HTTP error {})".format(rc))
        return weechat.WEECHAT_RC_ERROR

    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError) as e:
        t.print_error("Failed to parse device code response: {}".format(str(e)))
        return weechat.WEECHAT_RC_ERROR

    if "error" in data:
        t.print_error("Device code error: {} - {}".format(data.get("error", ""), data.get("error_description", "")))
        return weechat.WEECHAT_RC_ERROR

    message = data.get("message", "")
    device_code = data.get("device_code", "")
    interval = data.get("interval", 5)

    if not device_code:
        t.print_error("No device_code in response")
        return weechat.WEECHAT_RC_ERROR

    t._pending_device_code = device_code
    t.print(message)

    # Start polling for token
    interval_ms = int(interval) * 1000
    if interval_ms < 1000:
        interval_ms = 5000

    t.device_code_poll_hook = weechat.hook_timer(
        interval_ms, 0, 0, "device_code_poll_timer_cb", tenant_name
    )

    return weechat.WEECHAT_RC_OK


def device_code_poll_timer_cb(tenant_name, remaining_calls):
    t = tenants.get(tenant_name)
    if not t or not t._pending_device_code:
        return weechat.WEECHAT_RC_OK

    form_data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": CLIENT_ID,
        "device_code": t._pending_device_code,
    }

    cb_data = build_buffer_cb_data(TOKEN_URL, "device_code_token_cb", tenant_name)

    weechat.hook_process_hashtable(
        "url:" + TOKEN_URL,
        {
            "httpheader": "Content-Type: application/x-www-form-urlencoded",
            "postfields": urllib.parse.urlencode(form_data),
        },
        REQUEST_TIMEOUT_MS,
        "buffered_response_cb",
        cb_data,
    )

    return weechat.WEECHAT_RC_OK


def device_code_token_cb(tenant_name, command, rc, out, err):
    t = tenants.get(tenant_name)
    if not t:
        return weechat.WEECHAT_RC_ERROR

    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        # Might be a transient parse error from chunked response; keep polling
        return weechat.WEECHAT_RC_OK

    error = data.get("error", "")

    if error == "authorization_pending":
        # Still waiting for user to authenticate - keep polling
        return weechat.WEECHAT_RC_OK

    if error == "slow_down":
        # Server wants us to slow down - keep polling (timer interval is fine)
        return weechat.WEECHAT_RC_OK

    if error in ("expired_token", "authorization_declined", "bad_verification_code"):
        t.print_error("Authentication failed: {} - {}".format(error, data.get("error_description", "")))
        if t.device_code_poll_hook:
            weechat.unhook(t.device_code_poll_hook)
            t.device_code_poll_hook = None
        t._pending_device_code = None
        return weechat.WEECHAT_RC_ERROR

    if error:
        t.print_error("Token error: {} - {}".format(error, data.get("error_description", "")))
        if t.device_code_poll_hook:
            weechat.unhook(t.device_code_poll_hook)
            t.device_code_poll_hook = None
        t._pending_device_code = None
        return weechat.WEECHAT_RC_ERROR

    # Success
    access_token = data.get("access_token", "")
    refresh_token = data.get("refresh_token", "")

    if not access_token:
        t.print_error("No access_token in token response")
        return weechat.WEECHAT_RC_ERROR

    t.access_token = access_token
    t.refresh_token = refresh_token
    t._pending_device_code = None

    if t.device_code_poll_hook:
        weechat.unhook(t.device_code_poll_hook)
        t.device_code_poll_hook = None

    # Save refresh token to config
    config.set_tenant_refresh_token(t.name, refresh_token)
    config.save()

    t.print("Authentication successful!")

    # Fetch user profile and start connection
    _start_connection(t.name)

    return weechat.WEECHAT_RC_OK


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def refresh_token_for_tenant(tenant_name):
    t = tenants.get(tenant_name)
    if not t or not t.refresh_token:
        return

    form_data = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": t.refresh_token,
        "scope": SCOPES,
    }

    cb_data = build_buffer_cb_data(TOKEN_URL, "refresh_token_cb", tenant_name)

    weechat.hook_process_hashtable(
        "url:" + TOKEN_URL,
        {
            "httpheader": "Content-Type: application/x-www-form-urlencoded",
            "postfields": urllib.parse.urlencode(form_data),
        },
        REQUEST_TIMEOUT_MS,
        "buffered_response_cb",
        cb_data,
    )


def refresh_token_cb(tenant_name, command, rc, out, err):
    t = tenants.get(tenant_name)
    if not t:
        return weechat.WEECHAT_RC_ERROR

    if rc != 0:
        t.print_error("Token refresh HTTP error (rc={})".format(rc))
        return weechat.WEECHAT_RC_ERROR

    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError) as e:
        t.print_error("Token refresh parse error: {}".format(str(e)))
        return weechat.WEECHAT_RC_ERROR

    if "error" in data:
        t.print_error("Token refresh failed: {} - {}".format(
            data.get("error", ""), data.get("error_description", "")
        ))
        # If refresh fails, disconnect
        t.print_error("Disconnecting due to auth failure. Use /teams auth {} to re-authenticate.".format(t.name))
        t.disconnect()
        return weechat.WEECHAT_RC_ERROR

    t.access_token = data.get("access_token", "")
    new_refresh = data.get("refresh_token", "")
    if new_refresh:
        t.refresh_token = new_refresh
        config.set_tenant_refresh_token(t.name, new_refresh)
        config.save()

    # If we just refreshed during initial connect (not yet connected), start connection
    if not t.connected and t.access_token:
        _start_connection(t.name)

    return weechat.WEECHAT_RC_OK


def refresh_token_timer_cb(tenant_name, remaining_calls):
    refresh_token_for_tenant(tenant_name)
    return weechat.WEECHAT_RC_OK


# ---------------------------------------------------------------------------
# Connection flow
# ---------------------------------------------------------------------------

def _start_connection(tenant_name):
    """After auth is complete (token acquired), fetch /me and start polling."""
    graph_get(tenant_name, "/me", "connect_me_cb", tenant_name)


def connect_me_cb(tenant_name, command, rc, out, err):
    t = tenants.get(tenant_name)
    if not t:
        return weechat.WEECHAT_RC_ERROR

    if rc != 0:
        t.print_error("Failed to fetch user profile (rc={})".format(rc))
        return weechat.WEECHAT_RC_ERROR

    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError) as e:
        t.print_error("Failed to parse /me response: {}".format(str(e)))
        return weechat.WEECHAT_RC_ERROR

    if "error" in data:
        t.print_error("/me error: {}".format(data.get("error", {}).get("message", str(data))))
        return weechat.WEECHAT_RC_ERROR

    t.me = {
        "id": data.get("id", ""),
        "displayName": data.get("displayName", ""),
        "mail": data.get("mail", ""),
        "userPrincipalName": data.get("userPrincipalName", ""),
    }

    weechat.buffer_set(t.buffer, "localvar_set_nick", t.me["displayName"])
    t.connected = True
    t.print("Connected as {}".format(t.me["displayName"]))

    # Start polling timers
    poll_interval = config.get_value("look", "poll_interval")
    if not poll_interval or poll_interval < 5:
        poll_interval = DEFAULT_POLL_INTERVAL

    channel_poll_interval = config.get_value("look", "channel_poll_interval")
    if not channel_poll_interval or channel_poll_interval < 15:
        channel_poll_interval = CHANNEL_POLL_INTERVAL

    t.poll_hook = weechat.hook_timer(
        poll_interval * 1000, 0, 0, "chat_poll_timer_cb", tenant_name
    )
    t.channel_poll_hook = weechat.hook_timer(
        channel_poll_interval * 1000, 0, 0, "channel_poll_timer_cb", tenant_name
    )
    t.refresh_hook = weechat.hook_timer(
        TOKEN_REFRESH_INTERVAL * 1000, 0, 0, "refresh_token_timer_cb", tenant_name
    )

    # Do initial polls immediately
    EVENTROUTER.enqueue_request("poll_chats", tenant_name)
    EVENTROUTER.enqueue_request("poll_teams_and_channels", tenant_name)

    return weechat.WEECHAT_RC_OK


def connect_tenant(tenant_name):
    """Connect to a tenant using stored refresh_token, or print error."""
    if tenant_name in tenants:
        t = tenants[tenant_name]
        if t.connected:
            t.print_error("Already connected")
            return weechat.WEECHAT_RC_ERROR
        # Already have a tenant object but not connected; try to reuse
        t.unload()
        del tenants[tenant_name]

    if not config.is_tenant_valid(tenant_name):
        weechat.prnt("", weechat.prefix("error") + "teams: tenant '{}' not configured. Use /teams tenant add {}".format(
            tenant_name, tenant_name))
        return weechat.WEECHAT_RC_ERROR

    t = Tenant(tenant_name)
    tenants[tenant_name] = t

    stored_refresh = config.get_tenant_value(tenant_name, "refresh_token")
    if not stored_refresh:
        t.print("No refresh token stored. Use /teams auth {} to authenticate.".format(tenant_name))
        return weechat.WEECHAT_RC_OK

    t.refresh_token = stored_refresh
    t.print("Connecting to {}...".format(tenant_name))

    # Refresh the token to get an access_token.
    # refresh_token_cb will call _start_connection automatically on success.
    refresh_token_for_tenant(tenant_name)

    return weechat.WEECHAT_RC_OK


def disconnect_tenant(tenant_name):
    t = tenants.get(tenant_name)
    if not t:
        weechat.prnt("", weechat.prefix("error") + "teams: tenant '{}' not found".format(tenant_name))
        return weechat.WEECHAT_RC_ERROR

    if not t.connected:
        t.print_error("Not connected")
        return weechat.WEECHAT_RC_ERROR

    t.disconnect()
    return weechat.WEECHAT_RC_OK


# ---------------------------------------------------------------------------
# Chat polling
# ---------------------------------------------------------------------------

def chat_poll_timer_cb(tenant_name, remaining_calls):
    t = tenants.get(tenant_name)
    if not t or not t.connected:
        return weechat.WEECHAT_RC_OK

    EVENTROUTER.enqueue_request("poll_chats", tenant_name)
    return weechat.WEECHAT_RC_OK


def poll_chats(tenant_name):
    path = "/me/chats?%24expand=lastMessagePreview&%24orderby=lastMessagePreview/createdDateTime%20desc&%24top=50"
    graph_get(tenant_name, path, "poll_chats_cb", tenant_name)


def poll_chats_cb(tenant_name, command, rc, out, err):
    t = tenants.get(tenant_name)
    if not t:
        return weechat.WEECHAT_RC_ERROR

    if rc != 0:
        t.print_error("Chat poll failed (rc={}) err={} out={}".format(rc, err[:200] if err else "", out[:200] if out else ""))
        return weechat.WEECHAT_RC_ERROR

    if not out or not out.strip():
        # Empty response — skip silently (transient network issue)
        return weechat.WEECHAT_RC_OK

    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError) as e:
        # Log truncated response for debugging, but don't spam on every poll
        t.print_error("Chat poll parse error: {} — len={} first100={}".format(str(e), len(out), repr(out[:100])))
        return weechat.WEECHAT_RC_ERROR

    if "error" in data:
        t.print_error("Chat poll API error: {}".format(data.get("error", {}).get("message", str(data))))
        return weechat.WEECHAT_RC_ERROR

    max_buffers = config.get_value("look", "max_chat_buffers") or 20
    chats = data.get("value", [])
    open_buffer_count = sum(1 for c in t.chats.values() if c.get("buffer"))

    for idx, chat in enumerate(chats):
        chat_id = chat.get("id", "")
        if not chat_id:
            continue

        chat_type = chat.get("chatType", "")
        topic = chat.get("topic", "") or ""
        last_preview = chat.get("lastMessagePreview")
        last_ts = ""
        if last_preview:
            last_ts = last_preview.get("createdDateTime", "")

        existing = t.chats.get(chat_id)
        if existing:
            old_ts = existing.get("last_message_ts", "")
            if last_ts and last_ts != old_ts:
                # Chat has new messages - fetch them
                existing["last_message_ts"] = last_ts
                existing["topic"] = topic
                existing["chatType"] = chat_type
                # Auto-open buffer if within limit and not already open
                if not existing.get("buffer") and (max_buffers == 0 or open_buffer_count < max_buffers):
                    existing["_open_buffer"] = True
                    EVENTROUTER.enqueue_request("fetch_chat_members", tenant_name, chat_id)
                    open_buffer_count += 1
                EVENTROUTER.enqueue_request("fetch_chat_messages", tenant_name, chat_id)
        else:
            # Track the chat but only open buffer if within limit
            t.chats[chat_id] = {
                "topic": topic,
                "chatType": chat_type,
                "members": [],
                "last_message_ts": last_ts,
                "last_message_id": "",
                "buffer": None,
                "display_name": "",
            }
            # Flag for buffer creation if within limit
            should_open = last_ts and (max_buffers == 0 or open_buffer_count < max_buffers)
            if should_open:
                t.chats[chat_id]["_open_buffer"] = True
                open_buffer_count += 1
            # Fetch members (for display name); buffer created only if flagged
            EVENTROUTER.enqueue_request("fetch_chat_members", tenant_name, chat_id)
            if should_open and last_ts:
                EVENTROUTER.enqueue_request("fetch_chat_messages", tenant_name, chat_id)

    return weechat.WEECHAT_RC_OK


# ---------------------------------------------------------------------------
# Chat members
# ---------------------------------------------------------------------------

def fetch_chat_members(tenant_name, chat_id):
    path = "/me/chats/{}/members".format(chat_id)
    cb_data = "{}|{}".format(tenant_name, chat_id)
    graph_get(tenant_name, path, "fetch_chat_members_cb", cb_data)


def fetch_chat_members_cb(cb_data, command, rc, out, err):
    parts = cb_data.split("|", 1)
    if len(parts) < 2:
        return weechat.WEECHAT_RC_ERROR
    tenant_name, chat_id = parts[0], parts[1]
    t = tenants.get(tenant_name)
    if not t:
        return weechat.WEECHAT_RC_ERROR

    if rc != 0:
        return weechat.WEECHAT_RC_ERROR

    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return weechat.WEECHAT_RC_ERROR

    if "error" in data:
        return weechat.WEECHAT_RC_ERROR

    members = []
    for member in data.get("value", []):
        display_name = member.get("displayName", "")
        user_id = member.get("userId", "")
        members.append({"displayName": display_name, "userId": user_id})

    chat_info = t.chats.get(chat_id)
    if not chat_info:
        return weechat.WEECHAT_RC_OK

    chat_info["members"] = members

    # Compute display name
    chat_info["display_name"] = _compute_chat_display_name(t, chat_info)

    # Only create/update buffer if one already exists or messages are queued
    # (buffer creation is now controlled by poll_chats_cb's max_chat_buffers logic
    # or explicit /teams chat open command)
    if chat_info.get("buffer") or chat_info.get("_open_buffer"):
        chat_info.pop("_open_buffer", None)
        _ensure_chat_buffer(t, chat_id)

    return weechat.WEECHAT_RC_OK


def _compute_chat_display_name(tenant, chat_info):
    chat_type = chat_info.get("chatType", "")
    topic = chat_info.get("topic", "")
    members = chat_info.get("members", [])

    if chat_type == "oneOnOne":
        # Use the OTHER member's display name
        for m in members:
            if tenant.me and m.get("userId") != tenant.me.get("id"):
                return m.get("displayName", "Unknown")
        return "DM"
    elif chat_type == "group":
        if topic:
            return topic
        # Join member names (excluding self)
        names = []
        for m in members:
            if tenant.me and m.get("userId") != tenant.me.get("id"):
                name = m.get("displayName", "")
                if name:
                    names.append(name)
        return ", ".join(names) if names else "Group Chat"
    elif chat_type == "meeting":
        return topic if topic else "Meeting"
    else:
        return topic if topic else "Chat"


def _ensure_chat_buffer(tenant, chat_id):
    chat_info = tenant.chats.get(chat_id)
    if not chat_info:
        return None

    if chat_info.get("buffer"):
        # Update short_name in case display name changed
        display_name = chat_info.get("display_name", chat_id)
        weechat.buffer_set(chat_info["buffer"], "short_name", display_name)
        return chat_info["buffer"]

    display_name = chat_info.get("display_name", chat_id)
    buffer_name = "teams.{}.{}".format(tenant.name, display_name)

    buf = weechat.buffer_new(buffer_name, "buffer_input_cb", "", "buffer_close_cb", "")
    weechat.buffer_set(buf, "short_name", display_name)
    weechat.buffer_set(buf, "localvar_set_tenant_name", tenant.name)
    weechat.buffer_set(buf, "localvar_set_chat_id", chat_id)
    weechat.buffer_set(buf, "localvar_set_type", "chat")
    if tenant.me:
        weechat.buffer_set(buf, "localvar_set_nick", tenant.me.get("displayName", ""))
    weechat.buffer_set(buf, "nicklist", "1")

    chat_info["buffer"] = buf

    # Add members to nicklist
    for m in chat_info.get("members", []):
        name = m.get("displayName", "")
        if name:
            weechat.nicklist_add_nick(buf, "", name, "", "", "", 1)

    return buf


# ---------------------------------------------------------------------------
# Chat messages
# ---------------------------------------------------------------------------

def fetch_chat_messages(tenant_name, chat_id):
    path = "/me/chats/{}/messages?%24top=20&%24orderby=createdDateTime%20desc".format(chat_id)
    cb_data = "{}|{}".format(tenant_name, chat_id)
    graph_get(tenant_name, path, "fetch_chat_messages_cb", cb_data)


def fetch_chat_messages_cb(cb_data, command, rc, out, err):
    parts = cb_data.split("|", 1)
    if len(parts) < 2:
        return weechat.WEECHAT_RC_ERROR
    tenant_name, chat_id = parts[0], parts[1]
    t = tenants.get(tenant_name)
    if not t:
        return weechat.WEECHAT_RC_ERROR

    if rc != 0:
        return weechat.WEECHAT_RC_ERROR

    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return weechat.WEECHAT_RC_ERROR

    if "error" in data:
        return weechat.WEECHAT_RC_ERROR

    chat_info = t.chats.get(chat_id)
    if not chat_info:
        return weechat.WEECHAT_RC_OK

    # Ensure buffer exists (members might have been fetched already)
    buf = chat_info.get("buffer")
    if not buf:
        buf = _ensure_chat_buffer(t, chat_id)
    if not buf:
        return weechat.WEECHAT_RC_OK

    messages = data.get("value", [])
    last_known_id = chat_info.get("last_message_id", "")

    # Filter to only user messages, in chronological order (oldest first)
    user_messages = [m for m in reversed(messages) if m.get("messageType") == "message"]

    if last_known_id:
        # Find the position of the last known message and only show those after it
        found_idx = -1
        for i, msg in enumerate(user_messages):
            if msg.get("id") == last_known_id:
                found_idx = i
                break

        if found_idx >= 0:
            # Display only messages after the last known one
            messages_to_display = user_messages[found_idx + 1:]
        else:
            # Last known ID not in this batch (messages scrolled past).
            # Avoid re-displaying the whole batch -- show nothing to prevent dupes.
            # The last_message_id will be updated below so next poll picks up.
            messages_to_display = []
    else:
        # First time polling this chat -- display all messages
        messages_to_display = user_messages

    for msg in messages_to_display:
        _render_message(buf, msg)

    # Update last message ID to the newest message in the batch
    if user_messages:
        chat_info["last_message_id"] = user_messages[-1].get("id", "")

    return weechat.WEECHAT_RC_OK


# ---------------------------------------------------------------------------
# Teams & channels polling
# ---------------------------------------------------------------------------

def channel_poll_timer_cb(tenant_name, remaining_calls):
    t = tenants.get(tenant_name)
    if not t or not t.connected:
        return weechat.WEECHAT_RC_OK

    EVENTROUTER.enqueue_request("poll_teams_and_channels", tenant_name)
    return weechat.WEECHAT_RC_OK


def poll_teams_and_channels(tenant_name):
    graph_get(tenant_name, "/me/joinedTeams", "poll_teams_cb", tenant_name)


def poll_teams_cb(tenant_name, command, rc, out, err):
    t = tenants.get(tenant_name)
    if not t:
        return weechat.WEECHAT_RC_ERROR

    if rc != 0:
        t.print_error("Teams poll failed (rc={}) err={} out={}".format(rc, err[:200] if err else "", out[:200] if out else ""))
        return weechat.WEECHAT_RC_ERROR

    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError) as e:
        t.print_error("Teams poll parse error: {}".format(str(e)))
        return weechat.WEECHAT_RC_ERROR

    if "error" in data:
        t.print_error("Teams poll API error: {}".format(data.get("error", {}).get("message", str(data))))
        return weechat.WEECHAT_RC_ERROR

    teams_list = data.get("value", [])
    for team in teams_list:
        team_id = team.get("id", "")
        display_name = team.get("displayName", "")
        if not team_id:
            continue

        if team_id not in t.teams:
            t.teams[team_id] = {
                "displayName": display_name,
                "channels": {},
            }
        else:
            t.teams[team_id]["displayName"] = display_name

        # Fetch channels for this team
        EVENTROUTER.enqueue_request("fetch_team_channels", tenant_name, team_id)

    return weechat.WEECHAT_RC_OK


def fetch_team_channels(tenant_name, team_id):
    path = "/teams/{}/channels".format(team_id)
    cb_data = "{}|{}".format(tenant_name, team_id)
    graph_get(tenant_name, path, "fetch_team_channels_cb", cb_data)


def fetch_team_channels_cb(cb_data, command, rc, out, err):
    parts = cb_data.split("|", 1)
    if len(parts) < 2:
        return weechat.WEECHAT_RC_ERROR
    tenant_name, team_id = parts[0], parts[1]
    t = tenants.get(tenant_name)
    if not t:
        return weechat.WEECHAT_RC_ERROR

    if rc != 0:
        return weechat.WEECHAT_RC_ERROR

    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return weechat.WEECHAT_RC_ERROR

    if "error" in data:
        return weechat.WEECHAT_RC_ERROR

    team_info = t.teams.get(team_id)
    if not team_info:
        return weechat.WEECHAT_RC_OK

    team_display_name = team_info.get("displayName", team_id)
    channels = data.get("value", [])

    for ch in channels:
        channel_id = ch.get("id", "")
        ch_display_name = ch.get("displayName", "")
        if not channel_id:
            continue

        if channel_id not in team_info["channels"]:
            team_info["channels"][channel_id] = {
                "displayName": ch_display_name,
                "buffer": None,
                "last_message_ts": "",
                "last_message_id": "",
            }
        else:
            team_info["channels"][channel_id]["displayName"] = ch_display_name

        # Only poll messages for channels that already have an open buffer
        # (channels are opt-in via /teams channel open)
        if team_info["channels"][channel_id].get("buffer"):
            EVENTROUTER.enqueue_request("fetch_channel_messages", tenant_name, team_id, channel_id)

    return weechat.WEECHAT_RC_OK


def _ensure_channel_buffer(tenant, team_id, channel_id):
    team_info = tenant.teams.get(team_id)
    if not team_info:
        return None

    ch_info = team_info["channels"].get(channel_id)
    if not ch_info:
        return None

    if ch_info.get("buffer"):
        return ch_info["buffer"]

    team_name = team_info.get("displayName", team_id)
    ch_name = ch_info.get("displayName", channel_id)
    buffer_name = "teams.{}.#{}.{}".format(tenant.name, team_name, ch_name)
    short_name = "#{}.{}".format(team_name, ch_name)

    buf = weechat.buffer_new(buffer_name, "buffer_input_cb", "", "buffer_close_cb", "")
    weechat.buffer_set(buf, "short_name", short_name)
    weechat.buffer_set(buf, "localvar_set_tenant_name", tenant.name)
    weechat.buffer_set(buf, "localvar_set_team_id", team_id)
    weechat.buffer_set(buf, "localvar_set_channel_id", channel_id)
    weechat.buffer_set(buf, "localvar_set_type", "channel")
    if tenant.me:
        weechat.buffer_set(buf, "localvar_set_nick", tenant.me.get("displayName", ""))
    weechat.buffer_set(buf, "nicklist", "1")

    ch_info["buffer"] = buf
    return buf


# ---------------------------------------------------------------------------
# Channel messages
# ---------------------------------------------------------------------------

def fetch_channel_messages(tenant_name, team_id, channel_id):
    path = "/teams/{}/channels/{}/messages?%24top=20".format(team_id, channel_id)
    cb_data = "{}|{}|{}".format(tenant_name, team_id, channel_id)
    graph_get(tenant_name, path, "fetch_channel_messages_cb", cb_data)


def fetch_channel_messages_cb(cb_data, command, rc, out, err):
    parts = cb_data.split("|", 2)
    if len(parts) < 3:
        return weechat.WEECHAT_RC_ERROR
    tenant_name, team_id, channel_id = parts[0], parts[1], parts[2]
    t = tenants.get(tenant_name)
    if not t:
        return weechat.WEECHAT_RC_ERROR

    if rc != 0:
        return weechat.WEECHAT_RC_ERROR

    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return weechat.WEECHAT_RC_ERROR

    if "error" in data:
        return weechat.WEECHAT_RC_ERROR

    team_info = t.teams.get(team_id)
    if not team_info:
        return weechat.WEECHAT_RC_OK

    ch_info = team_info["channels"].get(channel_id)
    if not ch_info:
        return weechat.WEECHAT_RC_OK

    buf = ch_info.get("buffer")
    if not buf:
        buf = _ensure_channel_buffer(t, team_id, channel_id)
    if not buf:
        return weechat.WEECHAT_RC_OK

    messages = data.get("value", [])
    last_known_id = ch_info.get("last_message_id", "")

    # Filter to user messages in chronological order (oldest first)
    user_messages = [m for m in reversed(messages) if m.get("messageType") == "message"]

    if last_known_id:
        found_idx = -1
        for i, msg in enumerate(user_messages):
            if msg.get("id") == last_known_id:
                found_idx = i
                break

        if found_idx >= 0:
            messages_to_display = user_messages[found_idx + 1:]
        else:
            # Last known ID scrolled out of window -- skip to avoid dupes
            messages_to_display = []
    else:
        messages_to_display = user_messages

    for msg in messages_to_display:
        _render_message(buf, msg)

    # Update last message ID to newest in batch
    if user_messages:
        ch_info["last_message_id"] = user_messages[-1].get("id", "")

    return weechat.WEECHAT_RC_OK


# ---------------------------------------------------------------------------
# Message rendering
# ---------------------------------------------------------------------------

def _render_message(buf, msg):
    """Render a single Graph message to a weechat buffer."""
    body = msg.get("body", {})
    content = body.get("content", "")
    content_type = body.get("contentType", "text")

    if content_type == "html":
        text = strip_html(content)
    else:
        text = content

    if not text:
        return

    # Sender
    sender_info = msg.get("from")
    sender_name = ""
    if sender_info:
        user_info = sender_info.get("user")
        if user_info:
            sender_name = user_info.get("displayName", "")
        if not sender_name:
            app_info = sender_info.get("application")
            if app_info:
                sender_name = app_info.get("displayName", "Bot")
    if not sender_name:
        sender_name = "Unknown"

    # Timestamp
    created_dt = msg.get("createdDateTime", "")
    ts = iso_to_unix(created_dt)

    # Tags for notification
    tags = "notify_message,nick_{}".format(sender_name.replace(" ", "_"))

    # Print each line
    for line in text.split('\n'):
        if line.strip():
            weechat.prnt_date_tags(buf, ts, tags, "{}\t{}".format(sender_name, line))


# ---------------------------------------------------------------------------
# Sending messages
# ---------------------------------------------------------------------------

def buffer_input_cb(data, buffer, input_data):
    tenant_name = weechat.buffer_get_string(buffer, "localvar_tenant_name")
    chat_id = weechat.buffer_get_string(buffer, "localvar_chat_id")
    team_id = weechat.buffer_get_string(buffer, "localvar_team_id")
    channel_id = weechat.buffer_get_string(buffer, "localvar_channel_id")

    t = tenants.get(tenant_name)
    if not t or not t.connected:
        weechat.prnt(buffer, weechat.prefix("error") + "Not connected to tenant '{}'".format(tenant_name))
        return weechat.WEECHAT_RC_OK

    body = {"body": {"content": input_data}}

    if chat_id:
        path = "/me/chats/{}/messages".format(chat_id)
        cb_data = "{}|{}".format(tenant_name, chat_id)
        graph_post(tenant_name, path, body, "send_message_cb", cb_data)
    elif team_id and channel_id:
        path = "/teams/{}/channels/{}/messages".format(team_id, channel_id)
        cb_data = "{}|{}|{}".format(tenant_name, team_id, channel_id)
        graph_post(tenant_name, path, body, "send_message_cb", cb_data)
    else:
        weechat.prnt(buffer, weechat.prefix("error") + "Cannot determine where to send message (no chat_id or channel_id)")

    return weechat.WEECHAT_RC_OK


def send_message_cb(cb_data, command, rc, out, err):
    # Parse tenant name from cb_data (first field)
    parts = cb_data.split("|", 1)
    tenant_name = parts[0] if parts else ""
    t = tenants.get(tenant_name)

    if rc != 0:
        if t:
            t.print_error("Failed to send message (rc={})".format(rc))
        return weechat.WEECHAT_RC_ERROR

    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return weechat.WEECHAT_RC_OK

    if "error" in data:
        if t:
            t.print_error("Send message error: {}".format(data.get("error", {}).get("message", str(data))))
        return weechat.WEECHAT_RC_ERROR

    return weechat.WEECHAT_RC_OK


def buffer_close_cb(data, buffer):
    """Called when a chat/channel buffer is closed by the user."""
    tenant_name = weechat.buffer_get_string(buffer, "localvar_tenant_name")
    chat_id = weechat.buffer_get_string(buffer, "localvar_chat_id")
    team_id = weechat.buffer_get_string(buffer, "localvar_team_id")
    channel_id = weechat.buffer_get_string(buffer, "localvar_channel_id")

    t = tenants.get(tenant_name)
    if not t:
        return weechat.WEECHAT_RC_OK

    if chat_id and chat_id in t.chats:
        t.chats[chat_id]["buffer"] = None
    elif team_id and channel_id:
        team_info = t.teams.get(team_id)
        if team_info:
            ch_info = team_info["channels"].get(channel_id)
            if ch_info:
                ch_info["buffer"] = None

    return weechat.WEECHAT_RC_OK


# ---------------------------------------------------------------------------
# /teams command
# ---------------------------------------------------------------------------

def teams_command_cb(data, buffer, command):
    if not command.strip():
        write_command_error("", "Missing subcommand. Use: tenant, auth, connect, disconnect")
        return weechat.WEECHAT_RC_ERROR

    parts = command.strip().split(None, 1)
    subcmd = parts[0]
    args = parts[1] if len(parts) > 1 else ""

    if subcmd == "tenant":
        return command_tenant(args, buffer)
    elif subcmd == "auth":
        return command_auth(args, buffer)
    elif subcmd == "connect":
        return command_connect(args, buffer)
    elif subcmd == "disconnect":
        return command_disconnect(args, buffer)
    elif subcmd == "chat":
        return command_chat(args, buffer)
    elif subcmd == "channel":
        return command_channel(args, buffer)
    else:
        write_command_error(command, "Unknown subcommand '{}'".format(subcmd))
        return weechat.WEECHAT_RC_ERROR


def command_tenant(args, buffer):
    parts = args.strip().split(None, 1)
    if not parts:
        write_command_error("tenant", "Missing tenant action. Use: add, del, list")
        return weechat.WEECHAT_RC_ERROR

    action = parts[0]
    name = parts[1].strip() if len(parts) > 1 else ""

    if action == "add":
        if not name:
            write_command_error("tenant add", "Missing tenant name")
            return weechat.WEECHAT_RC_ERROR
        if not re.match(r'^[a-zA-Z0-9_-]+$', name):
            write_command_error("tenant add", "Tenant name must be alphanumeric (with - and _)")
            return weechat.WEECHAT_RC_ERROR
        config.add_tenant_options(name)
        config.save()
        weechat.prnt("", "teams: tenant '{}' added. Use /teams auth {} to authenticate.".format(name, name))
        return weechat.WEECHAT_RC_OK

    elif action == "del":
        if not name:
            write_command_error("tenant del", "Missing tenant name")
            return weechat.WEECHAT_RC_ERROR
        # Disconnect first if connected
        if name in tenants:
            tenants[name].unload()
            del tenants[name]
        config.remove_tenant_options(name)
        config.save()
        weechat.prnt("", "teams: tenant '{}' removed.".format(name))
        return weechat.WEECHAT_RC_OK

    elif action == "list":
        names = config.tenant_names()
        if not names:
            weechat.prnt("", "teams: no tenants configured. Use /teams tenant add <name>")
        else:
            weechat.prnt("", "teams: configured tenants:")
            for n in names:
                status = ""
                t = tenants.get(n)
                if t and t.connected:
                    status = " (connected as {})".format(t.me.get("displayName", "?") if t.me else "?")
                elif t:
                    status = " (not connected)"
                autoconnect = config.get_tenant_value(n, "autoconnect")
                ac_str = " [autoconnect]" if autoconnect == "on" else ""
                weechat.prnt("", "  {}{}{}".format(n, status, ac_str))
        return weechat.WEECHAT_RC_OK

    else:
        write_command_error("tenant " + action, "Unknown tenant action. Use: add, del, list")
        return weechat.WEECHAT_RC_ERROR


def command_auth(args, buffer):
    tenant_name = args.strip()
    if not tenant_name:
        write_command_error("auth", "Missing tenant name")
        return weechat.WEECHAT_RC_ERROR

    if not config.is_tenant_valid(tenant_name):
        # Auto-add tenant config if not exists
        config.add_tenant_options(tenant_name)
        config.save()

    # Ensure we have a tenant object
    if tenant_name not in tenants:
        t = Tenant(tenant_name)
        tenants[tenant_name] = t

    start_device_code_auth(tenant_name)
    return weechat.WEECHAT_RC_OK


def command_connect(args, buffer):
    tenant_name = args.strip()

    if not tenant_name:
        # Connect all autoconnect tenants
        names = config.tenant_names()
        connected_any = False
        for n in names:
            autoconnect = config.get_tenant_value(n, "autoconnect")
            if autoconnect == "on":
                connect_tenant(n)
                connected_any = True
        if not connected_any:
            weechat.prnt("", "teams: no autoconnect tenants configured. Use /teams connect <tenant>")
        return weechat.WEECHAT_RC_OK

    return connect_tenant(tenant_name)


def command_disconnect(args, buffer):
    tenant_name = args.strip()
    if not tenant_name:
        write_command_error("disconnect", "Missing tenant name")
        return weechat.WEECHAT_RC_ERROR

    return disconnect_tenant(tenant_name)


def write_command_error(args, message):
    weechat.prnt("", weechat.prefix("error") + message + ' "/teams ' + args + '" (help on command: /help teams)')


def command_chat(args, buffer):
    parts = args.strip().split(None, 1)
    if not parts:
        write_command_error("chat", "Missing action. Use: list [tenant], open <tenant> <search>")
        return weechat.WEECHAT_RC_ERROR

    action = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""

    if action == "list":
        # List all known chats across all tenants, or for a specific tenant
        target_tenant = rest if rest else None
        found = False
        for tname, t in tenants.items():
            if target_tenant and tname != target_tenant:
                continue
            if not t.connected:
                continue
            found = True
            chat_list = sorted(
                t.chats.items(),
                key=lambda x: x[1].get("last_message_ts", ""),
                reverse=True
            )
            weechat.prnt("", "")
            weechat.prnt("", "{}teams: chats for tenant '{}'{}".format(
                weechat.color("chat_server"), tname, weechat.color("reset")
            ))
            for chat_id, chat_info in chat_list:
                name = chat_info.get("display_name", "") or chat_id[:20]
                ctype = chat_info.get("chatType", "?")
                has_buf = "*" if chat_info.get("buffer") else " "
                ts = chat_info.get("last_message_ts", "")[:16].replace("T", " ")
                weechat.prnt("", "  [{}] ({}) {} {}".format(has_buf, ctype, name, ts))
            weechat.prnt("", "  (* = buffer open)  Total: {}".format(len(chat_list)))
        if not found:
            weechat.prnt("", "teams: no connected tenants" + (" matching '{}'".format(target_tenant) if target_tenant else ""))
        return weechat.WEECHAT_RC_OK

    elif action == "open":
        # Open a specific chat by fuzzy matching display name
        open_parts = rest.split(None, 1)
        if len(open_parts) < 2:
            write_command_error("chat open", "Usage: /teams chat open <tenant> <search>")
            return weechat.WEECHAT_RC_ERROR

        tenant_name = open_parts[0]
        search = open_parts[1].lower()

        t = tenants.get(tenant_name)
        if not t or not t.connected:
            write_command_error("chat open", "Tenant '{}' not connected".format(tenant_name))
            return weechat.WEECHAT_RC_ERROR

        matches = []
        for chat_id, chat_info in t.chats.items():
            name = chat_info.get("display_name", "").lower()
            if search in name:
                matches.append((chat_id, chat_info))

        if not matches:
            weechat.prnt("", "teams: no chats matching '{}' in tenant '{}'".format(search, tenant_name))
            return weechat.WEECHAT_RC_OK

        if len(matches) > 10:
            weechat.prnt("", "teams: {} matches for '{}' — showing first 10, be more specific:".format(len(matches), search))
            for chat_id, chat_info in matches[:10]:
                weechat.prnt("", "  ({}) {}".format(
                    chat_info.get("chatType", "?"),
                    chat_info.get("display_name", chat_id[:20])
                ))
            return weechat.WEECHAT_RC_OK

        for chat_id, chat_info in matches:
            if chat_info.get("buffer"):
                # Already open, just switch to it
                weechat.buffer_set(chat_info["buffer"], "display", "1")
                weechat.prnt("", "teams: switched to existing buffer for '{}'".format(chat_info.get("display_name", "")))
            else:
                # Open buffer and fetch messages
                chat_info["_open_buffer"] = True
                if chat_info.get("members"):
                    _ensure_chat_buffer(t, chat_id)
                else:
                    EVENTROUTER.enqueue_request("fetch_chat_members", tenant_name, chat_id)
                EVENTROUTER.enqueue_request("fetch_chat_messages", tenant_name, chat_id)
                weechat.prnt("", "teams: opening chat '{}'".format(chat_info.get("display_name", "")))

        return weechat.WEECHAT_RC_OK

    else:
        write_command_error("chat", "Unknown action '{}'. Use: list, open".format(action))
        return weechat.WEECHAT_RC_ERROR


def command_channel(args, buffer):
    parts = args.strip().split(None, 1)
    if not parts:
        write_command_error("channel", "Missing action. Use: list [tenant], open <tenant> <search>")
        return weechat.WEECHAT_RC_ERROR

    action = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""

    if action == "list":
        target_tenant = rest if rest else None
        found = False
        for tname, t in tenants.items():
            if target_tenant and tname != target_tenant:
                continue
            if not t.connected:
                continue
            found = True
            weechat.prnt("", "")
            weechat.prnt("", "{}teams: channels for tenant '{}'{}".format(
                weechat.color("chat_server"), tname, weechat.color("reset")
            ))
            for team_id, team_info in t.teams.items():
                team_name = team_info.get("displayName", team_id)
                for channel_id, ch_info in team_info.get("channels", {}).items():
                    ch_name = ch_info.get("displayName", channel_id)
                    has_buf = "*" if ch_info.get("buffer") else " "
                    weechat.prnt("", "  [{}] #{}.{}".format(has_buf, team_name, ch_name))
            total = sum(len(ti.get("channels", {})) for ti in t.teams.values())
            weechat.prnt("", "  (* = buffer open)  Total: {}".format(total))
        if not found:
            weechat.prnt("", "teams: no connected tenants" + (" matching '{}'".format(target_tenant) if target_tenant else ""))
        return weechat.WEECHAT_RC_OK

    elif action == "open":
        open_parts = rest.split(None, 1)
        if len(open_parts) < 2:
            write_command_error("channel open", "Usage: /teams channel open <tenant> <search>")
            return weechat.WEECHAT_RC_ERROR

        tenant_name = open_parts[0]
        search = open_parts[1].lower()

        t = tenants.get(tenant_name)
        if not t or not t.connected:
            write_command_error("channel open", "Tenant '{}' not connected".format(tenant_name))
            return weechat.WEECHAT_RC_ERROR

        matches = []
        for team_id, team_info in t.teams.items():
            team_name = team_info.get("displayName", "")
            for channel_id, ch_info in team_info.get("channels", {}).items():
                ch_name = ch_info.get("displayName", "")
                full_name = "{}.{}".format(team_name, ch_name).lower()
                if search in full_name:
                    matches.append((team_id, channel_id, team_info, ch_info))

        if not matches:
            weechat.prnt("", "teams: no channels matching '{}' in tenant '{}'".format(search, tenant_name))
            return weechat.WEECHAT_RC_OK

        if len(matches) > 10:
            weechat.prnt("", "teams: {} matches — showing first 10, be more specific:".format(len(matches)))
            for team_id, channel_id, team_info, ch_info in matches[:10]:
                weechat.prnt("", "  #{}.{}".format(
                    team_info.get("displayName", ""), ch_info.get("displayName", "")
                ))
            return weechat.WEECHAT_RC_OK

        for team_id, channel_id, team_info, ch_info in matches:
            if ch_info.get("buffer"):
                weechat.buffer_set(ch_info["buffer"], "display", "1")
                weechat.prnt("", "teams: switched to #{}.{}".format(
                    team_info.get("displayName", ""), ch_info.get("displayName", "")
                ))
            else:
                _ensure_channel_buffer(t, team_id, channel_id)
                EVENTROUTER.enqueue_request("fetch_channel_messages", tenant_name, team_id, channel_id)
                weechat.prnt("", "teams: opening #{}.{}".format(
                    team_info.get("displayName", ""), ch_info.get("displayName", "")
                ))

        return weechat.WEECHAT_RC_OK

    else:
        write_command_error("channel", "Unknown action '{}'. Use: list, open".format(action))
        return weechat.WEECHAT_RC_ERROR


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

def shutdown_cb():
    for name in list(tenants.keys()):
        t = tenants[name]
        t.disconnect()
    config.save()
    return weechat.WEECHAT_RC_OK


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

EVENTROUTER = EventRouter()

buffered_response_cb = EVENTROUTER.buffered_response_cb

config = Config()

# Register with weechat
weechat.register(
    SCRIPT_NAME,
    SCRIPT_AUTHOR,
    SCRIPT_VERSION,
    SCRIPT_LICENSE,
    SCRIPT_DESC,
    "shutdown_cb",
    ""
)

config.setup()
config.read()

# Register /teams command
weechat.hook_command(
    "teams",
    "Microsoft Teams commands",
    "tenant add <name> || tenant del <name> || tenant list"
    " || auth <tenant> || connect [<tenant>] || disconnect <tenant>"
    " || chat list [<tenant>] || chat open <tenant> <search>"
    " || channel list [<tenant>] || channel open <tenant> <search>",
    "     tenant add: add a new tenant configuration\n"
    "     tenant del: remove a tenant configuration\n"
    "    tenant list: list configured tenants\n"
    "           auth: start device code authentication for a tenant\n"
    "        connect: connect to a tenant (or all autoconnect tenants)\n"
    "     disconnect: disconnect from a tenant\n"
    "      chat list: list all known group chats (with open status)\n"
    "      chat open: open a chat buffer by fuzzy name search\n"
    "   channel list: list all team channels (with open status)\n"
    "   channel open: open a channel buffer by fuzzy name search",
    "tenant add || tenant del %(teams_tenant_names) || tenant list"
    " || auth %(teams_tenant_names)"
    " || connect %(teams_tenant_names)"
    " || disconnect %(teams_tenant_names)"
    " || chat list %(teams_tenant_names)"
    " || chat open %(teams_tenant_names)"
    " || channel list %(teams_tenant_names)"
    " || channel open %(teams_tenant_names)",
    "teams_command_cb",
    ""
)

# Register completions
def tenant_completion_cb(data, completion_item, current_buffer, completion):
    for name in config.tenant_names():
        weechat.hook_completion_list_add(completion, name, 0, weechat.WEECHAT_LIST_POS_SORT)
    return weechat.WEECHAT_RC_OK


weechat.hook_completion("teams_tenant_names", "complete tenant names for Teams", "tenant_completion_cb", "")

# Request queue timer
weechat.hook_timer(QUEUE_INTERVAL_MS, 0, 0, "handle_queued_request_cb", "")

# Auto-connect tenants if weechat auto_connect is enabled
if weechat.info_get("auto_connect", "") == "1":
    for name in config.tenant_names():
        autoconnect = config.get_tenant_value(name, "autoconnect")
        if autoconnect == "on":
            connect_tenant(name)
