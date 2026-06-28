"""Recording control API (org-scoped)."""

from __future__ import annotations

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, HTTPException, status

from rtt_alhuda.dependencies import (
    get_http_client,
    get_org_session,
    require_org_operator,
)
from rtt_alhuda.models import ServerSession
from rtt_alhuda.recording import start_recording, stop_recording

router = APIRouter()


@router.get("/api/orgs/{org_slug}/control/{action}")
async def control(
    action: str,
    source: str = "internal",
    session: ServerSession = Depends(get_org_session),
    _user=Depends(require_org_operator),
    http: ClientSession = Depends(get_http_client),
) -> dict:
    """Remote start/stop/status of recording for an organization."""
    if action == "status":
        return {"recording": session.recording}

    if action == "start":
        if session.recording:
            return {"ok": False, "reason": "already_recording"}
        if source not in ("internal", "remote"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_source")
        await start_recording(session, http, audio_source=source)
        return {"ok": True, "action": "started"}

    if action == "stop":
        if not session.recording:
            return {"ok": False, "reason": "not_recording"}
        await stop_recording(session)
        return {"ok": True, "action": "stopped"}

    raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown_action")
