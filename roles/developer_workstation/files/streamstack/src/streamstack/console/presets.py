"""Console presets: platform and tier choice, one-click profiles, and the user's saved profiles (see docs/presets.md).

A profile bundles a look, a platform, a bandwidth tier, a lighting scene, a voice preset and a framing mode.
Applying one sets every part through the actions the Console already has (`video.looks.apply`, `lighting.preset`,
`audio.preset`, `framing.mode`) and, for the platform and tier, through `stream.set`. `stream.set` is a pending
contract: the stream backend is built elsewhere, so until the rig answers it (HTTP 501 from the router) the choice
is saved and shown as "pending" and everything else is applied for real.

Actions (all take effect at once and are reflected in the `presets` block of the state snapshot):

    presets.platform        {id}                       choose the platform (keeps the tier, default Balanced)
    presets.tier            {id}                       choose the bandwidth tier
    presets.profile.apply   {id | name}                apply a built-in (by id) or saved (by name) profile
    presets.profile.save    {profile: {...}}           save or replace one of the user's profiles
    presets.profile.delete  {name}                     delete a saved profile
    presets.profile.import  {document: {...}}          import a profile file (schema streamstack.profile(s)/1)
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from streamstack.console.errors import ActionError
from streamstack.presets import PresetError, catalog, resolve
from streamstack.presets.schema import validate_profile, validate_profile_document
from streamstack.presets.store import ProfileStore

Dispatch = Callable[[str, dict], "str | None"]
Notify = Callable[[str, str], None]

DEFAULT_TIER = "balanced"
PENDING = 501  # the router's answer for an action whose backend is not part of this rig yet
STREAM_CONTRACT = "stream.set"

PART_LABELS = {
    "look": "Look",
    "stream": "Platform and tier",
    "lighting": "Lighting",
    "voice": "Voice",
    "framing": "Framing",
}


def _bad(exc: PresetError) -> ActionError:
    return ActionError(str(exc), 400)


class PresetsController:
    """The `presets.*` actions and the `presets` state block."""

    def __init__(
        self,
        dispatch: Dispatch,
        store: ProfileStore | None = None,
        notify: Notify | None = None,
    ) -> None:
        self.dispatch = dispatch
        self.catalog = catalog.load()
        self.store = store or ProfileStore(platforms=self.catalog.platform_ids, tiers=self.catalog.tier_ids)
        self.notify: Notify | None = notify
        self.lock = threading.RLock()
        self.stream: dict[str, str | None] = {"status": "idle", "message": None}
        self.last_apply: dict[str, Any] | None = None
        self.profile: str | None = None  # the name of the profile applied last

    # -- state ----------------------------------------------------------------------------------------------

    def plan(self, platform: str | None, tier: str | None) -> dict[str, Any] | None:
        if not platform or not tier:
            return None
        return resolve(self.catalog.platform(platform), self.catalog.tier(tier))

    def state(self) -> dict[str, Any]:
        with self.lock:
            selection = self.store.selection()
            profiles = [{**p, "builtin": True} for p in self.catalog.profiles]
            profiles += [{"id": None, "builtin": False, **p} for p in self.store.profiles().values()]
            return {
                "platform": selection["platform"],
                "tier": selection["tier"],
                "plan": self.plan(selection["platform"], selection["tier"]),
                "stream": {"contract": STREAM_CONTRACT, **self.stream},
                "profile": self.profile,
                "profiles": profiles,  # the built-ins first (with an id), then the user's own (applied by name)
                "last_apply": self.last_apply,
            }

    # -- actions --------------------------------------------------------------------------------------------

    def act(self, action: str, params: dict) -> str | None:
        handlers: dict[str, Callable[[dict], str | None]] = {
            "presets.platform": self._platform,
            "presets.tier": self._tier,
            "presets.clear": self._clear,
            "presets.profile.apply": self._apply,
            "presets.profile.save": self._save,
            "presets.profile.delete": self._delete,
            "presets.profile.import": self._import,
        }
        handler = handlers.get(action)
        if handler is None:
            raise ActionError(f"unknown action: {action}", 400)
        try:
            return handler(params)
        except PresetError as exc:
            raise _bad(exc) from exc

    @staticmethod
    def _id(params: dict, key: str) -> str:
        value = params.get(key)
        if not isinstance(value, str) or not value:
            raise ActionError(f"{key} is required", 400)
        return value

    def _platform(self, params: dict) -> str:
        platform = self._id(params, "id")
        self.catalog.platform(platform)  # unknown ids raise PresetError -> 400
        with self.lock:
            tier = self.store.selection()["tier"] or DEFAULT_TIER
            return self._choose(platform, tier)

    def _clear(self, params: dict) -> str:
        """Forget the platform and tier choice and the last apply (nothing is sent to the stream backend)."""
        with self.lock:
            try:
                self.store.set_selection(None, None)
            except OSError as exc:
                raise ActionError(f"Could not save the choice: {exc.strerror or exc}", 409) from exc
            self.stream = {"status": "idle", "message": None}
            self.last_apply = None
            self.profile = None
        return "Platform and tier cleared"

    def _tier(self, params: dict) -> str:
        tier = self._id(params, "id")
        self.catalog.tier(tier)
        with self.lock:
            platform = self.store.selection()["platform"]
            if platform is None:
                try:
                    self.store.set_selection(None, tier)
                except OSError as exc:
                    raise ActionError(f"Could not save the choice: {exc.strerror or exc}", 409) from exc
                return f"Tier: {self.catalog.tier(tier)['name']}"
            return self._choose(platform, tier)

    def _choose(self, platform: str, tier: str) -> str:
        """Save a platform and tier, then hand the resolved plan to `stream.set`."""
        try:
            self.store.set_selection(platform, tier)
        except OSError as exc:
            raise ActionError(f"Could not save the choice: {exc.strerror or exc}", 409) from exc
        status, message = self._send_stream(platform, tier)
        label = f"{self.catalog.platform(platform)['name']}, {self.catalog.tier(tier)['name']}"
        if status == "failed":
            raise ActionError(f"{label}: {message}", 409)
        return label if status == "applied" else f"{label} (saved; {message})"

    def _send_stream(self, platform: str, tier: str) -> tuple[str, str]:
        plan = self.plan(platform, tier)
        try:
            self.dispatch(STREAM_CONTRACT, {"platform": platform, "tier": tier, "plan": plan})
        except ActionError as exc:
            if exc.status == PENDING:
                result = ("pending", "the stream backend is not installed yet, so it takes effect once it is")
            else:
                result = ("failed", exc.message)
        else:
            result = ("applied", "sent to the stream backend")
        self.stream = {"status": result[0], "message": result[1]}
        return result

    # -- profiles -------------------------------------------------------------------------------------------

    def _find(self, params: dict) -> dict[str, Any]:
        if isinstance(params.get("id"), str):
            for profile in self.catalog.profiles:
                if profile["id"] == params["id"]:
                    return dict(profile)
            raise ActionError(f"No built-in profile with id {params['id']!r}.", 400)
        name = self._id(params, "name")
        saved = self.store.profiles()
        if name in saved:
            return dict(saved[name])
        for profile in self.catalog.profiles:
            if profile["name"] == name:
                return dict(profile)
        raise ActionError(f"No profile called {name}.", 400)

    def _step(self, part: str, run: Callable[[], str | None]) -> dict[str, str | None]:
        try:
            message = run()
        except ActionError as exc:
            return {"part": part, "label": PART_LABELS[part], "status": "failed", "message": exc.message}
        return {"part": part, "label": PART_LABELS[part], "status": "ok", "message": message}

    def _apply(self, params: dict) -> str:
        profile = self._find(params)
        steps: list[dict[str, str | None]] = []
        with self.lock:
            if profile["look"]:
                steps.append(self._step("look", lambda: self.dispatch("video.looks.apply", {"name": profile["look"]})))
            if profile["platform"] and profile["tier"]:
                steps.append(self._stream_step(profile["platform"], profile["tier"]))
            if profile["lighting"]:
                steps.append(
                    self._step("lighting", lambda: self.dispatch("lighting.preset", {"name": profile["lighting"]}))
                )
            if profile["voice"]:
                steps.append(self._step("voice", lambda: self.dispatch("audio.preset", {"name": profile["voice"]})))
            if profile["framing"]:
                steps.append(self._step("framing", lambda: self.dispatch("framing.mode", {"mode": profile["framing"]})))
            if not steps:
                raise ActionError(f"The profile {profile['name']} has no parts to apply.", 400)
            good = [s for s in steps if s["status"] != "failed"]
            if not good:
                raise ActionError(f"{profile['name']}: {steps[0]['message']}", 409)
            self.profile = profile["name"]
            self.last_apply = {"profile": profile["name"], "steps": steps, "at": round(time.time(), 1)}
        failed = [s for s in steps if s["status"] == "failed"]
        text = f"Profile {profile['name']}: {len(good)} of {len(steps)} parts applied"
        if failed:
            text += f" ({failed[0]['label'].lower()}: {failed[0]['message']})"
            if self.notify:
                self.notify("warn", text)
        return text

    def _stream_step(self, platform: str, tier: str) -> dict[str, str | None]:
        try:
            self.store.set_selection(platform, tier)
        except OSError as exc:
            return {
                "part": "stream",
                "label": PART_LABELS["stream"],
                "status": "failed",
                "message": exc.strerror or str(exc),
            }
        status, message = self._send_stream(platform, tier)
        return {
            "part": "stream",
            "label": PART_LABELS["stream"],
            "status": "ok" if status == "applied" else status,
            "message": message,
        }

    def _check_name(self, name: str) -> None:
        folded = name.casefold()
        for profile in self.catalog.profiles:
            if folded in (profile["name"].casefold(), profile["id"]):
                raise ActionError(f"{name} is a built-in profile; pick another name.", 400)

    def _save(self, params: dict) -> str:
        item = validate_profile(params.get("profile"), platforms=self.catalog.platform_ids, tiers=self.catalog.tier_ids)
        item.pop("id", None)
        self._check_name(item["name"])
        try:
            self.store.save_many([item])
        except OSError as exc:
            raise ActionError(f"Could not save the profile: {exc.strerror or exc}", 409) from exc
        return f"Saved profile {item['name']}"

    def _delete(self, params: dict) -> str:
        name = self._id(params, "name")
        try:
            self.store.delete(name)
        except OSError as exc:
            raise ActionError(f"Could not delete the profile: {exc.strerror or exc}", 409) from exc
        with self.lock:
            if self.profile == name:
                self.profile = None
        return f"Deleted profile {name}"

    def _import(self, params: dict) -> str:
        items = validate_profile_document(
            params.get("document"), platforms=self.catalog.platform_ids, tiers=self.catalog.tier_ids
        )
        for item in items:
            item.pop("id", None)
            self._check_name(item["name"])
        names = [i["name"] for i in items]
        if len({n.casefold() for n in names}) != len(names):
            raise ActionError("The file holds two profiles with the same name.", 400)
        replaced = [n for n in names if n in self.store.profiles()]
        try:
            self.store.save_many(items)
        except OSError as exc:
            raise ActionError(f"Could not save the imported profiles: {exc.strerror or exc}", 409) from exc
        text = f"Imported {len(items)} profile{'s' if len(items) != 1 else ''}"
        return text + (f" (replaced {', '.join(replaced)})" if replaced else "")
