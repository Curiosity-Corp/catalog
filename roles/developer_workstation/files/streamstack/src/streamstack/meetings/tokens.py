"""Where sign-in tokens and private calendar links are kept: the desktop keyring (Secret Service), never a file.

A calendar feed's address is a password in disguise (anyone holding the link can read the calendar), so those links
live here too and the config file keeps only a name. Without a keyring the Console refuses to save a secret and says
how to get one, instead of writing it in plain text.

The `keyring` package is the optional `calendar` extra. `SecretStore` is the small interface the rest uses; tests and
the demo rig use `MemorySecrets`.
"""

from __future__ import annotations

import threading
from typing import Protocol

SERVICE = "streamstack"


class SecretError(RuntimeError):
    """The keyring cannot be used; the message is for the person."""


class SecretStore(Protocol):
    def available(self) -> str | None: ...
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class MemorySecrets:
    """Secrets held in memory only (tests and the demo rig)."""

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        self._items: dict[str, str] = {}
        self._lock = threading.Lock()

    def available(self) -> str | None:
        return self.reason

    def _check(self) -> None:
        if self.reason:
            raise SecretError(self.reason)

    def get(self, name: str) -> str | None:
        self._check()
        with self._lock:
            return self._items.get(name)

    def set(self, name: str, value: str) -> None:
        self._check()
        with self._lock:
            self._items[name] = value

    def delete(self, name: str) -> None:
        self._check()
        with self._lock:
            self._items.pop(name, None)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._items)


class KeyringStore:
    """The desktop keyring through the `keyring` package."""

    NO_KEYRING = (
        "There is no keyring to keep the sign-in in. Install the calendar extra (pip install 'streamstack[calendar]') "
        "and a Secret Service keyring (GNOME Keyring or KWallet), then sign in again."
    )

    def __init__(self, service: str = SERVICE) -> None:
        self.service = service

    def available(self) -> str | None:
        try:
            import keyring
            from keyring.errors import KeyringError
        except ImportError:
            return self.NO_KEYRING
        try:
            backend = keyring.get_keyring()
        except KeyringError as exc:
            return f"The keyring cannot be opened: {exc}"
        if getattr(backend, "priority", 0) <= 0 or type(backend).__module__.startswith("keyring.backends.fail"):
            return self.NO_KEYRING
        return None

    def _call(self, fn_name: str, *args: str) -> str | None:
        reason = self.available()
        if reason:
            raise SecretError(reason)
        import keyring
        from keyring.errors import KeyringError

        try:
            return getattr(keyring, fn_name)(self.service, *args)
        except KeyringError as exc:
            raise SecretError(f"The keyring refused: {exc}") from exc
        except OSError as exc:
            raise SecretError(f"The keyring could not be reached: {exc}") from exc

    def get(self, name: str) -> str | None:
        return self._call("get_password", name)

    def set(self, name: str, value: str) -> None:
        self._call("set_password", name, value)

    def delete(self, name: str) -> None:
        try:
            self._call("delete_password", name)
        except SecretError as exc:
            if "not found" not in str(exc).lower():
                raise
