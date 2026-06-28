"""Browser/display control API (org-scoped)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, Depends

from rtt_alhuda.dependencies import get_org_session, require_org_operator
from rtt_alhuda.models import ServerSession
from rtt_alhuda.web_protocol import send_sse_control

router = APIRouter()

_XENV = {"DISPLAY": ":0", "XAUTHORITY": "/home/pi/.Xauthority"}

_BROWSER_PAGES = {
    "app": "http://localhost/app",
    "tv": "http://localhost/tv",
    "operator": "http://localhost/",
    "control": "http://localhost/control",
}


async def _run(cmd: str) -> tuple[int, str]:
    """Run a shell command asynchronously; return (returncode, combined output)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **_XENV},
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    return proc.returncode, (out or b"").decode(errors="replace").strip()


@router.get("/api/orgs/{org_slug}/browser/{action:path}")
async def browser(
    action: str,
    target: str | None = None,
    session: ServerSession = Depends(get_org_session),
    _user=Depends(require_org_operator),
) -> dict:
    """Control displays via SSE or Pi-specific OS actions."""
    # ── SSE-based actions (OS-independent) ────────────────────────
    if action.startswith("navigate/"):
        page_key = action.split("/", 1)[1]
        if page_key not in _BROWSER_PAGES:
            return {"ok": False, "reason": f"unknown page '{page_key}'"}
        await send_sse_control(session, "navigate", target_client_id=target, page=page_key)
        return {"ok": True, "action": f"navigate:{page_key}", "target": target}

    if action == "refresh":
        await send_sse_control(session, "refresh", target_client_id=target)
        return {"ok": True, "action": "refresh", "target": target}

    if action.startswith("language/"):
        lang = action.split("/", 1)[1]
        if not target:
            session.media_tts_language = lang
        await send_sse_control(session, "lang_switch", target_client_id=target, lang=lang)
        return {"ok": True, "lang": lang, "target": target}

    # ── OS-level actions (Pi-specific) ────────────────────────────
    browser_bin = (
        "chromium-browser"
        if Path("/usr/bin/chromium-browser").exists()
        else "chromium"
    )

    if action == "exit-kiosk":
        url = _BROWSER_PAGES["app"]
        await _run("pkill -f chromium 2>/dev/null; sleep 1")
        await _run(
            f"sudo -u pi DISPLAY=:0 XAUTHORITY=/home/pi/.Xauthority "
            f"{browser_bin} --start-maximized --noerrdialogs --no-first-run '{url}' &"
        )
        return {"ok": True, "action": "exit-kiosk"}

    if action == "kiosk":
        url = _BROWSER_PAGES["app"]
        await _run("pkill -f chromium 2>/dev/null; sleep 1")
        await _run(
            f"sudo -u pi DISPLAY=:0 XAUTHORITY=/home/pi/.Xauthority "
            f"{browser_bin} --kiosk --noerrdialogs --disable-infobars "
            f"--no-first-run '{url}' &"
        )
        return {"ok": True, "action": "kiosk"}

    if action == "close":
        await _run("pkill -f chromium 2>/dev/null")
        return {"ok": True, "action": "close"}

    return {"ok": False, "reason": "unknown browser action"}
