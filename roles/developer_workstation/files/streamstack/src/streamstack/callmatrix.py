"""The call-app compatibility matrix, scripted (#87): the same camera probe through every browser a call runs in.

Teams, Meet and Zoom's web client are browsers with their own camera handling, and the apps differ in how they open
a camera (#87). This runs `camprobe` through each browser available here and prints one table: which list StreamStack
Camera, which open it, and which get a moving picture.

- **Chromium**, **Chrome** (channel `chrome`) and **Edge** (channel `msedge`): the engines behind Meet and Teams web.
- **Teams in Edge**: Edge with the media path Teams uses (`getUserMedia` for the camera by device id at 1280x720,
  30 fps, permission prompt auto-accepted). It proves the camera path in the browser Teams runs in; it does not sign
  in to Teams.
- **Firefox**: its own camera stack (and the portal when PipeWire cameras are enabled).

A browser that is not installed is reported as skipped, not failed. `fake=True` uses each browser's own test camera
(CI); without it, the real cameras of this machine are opened for reading. Desktop Teams and Zoom are Electron and
native apps: they cannot be driven here and stay on the human list (docs/camera-proof.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from streamstack import camprobe

# name, playwright browser, channel
ENTRIES: tuple[tuple[str, str, str | None], ...] = (
    ("Chromium", "chromium", None),
    ("Chrome", "chromium", "chrome"),
    ("Edge", "chromium", "msedge"),
    ("Teams in Edge", "chromium", "msedge"),
    ("Firefox", "firefox", None),
)


@dataclass
class Result:
    name: str
    status: str  # pass | fail | skip
    note: str = ""
    rows: list[camprobe.Row] = field(default_factory=list)


def run_entry(
    entry: tuple[str, str, str | None],
    label: str | None,
    *,
    fake: bool,
    seconds: float,
    runner=camprobe.run,
) -> Result:
    name, browser, channel = entry
    wanted = label or (camprobe.fake_label(browser) if fake else camprobe.DEFAULT_LABEL)
    try:
        report = runner(wanted, seconds=seconds, fake=fake, channel=channel, browser=browser)
    except camprobe.BrowserMissing as exc:
        return Result(name, "skip", str(exc))
    except RuntimeError as exc:
        return Result(name, "fail", str(exc))
    rows = camprobe.evaluate(report, wanted)
    failed = [r for r in rows if not r.ok]
    return Result(name, "fail" if failed else "pass", failed[0].title if failed else "", rows)


def run_matrix(
    label: str | None = None,
    *,
    fake: bool = False,
    seconds: float = 3.0,
    only: tuple[str, ...] = (),
    runner=camprobe.run,
) -> list[Result]:
    wanted = {o.lower() for o in only}
    entries = [e for e in ENTRIES if not wanted or e[0].lower() in wanted]
    return [run_entry(e, label, fake=fake, seconds=seconds, runner=runner) for e in entries]


def exit_code(results: list[Result], *, require_all: bool = False) -> int:
    """0 every browser that ran passed (and, with require_all, none was skipped), 1 a failure, 2 nothing ran."""
    if any(r.status == "fail" for r in results):
        return 1
    if not any(r.status == "pass" for r in results) or (require_all and any(r.status == "skip" for r in results)):
        return 2
    return 0


def table(results: list[Result]) -> str:
    out = ["| Browser | Result | Detail |", "|---|---|---|"]
    for r in results:
        detail = r.note or next((row.title for row in reversed(r.rows)), "")
        out.append(f"| {r.name} | {r.status} | {detail} |")
    return "\n".join(out)
