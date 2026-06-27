"""Tests for audio capture including feed_remote_audio."""

import asyncio

import pytest

from rtt_alhuda.audio_capture import feed_remote_audio
from rtt_alhuda.config import CHANNELS, MAX_BUFFER_SECONDS, SAMPLE_RATE, SAMPLE_WIDTH_BYTES
from rtt_alhuda.models import ServerSession


@pytest.mark.asyncio
async def test_feed_remote_audio_writes_to_buffer():
    """PCM data written to the session buffer can be read back."""
    session = ServerSession()
    session.recording = True
    session.media_mic_queue = asyncio.Queue(maxsize=50)
    session.original_pcm_queue = asyncio.Queue(maxsize=50)

    pcm = b"\x00\x01" * 1600
    await feed_remote_audio(session, pcm)

    async with session.lock:
        assert len(session.pcm_buffer) == len(pcm)
        assert session.total_samples_written == 1600


@pytest.mark.asyncio
async def test_feed_remote_audio_fanout_queues():
    """PCM data should appear in both mic_queue and original_pcm_queue."""
    session = ServerSession()
    session.recording = True
    session.media_mic_queue = asyncio.Queue(maxsize=50)
    session.original_pcm_queue = asyncio.Queue(maxsize=50)

    pcm = b"\x00\x01" * 160
    await feed_remote_audio(session, pcm)

    mic_data = await asyncio.wait_for(session.media_mic_queue.get(), timeout=1)
    orig_data = await asyncio.wait_for(session.original_pcm_queue.get(), timeout=1)
    assert mic_data == pcm
    assert orig_data == pcm


@pytest.mark.asyncio
async def test_feed_remote_audio_buffer_overflow():
    """When buffer exceeds MAX_BUFFER_SECONDS, old data is trimmed."""
    session = ServerSession()
    session.recording = True

    bytes_per_frame = CHANNELS * SAMPLE_WIDTH_BYTES
    max_samples = int(SAMPLE_RATE * MAX_BUFFER_SECONDS)

    pcm = b"\x00\x01" * 1600
    for _ in range(int(MAX_BUFFER_SECONDS / 0.1) + 10):
        await feed_remote_audio(session, pcm)

    async with session.lock:
        max_bytes = max_samples * bytes_per_frame
        assert len(session.pcm_buffer) <= max_bytes


@pytest.mark.asyncio
async def test_feed_remote_audio_no_queues():
    """Should not crash when queues are None."""
    session = ServerSession()
    session.recording = True
    pcm = b"\x00\x01" * 160
    await feed_remote_audio(session, pcm)


@pytest.mark.asyncio
async def test_feed_remote_audio_total_samples_increment():
    """total_samples_written should increase by the correct amount."""
    session = ServerSession()
    session.recording = True

    pcm = b"\x00\x01" * 1600
    await feed_remote_audio(session, pcm)
    assert session.total_samples_written == 1600

    await feed_remote_audio(session, pcm)
    assert session.total_samples_written == 3200


@pytest.mark.asyncio
async def test_feed_remote_audio_concurrent_safe():
    """Concurrent calls to feed_remote_audio should not corrupt state."""
    session = ServerSession()
    session.recording = True
    session.media_mic_queue = asyncio.Queue(maxsize=200)
    session.original_pcm_queue = asyncio.Queue(maxsize=200)

    pcm = b"\x00\x01" * 1600

    async def feed_many(n: int):
        for _ in range(n):
            await feed_remote_audio(session, pcm)

    await asyncio.gather(feed_many(10), feed_many(10))

    async with session.lock:
        assert session.total_samples_written == 20 * 1600
