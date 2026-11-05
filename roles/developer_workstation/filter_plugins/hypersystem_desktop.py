"""Filters for the hypersystem_desktop task group (tasks/hypersystem-desktop.yml).

They read a suite app catalog, either the compiled `hypersystem-catalog/v1`
document or a list of apps given as role variables, and select the apps one
person may use. The rules follow the suite bar's own parser so a laptop and the
bar agree on which apps exist:

- an entry needs `id` (lowercase letters, digits and dashes, starting with a
  letter), `name`, `roles` (a list of non-empty strings; empty means every
  signed-in person), `urls.web` and `status` (`planned`, `beta`, `ga` or
  `retired`);
- `purpose`, `icon`, `ssh` and `urls.local` are optional here because the
  desktop package does not need them to install an app;
- a malformed entry is dropped and reported, and a duplicate id is reported,
  so one bad row is never installed silently. The task group fails on any
  problem unless `hypersystem_desktop_catalog_strict` is false.

`urls.web` is an absolute http or https URL, as in the suite schema. Each parsed
entry also carries what the laptop needs to know about that address:

- `host`: the lowercased host of `urls.web`;
- `local`: the host is the loopback (`localhost`, 127.0.0.0/8 or ::1), so the
  app is a per-machine one (hyperharness on the person's own laptop) that never
  goes through Ziti;
- `pwa`: the origin is secure (https, or http on the loopback), the only kind a
  browser installs as a web app. An app on plain http elsewhere still gets a
  launcher that opens it in an app window, but no web app install.

Everything here is deterministic and makes no network call.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlsplit

from ansible.errors import AnsibleFilterError

CATALOG_SCHEMA = "hypersystem-catalog/v1"
STATUSES = ("planned", "beta", "ga", "retired")
_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _web_url(value: Any) -> str | None:
    """Return the URL when it is an absolute http(s) URL with a host (and a valid port), else None."""
    if not isinstance(value, str) or value == "" or _CONTROL.search(value) or " " in value:
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a port outside 0-65535
    except ValueError:
        return None
    if parts.scheme not in ("https", "http") or not host or not (_HOST.match(host) or _is_ip(host)):
        return None
    return value


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _is_loopback(host: str) -> bool:
    """True for localhost, 127.0.0.0/8 and ::1: a per-machine address that never goes through Ziti."""
    host = host.lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _text(raw: dict[str, Any], field: str, problems: list[str], label: str, *, required: bool) -> str:
    value = raw.get(field)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or value.strip() == "":
        if required or value is not None:
            problems.append(f"{label}: {field} must be a non-empty string")
        return ""
    if _CONTROL.search(value):
        problems.append(f"{label}: {field} must not contain control characters")
        return ""
    return value.strip()


def _parse_entry(raw: Any, index: int, defaults: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    problems: list[str] = []
    if not isinstance(raw, dict):
        kind = "null" if raw is None else type(raw).__name__
        return None, [f"entry {index}: expected a mapping, got {kind}"]
    label = raw["id"] if isinstance(raw.get("id"), str) and raw["id"] else f"entry {index}"

    app_id = _text(raw, "id", problems, label, required=True)
    if app_id and not _ID.match(app_id):
        problems.append(f"{label}: id {app_id!r} must be lowercase letters, digits and dashes, starting with a letter")
    name = _text(raw, "name", problems, label, required=True)
    purpose = _text(raw, "purpose", problems, label, required=False)
    icon = _text(raw, "icon", problems, label, required=False)

    roles = raw.get("roles", defaults.get("roles"))
    if not isinstance(roles, list) or any(not isinstance(role, str) or role == "" for role in roles):
        problems.append(f"{label}: roles must be a list of non-empty strings")
        roles = []

    urls = raw.get("urls")
    web = local = None
    if not isinstance(urls, dict):
        problems.append(f"{label}: urls must be a mapping with a web address")
    else:
        web = _web_url(urls.get("web"))
        if web is None:
            problems.append(f"{label}: urls.web must be an absolute http(s) URL")
        if urls.get("local") is not None:
            local = _web_url(urls.get("local"))
            if local is None:
                problems.append(f"{label}: urls.local must be an absolute http(s) URL when present")

    ssh = raw.get("ssh")
    if ssh is not None and not (isinstance(ssh, str) and _HOST.match(ssh)):
        problems.append(f"{label}: ssh must be a host name or null")
        ssh = None

    status = raw.get("status", defaults.get("status"))
    if status not in STATUSES:
        problems.append(f"{label}: status must be one of {', '.join(STATUSES)}")

    if problems:
        return None, problems
    parts = urlsplit(web)
    host = (parts.hostname or "").lower()
    local_app = _is_loopback(host)
    entry: dict[str, Any] = {
        "id": app_id,
        "name": name,
        "purpose": purpose,
        "icon": icon,
        "roles": list(roles),
        "urls": {"web": web},
        "host": host,
        "local": local_app,
        "pwa": parts.scheme == "https" or local_app,
        "ssh": ssh,
        "status": status,
    }
    if local is not None:
        entry["urls"]["local"] = local
    return entry, []


def hypersystem_desktop_parse_catalog(document: Any, source: str = "catalog", mode: str = "document") -> dict[str, Any]:
    """Parse a catalog into `{apps: [...], problems: [...]}`.

    `document` is the compiled catalog (JSON text, a mapping with `schema` and
    `apps`, or a bare list) when `mode` is `document`, or the list from
    `hypersystem_desktop_apps` when `mode` is `variables`. Variable entries may
    leave out `roles` (everyone) and `status` (`ga`). A document that is not a
    catalog at all raises, naming `source`.
    """
    if mode not in ("document", "variables"):
        raise AnsibleFilterError(f"hypersystem_desktop_parse_catalog: unknown mode {mode!r}")
    if isinstance(document, (bytes, bytearray)):
        document = document.decode("utf-8", errors="replace")
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except json.JSONDecodeError as error:
            raise AnsibleFilterError(f"{source} is not valid JSON: {error}") from error

    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict) and mode == "document":
        schema = document.get("schema")
        if schema != CATALOG_SCHEMA:
            raise AnsibleFilterError(f"{source} has schema {schema!r}; expected {CATALOG_SCHEMA!r}")
        entries = document.get("apps")
        if not isinstance(entries, list):
            raise AnsibleFilterError(f"{source} has no `apps` list")
    else:
        raise AnsibleFilterError(f"{source} must be a list of apps or a {CATALOG_SCHEMA} document with an `apps` list")

    defaults = {"roles": [], "status": "ga"} if mode == "variables" else {}
    apps: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(entries):
        entry, entry_problems = _parse_entry(raw, index, defaults)
        problems.extend(entry_problems)
        if entry is None:
            continue
        if entry["id"] in seen:
            problems.append(f"{entry['id']}: duplicate id")
            continue
        seen.add(entry["id"])
        apps.append(entry)
    return {"apps": apps, "problems": problems}


def _string_list(name: str, value: Any, caller: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AnsibleFilterError(f"{caller}: {name} must be a list of strings")
    return value


def hypersystem_desktop_select(
    apps: Any,
    roles: Any,
    statuses: Any,
    local_ids: Any = None,
    prefiltered: bool = False,
) -> list[dict[str, Any]]:
    """Return the apps this laptop installs.

    An app is kept when its status is in `statuses` and its roles are empty or
    share one with `roles`. With `prefiltered` true the role test is skipped:
    the source (HyperSystem Home's `GET /suite/v1/catalog`) already filtered the
    document by the person's realm roles, as the bar does with `roles` null.
    A per-machine (`local`) app is kept only when its id is in `local_ids`,
    because its address is a service on this laptop that may not be installed;
    `local_ids` None keeps every one. Catalog order is kept, so launchers and
    the policy list are stable from run to run.
    """
    caller = "hypersystem_desktop_select"
    if not isinstance(apps, list):
        raise AnsibleFilterError(f"{caller}: apps must be a list")
    held = set(_string_list("roles", roles, caller))
    wanted = set(_string_list("statuses", statuses, caller))
    allowed_local = None if local_ids is None else set(_string_list("local_ids", local_ids, caller))
    return [
        app
        for app in apps
        if app.get("status") in wanted
        and (prefiltered or not app.get("roles") or held.intersection(app["roles"]))
        and (not app.get("local") or allowed_local is None or app.get("id") in allowed_local)
    ]


def hypersystem_desktop_skipped_local(apps: Any, roles: Any, statuses: Any, local_ids: Any, prefiltered: bool = False) -> list[str]:
    """Ids of the per-machine apps the person may use but this laptop does not install (not in `local_ids`)."""
    usable = hypersystem_desktop_select(apps, roles, statuses, None, prefiltered)
    kept = {app["id"] for app in hypersystem_desktop_select(apps, roles, statuses, local_ids, prefiltered)}
    return [app["id"] for app in usable if app["id"] not in kept]


def hypersystem_desktop_uncovered_hosts(apps: Any, domains: Any) -> list[str]:
    """Return the app hosts (web and ssh) that no Ziti intercept domain covers.

    A domain covers a host when the host equals it or ends with `.<domain>`.
    A leading `*.` or `.` on a domain is ignored, so `*.suite.example` and
    `suite.example` mean the same. A per-machine (`local`) app is on the
    loopback and never goes through Ziti, so its web host is not checked.
    """
    if not isinstance(apps, list) or not isinstance(domains, list):
        raise AnsibleFilterError("hypersystem_desktop_uncovered_hosts: apps and domains must be lists")
    suffixes = []
    for domain in domains:
        if not isinstance(domain, str):
            raise AnsibleFilterError("hypersystem_desktop_uncovered_hosts: every domain must be a string")
        cleaned = domain.strip().lower().removeprefix("*.").lstrip(".")
        if cleaned:
            suffixes.append(cleaned)
    uncovered: list[str] = []
    for app in apps:
        hosts = [] if app.get("local") else [str(app.get("host", "")).lower()]
        if isinstance(app.get("ssh"), str) and app["ssh"]:
            hosts.append(app["ssh"].lower())
        for host in hosts:
            if not any(host == suffix or host.endswith("." + suffix) for suffix in suffixes) and host not in uncovered:
                uncovered.append(host)
    return uncovered


def hypersystem_desktop_policy(apps: Any) -> dict[str, Any]:
    """Build the Chromium managed policy that force-installs each app on a secure origin (`pwa`) as a web app.

    An app on plain http outside the loopback is left out: Chromium installs web
    apps only from a secure origin. Its launcher still opens it in an app window.
    """
    if not isinstance(apps, list):
        raise AnsibleFilterError("hypersystem_desktop_policy: apps must be a list")
    return {
        "WebAppInstallForceList": [
            {
                "url": app["urls"]["web"],
                "default_launch_container": "window",
                "create_desktop_shortcut": False,
                "fallback_app_name": app["name"],
            }
            for app in apps
            if app.get("pwa", True)
        ]
    }


def hypersystem_desktop_launchers(apps: Any, suite_name: str, icon_map: Any, default_icon: str) -> list[dict[str, str]]:
    """Describe the menu launchers: one for the suite browser profile, then one per app.

    Each item has `file` (the .desktop file name), `name`, `comment`, `args` (the
    hypersystem-desktop arguments) and `icon` (the catalog icon mapped through
    `icon_map`, else `default_icon`).
    """
    if not isinstance(apps, list) or not isinstance(icon_map, dict):
        raise AnsibleFilterError("hypersystem_desktop_launchers: apps must be a list and icon_map a mapping")
    launchers = [
        {
            "file": "hypersystem-desktop.desktop",
            "name": str(suite_name),
            "comment": "Suite browser profile",
            "args": "browser",
            "icon": str(default_icon),
        }
    ]
    for app in apps:
        icon = icon_map.get(app.get("icon") or "", default_icon)
        launchers.append(
            {
                "file": f"hypersystem-desktop-{app['id']}.desktop",
                "name": app["name"],
                "comment": app.get("purpose") or "",
                "args": f"open {app['id']}",
                "icon": str(icon),
            }
        )
    return launchers


def hypersystem_desktop_ssh_config(apps: Any, header: str) -> str:
    """Render an ssh_config(5) fragment with one `Host <id>` per app that has a terminal UI (`ssh` set).

    `ssh <app>` then reaches the app's SSH endpoint by its catalog host, the way
    the terminal bar opens it (ADR 0310 section 2). Hosts were validated by the
    parser, so a value can never add an ssh_config keyword. Returns "" when no
    app has an SSH endpoint.
    """
    if not isinstance(apps, list):
        raise AnsibleFilterError("hypersystem_desktop_ssh_config: apps must be a list")
    blocks = [f"Host {app['id']}\n    HostName {app['ssh']}\n" for app in apps if isinstance(app.get("ssh"), str) and app["ssh"]]
    if not blocks:
        return ""
    return "".join([f"# {header}\n", *("\n" + block for block in blocks)])


def hypersystem_desktop_ziti_check(status: Any, identity_file: str, services: Any) -> dict[str, Any]:
    """Read `ziti-edge-tunnel tunnel_status` output and say whether the suite services are there.

    `status` is the command's JSON text (or the parsed mapping). With
    `identity_file` set, only the identity loaded from that file counts (matched
    on its `Identifier`, the identity file path); otherwise every loaded identity
    counts, which covers a hand-managed or OIDC tunnel. Returns
    `{ok, error, loaded, missing}`: `error` names a status the tunnel could not
    report, `loaded` says whether the identity is loaded, `missing` lists the
    wanted services no counted identity offers.
    """
    wanted = _string_list("services", services, "hypersystem_desktop_ziti_check")
    result: dict[str, Any] = {"ok": False, "error": "", "loaded": False, "missing": list(wanted)}
    if isinstance(status, (bytes, bytearray)):
        status = status.decode("utf-8", errors="replace")
    if isinstance(status, str):
        try:
            status = json.loads(status)
        except json.JSONDecodeError as error:
            result["error"] = f"tunnel_status printed invalid JSON ({error})"
            return result
    if not isinstance(status, dict):
        result["error"] = "tunnel_status JSON is not an object"
        return result
    if status.get("Success") is False:
        result["error"] = f"tunnel_status reported failure: {status.get('Error', 'no detail')}"
        return result
    data = status.get("Data")
    identities = data.get("Identities") if isinstance(data, dict) else None
    if not isinstance(identities, list):
        result["error"] = "tunnel_status JSON has no Data.Identities list"
        return result
    counted = [
        item
        for item in identities
        if isinstance(item, dict) and item.get("Loaded") is True and (not identity_file or item.get("Identifier") == identity_file)
    ]
    offered = {
        service.get("Name")
        for item in counted
        for service in (item.get("Services") or [])
        if isinstance(service, dict)
    }
    result["loaded"] = bool(counted)
    result["missing"] = [name for name in wanted if name not in offered]
    result["ok"] = result["loaded"] and not result["missing"]
    return result


def hypersystem_desktop_entry_value(value: Any) -> str:
    """Escape a string for a .desktop `localestring` value (Desktop Entry Specification 1.5)."""
    text = _CONTROL.sub(" ", str(value))
    return text.replace("\\", "\\\\")


class FilterModule:
    """Expose the hypersystem_desktop filters to the role."""

    def filters(self) -> dict[str, Any]:
        return {
            "hypersystem_desktop_parse_catalog": hypersystem_desktop_parse_catalog,
            "hypersystem_desktop_select": hypersystem_desktop_select,
            "hypersystem_desktop_skipped_local": hypersystem_desktop_skipped_local,
            "hypersystem_desktop_ssh_config": hypersystem_desktop_ssh_config,
            "hypersystem_desktop_ziti_check": hypersystem_desktop_ziti_check,
            "hypersystem_desktop_uncovered_hosts": hypersystem_desktop_uncovered_hosts,
            "hypersystem_desktop_policy": hypersystem_desktop_policy,
            "hypersystem_desktop_launchers": hypersystem_desktop_launchers,
            "hypersystem_desktop_entry_value": hypersystem_desktop_entry_value,
        }
