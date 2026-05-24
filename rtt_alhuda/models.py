"""Per-session state for WebSocket clients."""

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from aiohttp import web


def _tts_satellite_sets() -> dict[str, set[web.WebSocketResponse]]:
    return {"en": set(), "hu": set()}


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
    # True: PCM arrives as WebSocket binary from the browser (no server mic).
    use_client_microphone: bool = False
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
    # Per-language TTS for GET /stream/tts/{en|hu} (MP3). ``ar`` uses original PCM below.
    tts_queues: Optional[dict[str, asyncio.Queue[bytes]]] = None
    tts_fanout_tasks: Optional[dict[str, asyncio.Task]] = None
    tts_satellites: dict[str, set[web.WebSocketResponse]] = field(
        default_factory=_tts_satellite_sets,
    )
    # Live mic PCM for GET /stream/tts/ar (original speech; not TTS).
    original_pcm_queue: Optional[asyncio.Queue[bytes]] = None
    original_fanout_task: Optional[asyncio.Task] = None
    original_audio_satellites: set[web.WebSocketResponse] = field(default_factory=set)
