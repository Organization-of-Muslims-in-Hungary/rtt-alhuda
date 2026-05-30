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

    while client.recording:
        try:
            pcm = await asyncio.wait_for(queue.get(), timeout=0.2)
        except asyncio.TimeoutError:
            continue

        if client.ws and client.ws_mic_subscribed and not client.ws.closed:
            try:
                await client.ws.send_bytes(MIC_PREFIX + pcm)
            except Exception:
                pass


async def mic_original_fanout_loop(client: ClientState) -> None:
    """Fan-out live mic PCM (0x01) to /stream/tts/ar satellite sockets."""

    queue = client.original_pcm_queue
    if queue is None:
        return
    try:
        while client.recording:
            try:
                pcm = await asyncio.wait_for(queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            async with client.lock:
                targets = [w for w in client.original_audio_satellites if not w.closed]
            for sat_ws in targets:
                try:
                    await sat_ws.send_bytes(MIC_PREFIX + pcm)
                except (ConnectionResetError, ConnectionError, TypeError):
                    async with client.lock:
                        client.original_audio_satellites.discard(sat_ws)
    except asyncio.CancelledError:
        raise
    except (ConnectionResetError, ConnectionError):
        pass


async def tts_ws_sender(client: ClientState) -> None:
    """Drain media_tts_queue and send prefixed MP3 frames over WebSocket."""

    queue = client.media_tts_queue
    if queue is None:
        return

    while client.recording:
        try:
            audio = await asyncio.wait_for(queue.get(), timeout=0.2)
        except asyncio.TimeoutError:
            continue

        if client.ws and client.ws_tts_subscribed and not client.ws.closed:
            try:
                await client.ws.send_bytes(TTS_PREFIX + audio)
            except Exception:
                pass


async def tts_fanout_loop(client: ClientState, lang: str) -> None:
    """Drain per-lang TTS queue; fan-out MP3 to all satellite sockets for ``lang``."""

    queues = client.tts_queues
    if not queues or lang not in queues:
        return
    q = queues[lang]
    try:
        while client.recording:
            try:
                audio = await asyncio.wait_for(q.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            async with client.lock:
                targets = [
                    w for w in client.tts_satellites.get(lang, ()) if not w.closed
                ]
            for sat_ws in targets:
                try:
                    await sat_ws.send_bytes(TTS_PREFIX + audio)
                except (ConnectionResetError, ConnectionError, TypeError):
                    async with client.lock:
                        client.tts_satellites[lang].discard(sat_ws)
    except asyncio.CancelledError:
        raise
    except (ConnectionResetError, ConnectionError):
        pass
