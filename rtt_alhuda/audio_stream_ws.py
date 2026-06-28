"""WebSocket binary-frame senders for mic PCM and TTS audio streams."""

from __future__ import annotations

import asyncio

from rtt_alhuda.models import ServerSession
from rtt_alhuda.web_protocol import is_ws_closed

MIC_PREFIX = b"\x01"
TTS_PREFIX = b"\x02"


async def mic_ws_sender(session: ServerSession) -> None:
    """Drain media_mic_queue and broadcast prefixed PCM to mic subscribers."""

    queue = session.media_mic_queue
    if queue is None:
        return

    try:
        while session.recording:
            try:
                pcm = await asyncio.wait_for(queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue

            frame = MIC_PREFIX + pcm
            stale: list = []
            for ws in list(session.mic_subscribers):
                if is_ws_closed(ws):
                    stale.append(ws)
                    continue
                try:
                    await ws.send_bytes(frame)
                except (ConnectionResetError, ConnectionError, RuntimeError):
                    stale.append(ws)
            for ws in stale:
                session.mic_subscribers.discard(ws)
    except (ConnectionResetError, ConnectionError):
        pass


async def mic_original_fanout_loop(session: ServerSession) -> None:
    """Fan-out live mic PCM (0x01) to /{org}/stream/tts/ar satellite sockets."""

    queue = session.original_pcm_queue
    if queue is None:
        return
    try:
        while session.recording:
            try:
                pcm = await asyncio.wait_for(queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            async with session.lock:
                targets = [
                    w for w in session.original_audio_satellites if not is_ws_closed(w)
                ]
            for sat_ws in targets:
                try:
                    await sat_ws.send_bytes(MIC_PREFIX + pcm)
                except (ConnectionResetError, ConnectionError, TypeError):
                    async with session.lock:
                        session.original_audio_satellites.discard(sat_ws)
    except asyncio.CancelledError:
        raise
    except (ConnectionResetError, ConnectionError):
        pass


async def tts_ws_sender(session: ServerSession) -> None:
    """Drain media_tts_queue and broadcast prefixed MP3 to tts subscribers."""

    queue = session.media_tts_queue
    if queue is None:
        return

    try:
        while session.recording:
            try:
                audio = await asyncio.wait_for(queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue

            frame = TTS_PREFIX + audio
            stale: list = []
            for ws in list(session.tts_subscribers):
                if is_ws_closed(ws):
                    stale.append(ws)
                    continue
                try:
                    await ws.send_bytes(frame)
                except (ConnectionResetError, ConnectionError, RuntimeError):
                    stale.append(ws)
            for ws in stale:
                session.tts_subscribers.discard(ws)
    except (ConnectionResetError, ConnectionError):
        pass


async def tts_fanout_loop(session: ServerSession, lang: str) -> None:
    """Drain per-lang TTS queue; fan-out MP3 to all satellite sockets for ``lang``."""

    queues = session.tts_queues
    if not queues or lang not in queues:
        return
    q = queues[lang]
    try:
        while session.recording:
            try:
                audio = await asyncio.wait_for(q.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            async with session.lock:
                targets = [
                    w
                    for w in session.tts_satellites.get(lang, ())
                    if not is_ws_closed(w)
                ]
            for sat_ws in targets:
                try:
                    await sat_ws.send_bytes(TTS_PREFIX + audio)
                except (ConnectionResetError, ConnectionError, TypeError):
                    async with session.lock:
                        session.tts_satellites[lang].discard(sat_ws)
    except asyncio.CancelledError:
        raise
    except (ConnectionResetError, ConnectionError):
        pass
