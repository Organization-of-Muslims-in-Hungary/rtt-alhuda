"""Microphone capture (server device or browser WebSocket PCM) into client buffer."""

import asyncio

from rtt_alhuda.config import (
    CHANNELS,
    FRAME_CHUNK_SECONDS,
    MAX_BUFFER_SECONDS,
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
)
from rtt_alhuda.models import ClientState
from rtt_alhuda.web_protocol import send_log

_BYTES_PER_FRAME = CHANNELS * SAMPLE_WIDTH_BYTES
_MAX_BUFFER_SAMPLES = int(SAMPLE_RATE * MAX_BUFFER_SECONDS)


async def ingest_pcm_bytes(client: ClientState, pcm_bytes: bytes) -> None:
    """Append one mono int16 little-endian chunk (16 kHz) to rolling buffer + tap queues."""

    if not pcm_bytes or len(pcm_bytes) % _BYTES_PER_FRAME != 0:
        return
    sample_count = len(pcm_bytes) // _BYTES_PER_FRAME
    async with client.lock:
        client.pcm_buffer.extend(pcm_bytes)
        client.total_samples_written += sample_count

        available_samples = client.total_samples_written - client.buffer_start_sample
        overflow_samples = available_samples - _MAX_BUFFER_SAMPLES
        if overflow_samples > 0:
            overflow_bytes = overflow_samples * _BYTES_PER_FRAME
            del client.pcm_buffer[:overflow_bytes]
            client.buffer_start_sample += overflow_samples

    mic_q = client.media_mic_queue
    if mic_q is not None:
        try:
            mic_q.put_nowait(pcm_bytes)
        except asyncio.QueueFull:
            pass
    orig_q = client.original_pcm_queue
    if orig_q is not None:
        try:
            orig_q.put_nowait(pcm_bytes)
        except asyncio.QueueFull:
            pass


async def capture_microphone_loop(client: ClientState) -> None:
    """Continuously read **server** microphone into the client's rolling buffer."""

    try:
        import sounddevice as sd
    except ImportError:
        await send_log(
            client,
            "Missing dependency: sounddevice. Install with: pip install sounddevice",
            "error",
        )
        client.recording = False
        return

    frame_chunk = int(SAMPLE_RATE * FRAME_CHUNK_SECONDS)
    bytes_per_frame = CHANNELS * SAMPLE_WIDTH_BYTES

    try:
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=frame_chunk,
        ) as stream:
            await send_log(client, "Server microphone capture started")
            while client.recording and not client.ws.closed:
                data, overflowed = await asyncio.to_thread(stream.read, frame_chunk)
                if overflowed:
                    await send_log(client, "Input overflow detected", "warn")

                pcm_bytes = bytes(data)
                await ingest_pcm_bytes(client, pcm_bytes)
    except Exception as exc:
        await send_log(client, f"Microphone error: {exc}", "error")
        client.recording = False
    finally:
        client.recording = False
        await send_log(client, "Server microphone capture stopped")
