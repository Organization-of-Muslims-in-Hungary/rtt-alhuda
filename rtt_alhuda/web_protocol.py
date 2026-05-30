"""Outbound messages to the browser (WebSocket today; swappable later)."""

import json

from rtt_alhuda.models import ClientState


async def send_log(client: ClientState, message: str, level: str = "info") -> None:
    """Send a structured log message to the connected browser."""

    payload = {"type": "log", "level": level, "message": message}
    if client.ws and not client.ws.closed:
        try:
            await client.ws.send_str(json.dumps(payload))
        except Exception:
            pass


async def send_transcription(client: ClientState, message: dict) -> None:
    """Send a transcription update JSON object to the browser."""

    if client.ws and not client.ws.closed:
        try:
            await client.ws.send_str(json.dumps(message))
        except Exception:
            pass
