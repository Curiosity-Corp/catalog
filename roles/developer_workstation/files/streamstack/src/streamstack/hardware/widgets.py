"""The control descriptions a driver reports and the Console renders.

A widget is a plain dict so it goes straight into the state snapshot:

    id         stable key the UI sends back with `interface.set`
    kind       "switch" | "choice" | "slider" | "action" | "readout"
    label, group, hint
    value      bool | int (index into `options`) | float | str
    options    [str] for a choice
    min, max, step, unit   for a slider
    readonly   a reason string when the control cannot be changed right now, else ""
    danger     true when changing it needs a deliberate confirmation (48V)
    state      "" | "ok" | "warn" for a readout or an action's progress
"""

from __future__ import annotations

from typing import Any

Widget = dict[str, Any]


def widget(kind: str, wid: str, label: str, group: str, **extra: Any) -> Widget:
    base: Widget = {
        "id": wid,
        "kind": kind,
        "label": label,
        "group": group,
        "hint": "",
        "readonly": "",
        "danger": False,
    }
    base.update(extra)
    return base


def switch(wid: str, label: str, group: str, value: bool, **extra: Any) -> Widget:
    return widget("switch", wid, label, group, value=bool(value), **extra)


def choice(wid: str, label: str, group: str, options: list[str], index: int, **extra: Any) -> Widget:
    return widget("choice", wid, label, group, options=list(options), value=int(index), **extra)


def slider(
    wid: str, label: str, group: str, value: float, low: float, high: float, step: float, unit: str, **extra: Any
) -> Widget:
    return widget("slider", wid, label, group, value=value, min=low, max=high, step=step, unit=unit, **extra)


def action(wid: str, label: str, group: str, running: bool, text: str = "", **extra: Any) -> Widget:
    return widget("action", wid, label, group, value=bool(running), text=text, state="", **extra)


def readout(wid: str, label: str, group: str, value: str, state: str = "", **extra: Any) -> Widget:
    return widget("readout", wid, label, group, value=value, state=state, **extra)
