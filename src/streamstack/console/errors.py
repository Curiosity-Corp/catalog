"""Errors controllers raise for expected failures; the server turns them into JSON answers."""

from __future__ import annotations


class ActionError(Exception):
    """A request the rig cannot carry out: 400 for bad input, 409 for a helper that is not running."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class Superseded(ActionError):
    """A guarded write found the light changed by someone else since the caller last looked, and did not write."""

    def __init__(self, message: str = "The light was changed by someone else; the write was dropped.") -> None:
        super().__init__(message, 409)
