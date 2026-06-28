"""Per-client SSE channel backed by an asyncio queue.

FastAPI's ``StreamingResponse`` is produced by a generator, so instead of
holding a writable response object (as aiohttp did) we store one
``SseChannel`` per connected client. The audio pipeline calls ``write()``
exactly like the old ``StreamResponse.write()``; the SSE route generator
drains ``queue`` and yields the bytes.
"""

from __future__ import annotations

import asyncio


class SseChannel:
    """A single SSE client connection."""

    def __init__(self, max_queue: int = 256) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=max_queue)
        self.closed: bool = False

    async def write(self, data: bytes) -> None:
        """Push bytes to the client queue.

        Mirrors the old ``aiohttp.web.StreamResponse.write`` contract so the
        audio processor and SSE control broadcaster do not need to change.
        """
        if self.closed:
            return
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            self.closed = True

    def close(self) -> None:
        self.closed = True
