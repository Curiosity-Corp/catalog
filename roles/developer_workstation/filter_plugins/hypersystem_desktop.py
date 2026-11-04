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

`urls.web` must be an https URL: browsers install a web app only from a secure
origin. Everything here is deterministic and makes no network call.
"""

from __future__ import annotations

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


def _web_url(value: Any, *, https_only: bool) -> str | None:
    """Return the URL when it is an absolute http(s) URL with a host, else None."""
    if not isinstance(value, str) or value == "" or _CONTROL.search(value) or " " in value:
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        return None
    allowed = ("https",) if https_only else ("https", "http")
    if parts.scheme not in allowed or not host or not _HOST.match(host):
        return None
    return value


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
        web = _web_url(urls.get("web"), https_only=True)
        if web is None:
            problems.append(f"{label}: urls.web must be an absolute https URL (web apps install only from a secure origin)")
        if urls.get("local") is not None:
            local = _web_url(urls.get("local"), https_only=False)
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
    entry: dict[str, Any] = {
        "id": app_id,
        "name": name,
        "purpose": purpose,
        "icon": icon,
        "roles": list(roles),
        "urls": {"web": web},
        "host": (urlsplit(web).hostname or "").lower(),
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


def hypersystem_desktop_select(apps: Any, roles: Any, statuses: Any) -> list[dict[str, Any]]:
    """Return the apps whose status is in `statuses` and whose roles are empty or share one with `roles`.

    Catalog order is kept, so launchers and the policy list are stable from run to run.
    """
    if not isinstance(apps, list):
        raise AnsibleFilterError("hypersystem_desktop_select: apps must be a list")
    for name, value in (("roles", roles), ("statuses", statuses)):
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise AnsibleFilterError(f"hypersystem_desktop_select: {name} must be a list of strings")
    held = set(roles)
    wanted = set(statuses)
    return [
        app
        for app in apps
        if app.get("status") in wanted and (not app.get("roles") or held.intersection(app["roles"]))
    ]


def hypersystem_desktop_uncovered_hosts(apps: Any, domains: Any) -> list[str]:
    """Return the app web hosts that no Ziti intercept domain covers.

    A domain covers a host when the host equals it or ends with `.<domain>`.
    A leading `*.` or `.` on a domain is ignored, so `*.suite.example` and
    `suite.example` mean the same.
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
        host = str(app.get("host", "")).lower()
        if not any(host == suffix or host.endswith("." + suffix) for suffix in suffixes) and host not in uncovered:
            uncovered.append(host)
    return uncovered


def hypersystem_desktop_policy(apps: Any) -> dict[str, Any]:
    """Build the Chromium managed policy that force-installs each app as a web app."""
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
            "hypersystem_desktop_uncovered_hosts": hypersystem_desktop_uncovered_hosts,
            "hypersystem_desktop_policy": hypersystem_desktop_policy,
            "hypersystem_desktop_launchers": hypersystem_desktop_launchers,
            "hypersystem_desktop_entry_value": hypersystem_desktop_entry_value,
        }
