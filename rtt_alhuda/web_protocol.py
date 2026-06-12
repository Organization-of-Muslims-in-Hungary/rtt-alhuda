"""Outbound messages broadcast to all connected debug WebSocket clients."""

import json
from typing import Optional

from rtt_alhuda.models import ServerSession


async def _broadcast(session: ServerSession, data: str) -> None:
    """Send a JSON string to every connected debug WebSocket client.

    Silently removes clients whose connections have broken.
    """
    stale: list = []
    for ws in list(session.debug_ws_clients):
        if ws.closed:
            stale.append(ws)
            continue
        try:
            await ws.send_str(data)
        except (ConnectionResetError, ConnectionError, RuntimeError, OSError):
            stale.append(ws)
    for ws in stale:
        session.debug_ws_clients.discard(ws)
        session.mic_subscribers.discard(ws)
        session.tts_subscribers.discard(ws)


async def send_log(
    session: ServerSession,
    message: str,
    level: str = "info",
    timing: Optional[dict] = None,
) -> None:
    """Broadcast a structured log message to all debug WebSocket clients."""

    payload = {"type": "log", "level": level, "message": message}
    if timing is not None:
        payload["timing"] = timing
    await _broadcast(session, json.dumps(payload))


async def send_transcription(session: ServerSession, message: dict) -> None:
    """Broadcast a transcription update to all debug WebSocket clients."""

    await _broadcast(session, json.dumps(message))


async def send_sse_control(session: ServerSession, action: str, **fields) -> None:
    """Broadcast a named ``event: control`` SSE message to all /stream/text clients.

    The frontend listens with ``evtSource.addEventListener('control', ...)``,
    keeping control events separate from the default transcription data stream.
    """

    payload = {"action": action, **fields}
    sse_msg = f"event: control\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
    stale: list = []
    for sse_resp in list(session.text_sse_clients):
        try:
            await sse_resp.write(sse_msg)
        except Exception:
            stale.append(sse_resp)
    for resp in stale:
        session.text_sse_clients.discard(resp)
