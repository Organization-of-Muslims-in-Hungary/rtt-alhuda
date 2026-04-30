"""Tests for WebSocket audio streaming over binary frames."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from rtt_alhuda.audio_stream_ws import mic_ws_sender, tts_ws_sender


def _make_client(*, mic_subscribed: bool = True, tts_subscribed: bool = True):
    """Build a minimal mock ClientState for sender tests."""
    client = MagicMock()
    client.ws = AsyncMock()
    client.ws.closed = False
    client.recording = True
    client.ws_mic_subscribed = mic_subscribed
    client.ws_tts_subscribed = tts_subscribed
    client.media_mic_queue = asyncio.Queue()
    client.media_tts_queue = asyncio.Queue()
    return client


@pytest.mark.asyncio
async def test_mic_sender_prefixes_with_0x01():
    client = _make_client()
    pcm = b"\x00\x01" * 160
    await client.media_mic_queue.put(pcm)

    async def stop_after_one(*_args, **_kwargs):
        client.recording = False

    client.ws.send_bytes = AsyncMock(side_effect=stop_after_one)

    await mic_ws_sender(client)

    client.ws.send_bytes.assert_called_once()
    sent = client.ws.send_bytes.call_args[0][0]
    assert sent[0:1] == b"\x01"
    assert sent[1:] == pcm


@pytest.mark.asyncio
async def test_tts_sender_prefixes_with_0x02():
    client = _make_client()
    mp3 = b"\xff\xfb\x90\x00" + b"\x00" * 100
    await client.media_tts_queue.put(mp3)

    async def stop_after_one(*_args, **_kwargs):
        client.recording = False

    client.ws.send_bytes = AsyncMock(side_effect=stop_after_one)

    await tts_ws_sender(client)

    client.ws.send_bytes.assert_called_once()
    sent = client.ws.send_bytes.call_args[0][0]
    assert sent[0:1] == b"\x02"
    assert sent[1:] == mp3


@pytest.mark.asyncio
async def test_mic_sender_discards_when_unsubscribed():
    client = _make_client(mic_subscribed=False)
    pcm = b"\x00\x01" * 160
    await client.media_mic_queue.put(pcm)

    original_get = client.media_mic_queue.get

    call_count = 0
    async def counting_get():
        nonlocal call_count
        result = await original_get()
        call_count += 1
        client.recording = False
        return result

    client.media_mic_queue.get = counting_get

    await mic_ws_sender(client)

    assert call_count == 1
    client.ws.send_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_tts_sender_discards_when_unsubscribed():
    client = _make_client(tts_subscribed=False)
    mp3 = b"\xff\xfb\x90\x00" + b"\x00" * 100
    await client.media_tts_queue.put(mp3)

    original_get = client.media_tts_queue.get

    call_count = 0
    async def counting_get():
        nonlocal call_count
        result = await original_get()
        call_count += 1
        client.recording = False
        return result

    client.media_tts_queue.get = counting_get

    await tts_ws_sender(client)

    assert call_count == 1
    client.ws.send_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_mic_sender_exits_on_ws_closed():
    client = _make_client()
    client.ws.closed = True
    await client.media_mic_queue.put(b"\x00" * 320)

    await mic_ws_sender(client)

    client.ws.send_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_tts_sender_exits_on_ws_closed():
    client = _make_client()
    client.ws.closed = True
    await client.media_tts_queue.put(b"\x00" * 100)

    await tts_ws_sender(client)

    client.ws.send_bytes.assert_not_called()
