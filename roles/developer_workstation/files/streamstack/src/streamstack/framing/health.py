"""The "Auto-framing" check for `streamstack doctor` and the Console System page.

This static form never opens a detector and never touches the GPU: it reports whether the optional packages and
the verified model are in place. The running Console replaces it with the live form (device and time per
detection) while framing is on.
"""

from __future__ import annotations

from streamstack.framing import detector, model

INSTALL = "pip install 'streamstack[framing]' && streamstack framing --fetch"


def check(state: str, title: str, detail: str = "", hint: str = "") -> dict[str, str]:
    return {"id": "framing", "group": "video", "state": state, "title": title, "detail": detail, "hint": hint}


def readiness() -> tuple[bool, str, str]:
    """(ready, what is missing, how to fix it). Cheap: no import of the heavy packages, no device access."""
    modules = detector.available_modules()
    missing = [name for name in ("numpy", "cv2") if not modules[name]]
    if missing:
        return False, "needs " + " and ".join("opencv" if m == "cv2" else m for m in missing), INSTALL
    state, detail = model.status()
    if state != "ok":
        return False, detail, "streamstack framing --fetch"
    return True, "", ""


def health_check(cfg: dict[str, str] | None = None) -> dict[str, str]:
    """ok when it is ready or simply not installed (it is optional), warn when it is half installed."""
    modules = detector.available_modules()
    wanted = (cfg or {}).get("STREAMSTACK_FRAMING_MODE", "off") not in ("", "off")
    ready, why, hint = readiness()
    if not ready and not (modules["numpy"] and modules["cv2"]):
        return check("warn" if wanted else "ok", "Auto-framing is not installed (optional)", why, hint)
    if not ready:
        return check("warn", "Auto-framing is missing its face model", why, hint)
    engine = (
        "OpenVINO (Intel GPU or CPU)" if modules["openvino"] else "OpenCV on the CPU (install openvino for the GPU)"
    )
    return check("ok", "Auto-framing is ready", f"{model.YUNET.name}, {engine}; {model.status()[1]}")
