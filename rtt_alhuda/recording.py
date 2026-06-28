"""Recording lifecycle helpers (ported from the old aiohttp web layer)."""

from __future__ import annotations

import asyncio
import time

from aiohttp import ClientSession

from rtt_alhuda.audio_capture import capture_microphone_loop, stop_recording
from rtt_alhuda.audio_processor import process_audio_loop
from rtt_alhuda.audio_stream_ws import (
    mic_original_fanout_loop,
    mic_ws_sender,
    tts_fanout_loop,
    tts_ws_sender,
)
from rtt_alhuda.models import ServerSession
from rtt_alhuda.web_protocol import send_log

__all__ = ["start_recording", "stop_recording"]


async def start_recording(
    session: ServerSession,
    http: ClientSession,
    audio_source: str = "internal",
) -> None:
    """Reset session state and start microphone capture plus audio processing.

    *audio_source* controls where PCM comes from:
      - ``"internal"`` — the server's local microphone (sounddevice)
      - ``"remote"``  — PCM pushed over a WebSocket from the frontend.
    """

    if session.recording:
        await send_log(session, "Recording already running", "warn")
        return

    session.recording = True
    session.audio_source = audio_source
    session.pcm_buffer.clear()
    session.buffer_start_sample = 0
    session.total_samples_written = 0
    session.chunk_history.clear()
    session.last_chunk_end_sample = 0
    # Clear stale remote-mic state from previous sessions and initialise the
    # watchdog timestamp so a remote recording that never receives audio will
    # still time out.
    session.remote_mic_ws = None
    session.last_remote_audio_ts = time.monotonic() if audio_source == "remote" else 0.0

    session.media_mic_queue = asyncio.Queue(maxsize=50)
    session.media_tts_queue = asyncio.Queue(maxsize=8)
    session.original_pcm_queue = asyncio.Queue(maxsize=50)
    session.original_fanout_task = asyncio.create_task(
        mic_original_fanout_loop(session),
        name="mic-original-fanout",
    )
    session.tts_queues = {
        "en": asyncio.Queue(maxsize=8),
        "hu": asyncio.Queue(maxsize=8),
    }
    session.tts_fanout_tasks = {
        lang: asyncio.create_task(
            tts_fanout_loop(session, lang),
            name=f"tts-fanout-{lang}",
        )
        for lang in ("en", "hu")
    }

    if audio_source == "internal":
        session.recorder_task = asyncio.create_task(capture_microphone_loop(session))
    else:
        session.recorder_task = None

    session.processor_task = asyncio.create_task(process_audio_loop(session, http))
    session.mic_sender_task = asyncio.create_task(mic_ws_sender(session))
    session.tts_sender_task = asyncio.create_task(tts_ws_sender(session))

    await send_log(
        session,
        f"Recording started (audio_source={audio_source})",
    )
