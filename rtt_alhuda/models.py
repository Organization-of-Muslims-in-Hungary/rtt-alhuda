"""Per-session state for WebSocket clients."""

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from aiohttp import web


@dataclass
class ChunkInfo:
    """Stores exact sample boundaries and results for a transcribed audio chunk."""

    start_sample: int
    end_sample: int
    ar: str
    en: str
    hu: str


@dataclass
class ClientState:
    """Per-WebSocket runtime state for one connected browser session."""

    ws: web.WebSocketResponse
    pcm_buffer: bytearray = field(default_factory=bytearray)
    buffer_start_sample: int = 0
    total_samples_written: int = 0
    chunk_history: list[ChunkInfo] = field(default_factory=list)
    recorder_task: Optional[asyncio.Task] = None
    processor_task: Optional[asyncio.Task] = None
    recording: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_chunk_end_sample: int = 0
    # Audio stream taps (created when recording starts).
    media_mic_queue: Optional[asyncio.Queue[bytes]] = None
    media_tts_queue: Optional[asyncio.Queue[bytes]] = None
    media_tts_language: str = "en"
    ws_mic_subscribed: bool = False
    ws_tts_subscribed: bool = False
    mic_sender_task: Optional[asyncio.Task] = None
    tts_sender_task: Optional[asyncio.Task] = None
