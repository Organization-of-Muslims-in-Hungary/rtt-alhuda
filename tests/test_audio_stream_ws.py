"""Tests for WebSocket audio streaming over binary frames."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketState

from rtt_alhuda.audio_stream_ws import (
    mic_original_fanout_loop,
    mic_ws_sender,
    tts_fanout_loop,
    tts_ws_sender,
)
from rtt_alhuda.models import ServerSession


def _make_session(
    *,
    mic_subscribers: list | None = None,
    tts_subscribers: list | None = None,
) -> ServerSession:
    """Build a ServerSession with queues and optional subscriber mocks."""
    session = ServerSession()
    session.recording = True
    session.media_mic_queue = asyncio.Queue()
    session.media_tts_queue = asyncio.Queue()
    if mic_subscribers:
        session.mic_subscribers = set(mic_subscribers)
    if tts_subscribers:
        session.tts_subscribers = set(tts_subscribers)
    return session


def _mock_ws(*, closed: bool = False) -> AsyncMock:
    ws = AsyncMock()
    ws.client_state = WebSocketState.DISCONNECTED if closed else WebSocketState.CONNECTED
    ws.application_state = (
        WebSocketState.DISCONNECTED if closed else WebSocketState.CONNECTED
    )
    return ws


@pytest.mark.asyncio
async def test_mic_sender_prefixes_with_0x01():
    ws = _mock_ws()
    session = _make_session(mic_subscribers=[ws])
    pcm = b"\x00\x01" * 160
    await session.media_mic_queue.put(pcm)

    async def stop_after_one(*_args, **_kwargs):
        session.recording = False

    ws.send_bytes = AsyncMock(side_effect=stop_after_one)

    await mic_ws_sender(session)

    ws.send_bytes.assert_called_once()
    sent = ws.send_bytes.call_args[0][0]
    assert sent[0:1] == b"\x01"
    assert sent[1:] == pcm


@pytest.mark.asyncio
async def test_tts_sender_prefixes_with_0x02():
    ws = _mock_ws()
    session = _make_session(tts_subscribers=[ws])
    mp3 = b"\xff\xfb\x90\x00" + b"\x00" * 100
    await session.media_tts_queue.put(mp3)

    async def stop_after_one(*_args, **_kwargs):
        session.recording = False

    ws.send_bytes = AsyncMock(side_effect=stop_after_one)

    await tts_ws_sender(session)

    ws.send_bytes.assert_called_once()
    sent = ws.send_bytes.call_args[0][0]
    assert sent[0:1] == b"\x02"
    assert sent[1:] == mp3


@pytest.mark.asyncio
async def test_mic_sender_skips_when_no_subscribers():
    """When no WS is subscribed, frames are drained but not sent."""
    session = _make_session()  # no mic_subscribers
    pcm = b"\x00\x01" * 160
    await session.media_mic_queue.put(pcm)

    original_get = session.media_mic_queue.get
    call_count = 0

    async def counting_get():
        nonlocal call_count
        result = await original_get()
        call_count += 1
        session.recording = False
        return result

    session.media_mic_queue.get = counting_get

    await mic_ws_sender(session)

    assert call_count == 1


@pytest.mark.asyncio
async def test_tts_sender_skips_when_no_subscribers():
    """When no WS is subscribed, frames are drained but not sent."""
    session = _make_session()  # no tts_subscribers
    mp3 = b"\xff\xfb\x90\x00" + b"\x00" * 100
    await session.media_tts_queue.put(mp3)

    original_get = session.media_tts_queue.get
    call_count = 0

    async def counting_get():
        nonlocal call_count
        result = await original_get()
        call_count += 1
        session.recording = False
        return result

    session.media_tts_queue.get = counting_get

    await tts_ws_sender(session)

    assert call_count == 1


@pytest.mark.asyncio
async def test_tts_fanout_sends_prefixed_mp3_to_satellite():
    sat = _mock_ws()
    session = ServerSession()
    session.recording = True
    q = asyncio.Queue()
    session.tts_queues = {"en": q}
    session.tts_satellites = {"en": {sat}}
    mp3 = b"\xff\xfb\x90" + b"\x00" * 20

    async def stop_later():
        await q.put(mp3)
        await asyncio.sleep(0.05)
        session.recording = False

    await asyncio.gather(tts_fanout_loop(session, "en"), stop_later())
    sat.send_bytes.assert_called_once()
    sent = sat.send_bytes.call_args[0][0]
    assert sent[0:1] == b"\x02"
    assert sent[1:] == mp3


@pytest.mark.asyncio
async def test_mic_original_fanout_sends_prefixed_pcm_to_satellite():
    sat = _mock_ws()
    session = ServerSession()
    session.recording = True
    q = asyncio.Queue()
    session.original_pcm_queue = q
    session.original_audio_satellites = {sat}
    pcm = b"\x00\x01" * 160

    async def stop_later():
        await q.put(pcm)
        await asyncio.sleep(0.05)
        session.recording = False

    await asyncio.gather(mic_original_fanout_loop(session), stop_later())
    sat.send_bytes.assert_called_once()
    sent = sat.send_bytes.call_args[0][0]
    assert sent[0:1] == b"\x01"
    assert sent[1:] == pcm


@pytest.mark.asyncio
async def test_mic_sender_broadcasts_to_multiple_subscribers():
    """Multiple mic subscribers each receive the same prefixed frame."""
    ws1 = _mock_ws()
    ws2 = _mock_ws()
    session = _make_session(mic_subscribers=[ws1, ws2])
    pcm = b"\x00\x01" * 160
    await session.media_mic_queue.put(pcm)

    call_count = 0

    async def stop_after_one(*_a, **_k):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            session.recording = False

    ws1.send_bytes = AsyncMock(side_effect=stop_after_one)
    ws2.send_bytes = AsyncMock(side_effect=stop_after_one)

    await mic_ws_sender(session)

    for ws in (ws1, ws2):
        ws.send_bytes.assert_called_once()
        sent = ws.send_bytes.call_args[0][0]
        assert sent[0:1] == b"\x01"
        assert sent[1:] == pcm


@pytest.mark.asyncio
async def test_mic_sender_removes_closed_subscriber():
    """A closed WS is removed from subscribers during fanout."""
    ws_ok = _mock_ws()
    ws_dead = _mock_ws(closed=True)
    session = _make_session(mic_subscribers=[ws_ok, ws_dead])
    pcm = b"\x00\x01" * 160
    await session.media_mic_queue.put(pcm)

    async def stop(*_a, **_k):
        session.recording = False

    ws_ok.send_bytes = AsyncMock(side_effect=stop)

    await mic_ws_sender(session)

    ws_ok.send_bytes.assert_called_once()
    ws_dead.send_bytes.assert_not_called()
    assert ws_dead not in session.mic_subscribers
