"""WebSocket binary-frame senders for mic PCM and TTS audio streams."""

import asyncio

from rtt_alhuda.models import ClientState

MIC_PREFIX = b"\x01"
TTS_PREFIX = b"\x02"


async def mic_ws_sender(client: ClientState) -> None:
    """Drain media_mic_queue and send prefixed PCM frames over WebSocket."""

    queue = client.media_mic_queue
    if queue is None:
        return

    try:
        while client.recording and not client.ws.closed:
            try:
                pcm = await asyncio.wait_for(queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue

            if client.ws_mic_subscribed and not client.ws.closed:
                await client.ws.send_bytes(MIC_PREFIX + pcm)
    except (ConnectionResetError, ConnectionError):
        pass


async def tts_ws_sender(client: ClientState) -> None:
    """Drain media_tts_queue and send prefixed MP3 frames over WebSocket."""

    queue = client.media_tts_queue
    if queue is None:
        return

    try:
        while client.recording and not client.ws.closed:
            try:
                audio = await asyncio.wait_for(queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue

            if client.ws_tts_subscribed and not client.ws.closed:
                await client.ws.send_bytes(TTS_PREFIX + audio)
    except (ConnectionResetError, ConnectionError):
        pass
