"""Outbound messages to the browser (WebSocket today; swappable later)."""

import json
from typing import Optional

from rtt_alhuda.models import ClientState


async def send_log(
    client: ClientState,
    message: str,
    level: str = "info",
    timing: Optional[dict] = None,
) -> None:
    """Send a structured log message to the connected browser."""

    payload = {"type": "log", "level": level, "message": message}
    if timing is not None:
        payload["timing"] = timing
    if not client.ws.closed:
        await client.ws.send_str(json.dumps(payload))


async def send_transcription(client: ClientState, message: dict) -> None:
    """Send a transcription update JSON object to the browser."""

    if not client.ws.closed:
        await client.ws.send_str(json.dumps(message))
