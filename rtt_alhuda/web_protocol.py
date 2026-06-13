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


async def send_sse_control(
    session: ServerSession,
    action: str,
    *,
    target_client_id: Optional[str] = None,
    **fields,
) -> None:
    """Send a named ``event: control`` SSE message.

    If *target_client_id* is given, sends only to that client's SSE connection.
    Otherwise broadcasts to all /stream/text clients (legacy behaviour).
    """

    payload = {"action": action, **fields}
    sse_msg = f"event: control\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()

    if target_client_id:
        sse_set = session.client_sse_map.get(target_client_id)
        if sse_set:
            stale: list = []
            for sse_resp in list(sse_set):
                try:
                    await sse_resp.write(sse_msg)
                except Exception:
                    stale.append(sse_resp)
            for resp in stale:
                sse_set.discard(resp)
                session.text_sse_clients.discard(resp)
            if not sse_set:
                del session.client_sse_map[target_client_id]
        return

    stale_resp: list = []
    for sse_resp in list(session.text_sse_clients):
        try:
            await sse_resp.write(sse_msg)
        except Exception:
            stale_resp.append(sse_resp)
    for resp in stale_resp:
        session.text_sse_clients.discard(resp)
        for cid, s in list(session.client_sse_map.items()):
            s.discard(resp)
            if not s:
                del session.client_sse_map[cid]
