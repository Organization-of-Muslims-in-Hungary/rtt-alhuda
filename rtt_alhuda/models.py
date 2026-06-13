"""Server-owned session state (decoupled from any single WebSocket)."""

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
class ServerSession:
    """Server-owned recording session, independent of any WebSocket connection.

    A single instance lives on the application for the lifetime of the server.
    Debug WebSocket clients come and go without affecting recording state.
    """

    pcm_buffer: bytearray = field(default_factory=bytearray)
    buffer_start_sample: int = 0
    total_samples_written: int = 0
    chunk_history: list[ChunkInfo] = field(default_factory=list)
    recorder_task: Optional[asyncio.Task] = None
    processor_task: Optional[asyncio.Task] = None
    recording: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_chunk_end_sample: int = 0
    media_tts_language: str = "en"

    # Debug WebSocket observers (multiple allowed, connect/disconnect freely).
    debug_ws_clients: set[web.WebSocketResponse] = field(default_factory=set)
    # Per-client audio preview subscriptions.
    mic_subscribers: set[web.WebSocketResponse] = field(default_factory=set)
    tts_subscribers: set[web.WebSocketResponse] = field(default_factory=set)
    # Background tasks for debug audio preview.
    mic_sender_task: Optional[asyncio.Task] = None
    tts_sender_task: Optional[asyncio.Task] = None
    media_mic_queue: Optional[asyncio.Queue[bytes]] = None
    media_tts_queue: Optional[asyncio.Queue[bytes]] = None

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
    # SSE /stream/text clients (app-level, never tied to any WebSocket).
    text_sse_clients: set[web.StreamResponse] = field(default_factory=set)
    # Per-client SSE mapping: client_id -> set of StreamResponses.
    # Multiple tabs/windows may share a localStorage client_id; all of them
    # are kept so targeted control reaches every tab for that device.
    client_sse_map: dict[str, set[web.StreamResponse]] = field(default_factory=dict)
