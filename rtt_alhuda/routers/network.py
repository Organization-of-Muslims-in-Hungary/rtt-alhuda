"""Network status endpoint (org-scoped)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from rtt_alhuda.dependencies import get_org_session, require_org_operator
from rtt_alhuda.models import ServerSession
from rtt_alhuda.web_protocol import is_ws_closed

router = APIRouter()


@router.get("/api/orgs/{org_slug}/network-status")
async def network_status(
    session: ServerSession = Depends(get_org_session),
    _user=Depends(require_org_operator),
) -> dict:
    """Return live connection counts for SSE, debug WS, and satellite clients."""
    async with session.lock:
        ws_satellites = {
            "ar": sum(1 for w in session.original_audio_satellites if not is_ws_closed(w)),
            "en": sum(
                1 for w in session.tts_satellites.get("en", ()) if not is_ws_closed(w)
            ),
            "hu": sum(
                1 for w in session.tts_satellites.get("hu", ()) if not is_ws_closed(w)
            ),
        }

    debug_count = sum(1 for w in session.debug_ws_clients if not is_ws_closed(w))

    return {
        "sse_clients": len(session.text_sse_clients),
        "debug_ws_clients": debug_count,
        "ws_recording": session.recording,
        "ws_satellites": ws_satellites,
    }
