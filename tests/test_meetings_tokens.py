"""The keyring store: secrets go to the desktop keyring, and without one nothing is written anywhere."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

keyring = pytest.importorskip("keyring")
from keyring.backend import KeyringBackend  # noqa: E402
from keyring.backends import fail  # noqa: E402
from keyring.errors import PasswordDeleteError  # noqa: E402

from streamstack.meetings.tokens import KeyringStore, SecretError  # noqa: E402


class Memory(KeyringBackend):
    priority = 5

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.items:
            raise PasswordDeleteError("Password not found")
        del self.items[(service, username)]


@pytest.fixture
def backend() -> Iterator[Memory]:
    previous = keyring.get_keyring()
    memory = Memory()
    keyring.set_keyring(memory)
    yield memory
    keyring.set_keyring(previous)


def test_secrets_round_trip_through_the_keyring_under_the_streamstack_service(backend: Memory) -> None:
    store = KeyringStore()
    assert store.available() is None
    assert store.get("m365") is None
    store.set("m365", "secret-value")
    assert store.get("m365") == "secret-value" and backend.items == {("streamstack", "m365"): "secret-value"}
    store.delete("m365")
    assert store.get("m365") is None
    store.delete("m365")  # deleting what is not there is fine


def test_a_machine_without_a_usable_keyring_refuses_to_store_anything() -> None:
    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        store = KeyringStore()
        assert "no keyring" in (store.available() or "").lower()
        with pytest.raises(SecretError, match="no keyring"):
            store.set("m365", "x")
    finally:
        keyring.set_keyring(previous)
