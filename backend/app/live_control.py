"""
Lets someone watch and directly drive the Playwright browser that's actually
performing a portal login/refresh, streamed over a WebSocket via Chrome
DevTools Protocol screencasting, with mouse/keyboard input relayed back the
same way.

Why this exists: the automation browser always runs on whichever machine
executes this server (HEADLESS=false is required — see config.py — so it's a
real, visible window there, not something a remote viewer's OS can show).
Normally nobody needs to touch it: OTP is relayed automatically through the
regular dashboard flow. This is the escape hatch for when someone on a
*different* machine needs to actually see and interact with that browser
directly — e.g. an unexpected verification step the automation doesn't
already handle.
"""
from typing import Optional

from fastapi import WebSocket
from playwright.async_api import Page

# job_id -> {"page": Page, "cdp": CDPSession, "viewer": Optional[WebSocket], "streaming": bool}
_SESSIONS: dict = {}


async def register_page(job_id: str, page: Page) -> None:
    cdp = await page.context.new_cdp_session(page)
    _SESSIONS[job_id] = {"page": page, "cdp": cdp, "viewer": None, "streaming": False}


async def unregister(job_id: str) -> None:
    session = _SESSIONS.pop(job_id, None)
    if not session:
        return
    if session["streaming"]:
        try:
            await session["cdp"].send("Page.stopScreencast")
        except Exception:
            pass


def get_session(job_id: str) -> Optional[dict]:
    return _SESSIONS.get(job_id)


def attach_viewer(job_id: str, ws: WebSocket) -> Optional[dict]:
    """Claims the single viewer slot for a job's live session — only one person can drive it at a time."""
    session = _SESSIONS.get(job_id)
    if not session or session["viewer"] is not None:
        return None
    session["viewer"] = ws
    return session


def detach_viewer(job_id: str) -> None:
    session = _SESSIONS.get(job_id)
    if session:
        session["viewer"] = None


async def dispatch_input(cdp, msg: dict) -> None:
    """Translates one input event from the remote viewer into a CDP Input.* call."""
    kind = msg.get("type")
    try:
        if kind == "mouse":
            await cdp.send("Input.dispatchMouseEvent", {
                "type": msg["event"],  # mouseMoved | mousePressed | mouseReleased | mouseWheel
                "x": msg["x"],
                "y": msg["y"],
                "button": msg.get("button", "left"),
                "clickCount": msg.get("clickCount", 1),
                "deltaX": msg.get("deltaX", 0),
                "deltaY": msg.get("deltaY", 0),
            })
        elif kind == "key":
            event_type = msg["event"]  # rawKeyDown | keyUp
            await cdp.send("Input.dispatchKeyEvent", {
                "type": event_type,
                "key": msg.get("key", ""),
                "code": msg.get("code", ""),
                "windowsVirtualKeyCode": msg.get("keyCode", 0),
                "nativeVirtualKeyCode": msg.get("keyCode", 0),
            })
        elif kind == "text":
            await cdp.send("Input.insertText", {"text": msg["text"]})
    except Exception:
        pass  # a dropped input event isn't worth failing the whole live session over
