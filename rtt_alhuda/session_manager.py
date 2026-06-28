"""Per-organization runtime recording sessions."""

from __future__ import annotations

import asyncio
import uuid

from rtt_alhuda.models import ServerSession


class SessionManager:
    """Owns one ``ServerSession`` per organization."""

    def __init__(self) -> None:
        self._sessions: dict[uuid.UUID, ServerSession] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, org_id: uuid.UUID) -> ServerSession:
        async with self._lock:
            session = self._sessions.get(org_id)
            if session is None:
                session = ServerSession()
                self._sessions[org_id] = session
            return session

    def get(self, org_id: uuid.UUID) -> ServerSession | None:
        return self._sessions.get(org_id)

    def all_org_ids(self) -> list[uuid.UUID]:
        return list(self._sessions.keys())

    async def stop_all(self) -> None:
        """Stop every active recording (used on application shutdown)."""
        from rtt_alhuda.audio_capture import stop_recording

        async with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.recording:
                await stop_recording(session)
